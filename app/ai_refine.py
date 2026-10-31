"""DeepSeek 视觉复核：给可疑/全部字框出修正建议（app-ai，task C）。

契约（app/SPEC-APP.md「AI 复核」）：

    cfg = AIConfig(api_key="sk-...", model="deepseek-flash")
    props = refine_boxes(page_rgb, boxes, ids=None, mode="all",
                         cfg=cfg, progress_cb=cb, cancel_cb=cc)

每个 Proposal 是一个 dict（坐标一律原图像素、x1/y1 开区间，统一用 dict 表示 box）：

    {"box_id": int, "action": "tighten|split|merge|keep",
     "before": {"x0","y0","x1","y1"},
     "after":  [{"x0","y0","x1","y1"}, ...],   # tighten/merge 1 个；split 2 个；keep 空表
     "confidence": 0.0~1.0, "reason": "<=20字", "status": "pending"}

实现要点
- 只依赖已有包：numpy / PIL / openai（openai 惰性 import，没装也能 import 本模块）。
- 坐标一律原图像素；发给模型的是「相对裁块」归一化 0-1000，服务端换算回来。
- 无 key 或 cfg.mock=True：确定性 mock（每边收紧 2%），reason 带 [mock]，端到端可离线测。
- 取消：cancel_cb 返回 True 时立即停止派发未开始的框，refine_boxes 返回**已完成部分**
  （长度可能小于目标数，不抛异常）；app-api 按自己的 job.canceled 把状态置 canceled。
- 进度：progress_cb(done, total)，开始时先报 (0, total)，每完成一框报一次；回调抛错只记日志。
- 429/5xx/连接错误：指数退避重试 cfg.retries 次（默认 2，即最多 3 次请求）。
- 所有判断都可单测：crop_context / norm_bbox / denorm_bbox / parse_model_json / validate_proposal。

自测：python -m app.ai_refine --selftest     （mock + 归一化 + 校验 + 本地假 OpenAI 服务）
证据：python -m app.ai_refine --fake-server  （打印假服务记录的请求体摘要，证明请求里真带了图）
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import math
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

import numpy as np
from PIL import Image, ImageDraw

LOG = logging.getLogger("app.ai_refine")

__all__ = [
    "AIConfig", "refine_boxes", "test_connection",
    "crop_context", "norm_bbox", "denorm_bbox", "iou", "parse_model_json",
    "validate_proposal", "build_request_image", "build_prompt",
    "select_target_indices", "load_page_rgb",
]

# ---------------------------------------------------------------- 常量
NORM_MAX = 1000.0            # 归一化坐标上限（相对裁块）
TARGET_GLYPH_PX = 240        # 送模型时目标字高（像素）
MAX_IMAGE_SIDE = 4096        # 单边上限，避免超出视觉接口的 original 档
JPEG_QUALITY = 88
BOX_LINE_PX = 3              # 当前框红框线宽（画在放大后的图上）
BOX_LINE_RGB = (255, 0, 0)
CONTEXT_PAD_Y = 0.9          # 上下各扩 0.9 * 字高
CONTEXT_PAD_X = 0.5          # 左右各扩 0.5 * 字宽
IOU_MIN = 0.2
SIZE_LO, SIZE_HI = 0.4, 2.5
EDGE_TOL = 3                 # 允许模型坐标溢出页面 3px，落盘前 clamp
SPLIT_OVERLAP_MAX = 0.05     # split 两框相交面积 / 较小框面积
REASON_MAX = 20              # reason 字符上限（中文按字计）
ACTIONS = ("tighten", "split", "merge", "keep")

SYSTEM_PROMPT = (
    "你是古籍 OCR 与版面分析专家，负责判定单字检测框是否准确。"
    "你只输出一个 JSON 对象，不输出任何其他内容。"
)


# ---------------------------------------------------------------- 配置
@dataclass
class AIConfig:
    """DeepSeek 视觉接口配置。

    前 5 个字段与 app/config.py 的 DEFAULT_AI 同名，可直接
    AIConfig(**{k: cfg.ai[k] for k in (...)}) 构造；其余是可选的调参项。
    """
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    max_workers: int = 4
    timeout: float = 60.0
    mock: bool = False
    detail: str = "original"          # 视觉接口 detail：original / low
    # DeepSeek 默认开启"思考模式"：思维链计入 max_tokens，额度小的时候 content 会是空串。
    # 本任务只想要一个 JSON，关掉思考既省 token 又快（实测 30 vs 500+ tokens）。
    thinking: str = "disabled"        # "disabled" | "enabled"
    max_tokens: int = 512             # 关闭思考 512 足够；开启思考建议 >= 2000
    retries: int = 2                  # 429/5xx 的重试次数（最多 3 次请求）
    retry_backoff: float = 0.5        # 首次退避秒数，之后翻倍（封顶 8s）
    mock_delay: float = 0.0           # mock 每框耗时（测并发/取消用）
    target_glyph_px: int = TARGET_GLYPH_PX
    max_image_side: int = MAX_IMAGE_SIDE
    jpeg_quality: int = JPEG_QUALITY


class _CancelSignal(Exception):
    """内部取消信号：不在 refine_boxes 之外暴露（取消时只返回已完成部分）。"""


# ---------------------------------------------------------------- 基础工具
def _short(value: Any, limit: int = 120) -> str:
    s = str(value).replace("\r", " ").replace("\n", " ").strip()
    return s[:limit]


def _as_rgb(page_rgb: Any) -> np.ndarray:
    a = np.asarray(page_rgb)
    if a.ndim == 2:
        a = np.repeat(a[:, :, None], 3, axis=2)
    if a.ndim != 3 or a.shape[2] not in (3, 4):
        raise ValueError("page_rgb 必须是 HxWx3 的 RGB ndarray（收到 shape=%r）" % (a.shape,))
    if a.shape[2] == 4:
        a = a[:, :, :3]
    if a.dtype != np.uint8:
        a = np.clip(a, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(a)


def load_page_rgb(path: str) -> np.ndarray:
    """读整页为 RGB ndarray（PIL，兼容 16bit/RGBA；不依赖 yi_core）。"""
    Image.MAX_IMAGE_PIXELS = None
    with Image.open(path) as im:
        return np.ascontiguousarray(np.asarray(im.convert("RGB"), dtype=np.uint8))


def _box_xyxy(box: Any) -> Optional[tuple]:
    if isinstance(box, dict):
        vals = [box.get("x0"), box.get("y0"), box.get("x1"), box.get("y1")]
    elif isinstance(box, (list, tuple)) and len(box) >= 4:
        vals = list(box[:4])
    else:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in vals)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
        return None
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None
    return (x0, y0, x1, y1)


def _box_id(box: Any, idx: int) -> int:
    """优先用 box["id"]，没有就按 1 起序号（与 page.json glyph id 一致）。"""
    if isinstance(box, dict):
        v = box.get("id")
        if isinstance(v, bool):
            v = None
        if isinstance(v, (int, float)) and math.isfinite(float(v)):
            return int(v)
        if isinstance(v, str) and v.strip().isdigit():
            return int(v.strip())
    return idx + 1


def crop_context(page_w: int, page_h: int, box: Sequence[float], *,
                 pad_y: float = CONTEXT_PAD_Y, pad_x: float = CONTEXT_PAD_X) -> tuple:
    """当前框 + 上下 0.9h / 左右 0.5w 的上下文窗（clamp 到页面内，x1/y1 开区间）。"""
    page_w, page_h = int(page_w), int(page_h)
    x0, y0, x1, y1 = (float(v) for v in box[:4])
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    w = max(1.0, x1 - x0)
    h = max(1.0, y1 - y0)
    cx0 = int(math.floor(x0 - pad_x * w))
    cy0 = int(math.floor(y0 - pad_y * h))
    cx1 = int(math.ceil(x1 + pad_x * w))
    cy1 = int(math.ceil(y1 + pad_y * h))
    cx0 = max(0, min(cx0, page_w - 1))
    cy0 = max(0, min(cy0, page_h - 1))
    cx1 = max(cx0 + 1, min(cx1, page_w))
    cy1 = max(cy0 + 1, min(cy1, page_h))
    return (cx0, cy0, cx1, cy1)


def norm_bbox(box: Sequence[float], crop: Sequence[int], *, norm_max: float = NORM_MAX) -> list:
    """原图坐标 -> 相对裁块的归一化坐标（0-1000，保留 1 位小数）。"""
    cx0, cy0, cx1, cy1 = (int(v) for v in crop[:4])
    cw = max(1, cx1 - cx0)
    ch = max(1, cy1 - cy0)
    x0, y0, x1, y1 = (float(v) for v in box[:4])
    return [
        round((x0 - cx0) / cw * norm_max, 1),
        round((y0 - cy0) / ch * norm_max, 1),
        round((x1 - cx0) / cw * norm_max, 1),
        round((y1 - cy0) / ch * norm_max, 1),
    ]


def denorm_bbox(nb: Sequence[float], crop: Sequence[int], *,
                norm_max: float = NORM_MAX) -> list:
    """归一化 0-1000（相对裁块）-> 原图像素坐标（int，x1/y1 开区间，不 clamp）。"""
    cx0, cy0, cx1, cy1 = (int(v) for v in crop[:4])
    cw = max(1, cx1 - cx0)
    ch = max(1, cy1 - cy0)
    x0 = cx0 + float(nb[0]) / norm_max * cw
    y0 = cy0 + float(nb[1]) / norm_max * ch
    x1 = cx0 + float(nb[2]) / norm_max * cw
    y1 = cy0 + float(nb[3]) / norm_max * ch
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    return [int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))]


def _clamp_box(box: Sequence[float], page_w: int, page_h: int) -> list:
    x0, y0, x1, y1 = (int(round(float(v))) for v in box[:4])
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    x0 = max(0, min(x0, page_w - 1))
    y0 = max(0, min(y0, page_h - 1))
    x1 = max(x0 + 1, min(x1, page_w))
    y1 = max(y0 + 1, min(y1, page_h))
    return [x0, y0, x1, y1]


def _area(b: Sequence[float]) -> float:
    return max(0.0, float(b[2]) - float(b[0])) * max(0.0, float(b[3]) - float(b[1]))


def iou(a: Sequence[float], b: Sequence[float]) -> float:
    ix0 = max(float(a[0]), float(b[0]))
    iy0 = max(float(a[1]), float(b[1]))
    ix1 = min(float(a[2]), float(b[2]))
    iy1 = min(float(a[3]), float(b[3]))
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    union = _area(a) + _area(b) - inter
    return inter / union if union > 0 else 0.0


def _overlap_frac(a: Sequence[float], b: Sequence[float]) -> float:
    ix0 = max(float(a[0]), float(b[0]))
    iy0 = max(float(a[1]), float(b[1]))
    ix1 = min(float(a[2]), float(b[2]))
    iy1 = min(float(a[3]), float(b[3]))
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    small = min(_area(a), _area(b))
    return inter / small if small > 0 else 0.0


def _union_bbox(boxes: Sequence[Sequence[float]]) -> list:
    return [min(float(b[0]) for b in boxes), min(float(b[1]) for b in boxes),
            max(float(b[2]) for b in boxes), max(float(b[3]) for b in boxes)]


# ---------------------------------------------------------------- JSON 容错解析
def _extract_balanced(text: str, start: int) -> Optional[str]:
    """从 text[start]=='{' 起找配对的右括号，跳过字符串内的括号/转义。"""
    if start < 0 or start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _strip_json_noise(text: str) -> str:
    s = text.strip()
    if s.startswith("\ufeff"):
        s = s[1:].strip()
    s = re.sub(r",\s*([}\]])", r"\1", s)      # 尾逗号
    s = s.replace("\u3000", " ")
    return s.strip()


def parse_model_json(text: Any) -> dict:
    """把模型回复解析成 dict：容忍前后废话、json 代码块包裹、尾逗号、单元素数组。"""
    if isinstance(text, dict):
        return text
    if isinstance(text, (list, tuple)) and text and isinstance(text[0], dict):
        return text[0]
    if not isinstance(text, str):
        raise ValueError("模型返回内容不是文本：%r" % (type(text),))
    raw = text.strip()
    if not raw:
        raise ValueError("模型返回空内容")
    cands = []
    for m in re.finditer(r"```(?:json|JSON)?\s*(.*?)```", raw, re.S):
        cands.append(m.group(1))
    for m in re.finditer(r"\{", raw):
        obj = _extract_balanced(raw, m.start())
        if obj:
            cands.append(obj)
    cands.append(raw)
    seen = set()
    for cand in cands:
        cand = _strip_json_noise(cand)
        if not cand or cand in seen:
            continue
        seen.add(cand)
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, (list, tuple)) and obj and isinstance(obj[0], dict):
            return obj[0]
    raise ValueError("模型未返回可解析的 JSON：" + _short(raw, 60))


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _clean_reason(value: Any, limit: int = REASON_MAX) -> str:
    if value is None:
        return ""
    s = re.sub(r"\s+", " ", str(value)).strip()
    return s[:limit]


def _coerce_norm_boxes(raw: Any) -> list:
    """模型给的 bboxes 统一成 [[x0,y0,x1,y1],...]（容错 dict / 字符串 / 乱序）。"""
    if raw is None:
        return []
    if isinstance(raw, dict):
        if all(k in raw for k in ("x0", "y0", "x1", "y1")):
            raw = [[raw["x0"], raw["y0"], raw["x1"], raw["y1"]]]
        else:
            return []
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for item in raw:
        vals = None
        if isinstance(item, dict):
            try:
                vals = [item["x0"], item["y0"], item["x1"], item["y1"]]
            except (KeyError, TypeError):
                vals = None
        elif isinstance(item, (list, tuple)) and len(item) >= 4:
            vals = list(item[:4])
        elif isinstance(item, str):
            parts = [p for p in re.split(r"[,，\s]+", item.strip()) if p]
            if len(parts) >= 4:
                vals = parts[:4]
        if vals is None:
            continue
        try:
            nums = [float(v) for v in vals]
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(n) for n in nums):
            continue
        x0, y0, x1, y1 = nums
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        if x1 - x0 < 1 or y1 - y0 < 1:
            continue
        out.append([x0, y0, x1, y1])
    return out


# ---------------------------------------------------------------- 校验
def validate_proposal(before: Sequence[float], action: str, boxes: Sequence[Sequence[float]],
                      page_w: int, page_h: int, *, iou_min: float = IOU_MIN,
                      size_lo: float = SIZE_LO, size_hi: float = SIZE_HI,
                      edge_tol: int = EDGE_TOL) -> tuple:
    """返回 (action, reason)。校验不过 -> ("keep", "退回:...")。

    规则（SPEC）：与 before 的 IoU > 0.2、尺寸比 0.4-2.5、框在图内；
    split 必须 2 个且互不重叠（相交面积 / 小框面积 <= 0.05）。
    """
    if action == "keep":
        return "keep", ""
    boxes = [[float(v) for v in b[:4]] for b in boxes]
    if action in ("tighten", "merge"):
        if len(boxes) != 1:
            return "keep", "退回:框数应为1"
    elif action == "split":
        if len(boxes) != 2:
            return "keep", "退回:拆分应为2框"
    else:
        return "keep", "退回:未知动作"
    bw = max(1.0, float(before[2]) - float(before[0]))
    bh = max(1.0, float(before[3]) - float(before[1]))
    for b in boxes:
        if b[2] - b[0] < 2 or b[3] - b[1] < 2:
            return "keep", "退回:框为空"
        if b[0] < -edge_tol or b[1] < -edge_tol or b[2] > page_w + edge_tol or b[3] > page_h + edge_tol:
            return "keep", "退回:超出页面"
        rw = (b[2] - b[0]) / bw
        rh = (b[3] - b[1]) / bh
        if not (size_lo <= rw <= size_hi) or not (size_lo <= rh <= size_hi):
            return "keep", "退回:尺寸比越界"
    if action == "split":
        if _overlap_frac(boxes[0], boxes[1]) > SPLIT_OVERLAP_MAX:
            return "keep", "退回:拆分框重叠"
        if iou(_union_bbox(boxes), before) <= iou_min:
            return "keep", "退回:与红框重叠不足"
    else:
        if iou(boxes[0], before) <= iou_min:
            return "keep", "退回:与红框重叠不足"
    return action, ""


# ---------------------------------------------------------------- 裁块 / prompt / 请求
def build_request_image(page_rgb: np.ndarray, box: Sequence[float], crop: Sequence[int], *,
                        target_h: int = TARGET_GLYPH_PX, max_side: int = MAX_IMAGE_SIDE,
                        quality: int = JPEG_QUALITY, line_px: int = BOX_LINE_PX) -> dict:
    """裁上下文 -> 放大到字高 >= target_h（单边 <= max_side）-> 画 3px 红框 -> JPEG q88。

    返回 {data_url, jpeg, width, height, scale, crop, box_in_image, glyph_px}。
    """
    page_h, page_w = page_rgb.shape[:2]
    cx0, cy0, cx1, cy1 = (int(v) for v in crop[:4])
    cx0 = max(0, min(cx0, page_w - 1))
    cy0 = max(0, min(cy0, page_h - 1))
    cx1 = max(cx0 + 1, min(cx1, page_w))
    cy1 = max(cy0 + 1, min(cy1, page_h))
    sub = page_rgb[cy0:cy1, cx0:cx1]
    im = Image.fromarray(np.ascontiguousarray(sub))

    x0, y0, x1, y1 = (float(v) for v in box[:4])
    gh = max(1.0, y1 - y0)
    scale = float(target_h) / gh if gh < float(target_h) else 1.0
    cw, ch = im.size
    if max(cw, ch) * scale > max_side:
        scale = float(max_side) / max(cw, ch)
    scale = max(0.05, scale)
    if abs(scale - 1.0) > 1e-3:
        im = im.resize((max(1, int(round(cw * scale))), max(1, int(round(ch * scale)))),
                       Image.LANCZOS)

    iw, ih = im.size
    draw = ImageDraw.Draw(im)
    rx0 = (x0 - cx0) * scale
    ry0 = (y0 - cy0) * scale
    rx1 = (x1 - cx0) * scale
    ry1 = (y1 - cy0) * scale
    rx0 = max(0.0, min(rx0, iw - 1.0))
    ry0 = max(0.0, min(ry0, ih - 1.0))
    rx1 = max(0.0, min(rx1, iw - 1.0))
    ry1 = max(0.0, min(ry1, ih - 1.0))
    if rx1 - rx0 >= 2 and ry1 - ry0 >= 2:
        draw.rectangle([rx0, ry0, rx1 - 1, ry1 - 1], outline=BOX_LINE_RGB, width=int(line_px))

    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=int(quality), optimize=True)
    jpeg = buf.getvalue()
    return {
        "data_url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii"),
        "jpeg": jpeg,
        "width": iw,
        "height": ih,
        "scale": scale,
        "crop": (cx0, cy0, cx1, cy1),
        "box_in_image": [rx0, ry0, rx1, ry1],
        "glyph_px": gh * scale,
    }


def build_prompt(before: Sequence[float], crop: Sequence[int], image_w: int, image_h: int,
                 page_w: int, page_h: int) -> str:
    """构造要求模型只回 JSON 的 prompt（附当前框归一化坐标作为参照）。"""
    cx0, cy0, cx1, cy1 = (int(v) for v in crop[:4])
    nb = norm_bbox(before, crop)
    lines = [
        "任务：复核彝文古籍扫描页上的一个单字框。",
        "图片是一块放大的裁块。图中红色矩形是「当前框」，它可能偏移、过松、过紧、含两个字，或只含半个字。",
        "正确目标：框应恰好紧贴这个字的墨迹外接矩形，四周留白约 3-8 像素（按图片当前比例）。",
        "",
        "只输出一个 JSON 对象，不要代码块标记，不要任何解释文字：",
        '{"action":"tighten|split|merge|keep","bboxes":[[x0,y0,x1,y1],...],"confidence":0.0,"reason":"不超过20字的理由"}',
        "",
        "action 取值：",
        "  tighten = 当前框没贴紧字（过松/过紧/偏移），给出 1 个修正框；",
        "  split   = 当前框里有两个字，给出 2 个互不重叠的框，各框住一个字；",
        "  merge   = 当前框只框住某个字的一部分，给出 1 个补全后的框；",
        "  keep    = 当前框已经正确，bboxes 给出与当前框相同的 1 个框；",
        "坐标规则：相对这张裁块的归一化整数，0-1000；x 向右，y 向下；"
        "(0,0) 在裁块左上角，(1000,1000) 在右下角。bboxes 只能是 1 个或 2 个框，不能超出 0-1000。",
        "",
        "裁块信息：原图 %d x %d 像素；裁块显示为 %d x %d 像素；"
        "裁块在原图中的范围 x=[%d,%d), y=[%d,%d)（x1/y1 开区间）。"
        % (int(page_w), int(page_h), int(image_w), int(image_h), cx0, cx1, cy0, cy1),
        "当前框（原图像素）：x0=%d, y0=%d, x1=%d, y1=%d；归一化坐标 [[%s, %s, %s, %s]]。"
        % (round(float(before[0])), round(float(before[1])), round(float(before[2])),
           round(float(before[3])), nb[0], nb[1], nb[2], nb[3]),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- 模型调用
def _make_client(cfg: AIConfig):
    from openai import OpenAI
    return OpenAI(
        api_key=cfg.api_key,
        base_url=str(cfg.base_url).rstrip("/"),
        timeout=float(cfg.timeout),
        max_retries=0,          # 重试由本模块统一控制（可测、可控）
    )


class _EmptyContent(ValueError):
    """模型没给正文（最常见：思考模式把 max_tokens 用完了）。"""

    def __init__(self, message: str, finish_reason: str = "", usage: Any = None,
                 raw: str = ""):
        super().__init__(message)
        self.finish_reason = finish_reason
        self.usage = usage
        self.raw = raw


def _flatten_content(content: Any) -> str:
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text") or "")
            elif isinstance(p, str):
                parts.append(p)
        return "".join(parts)
    return content if isinstance(content, str) else ""


def _message_text(resp: Any) -> str:
    try:
        choice = resp.choices[0]
        msg = choice.message
        content = msg.content
        reasoning = getattr(msg, "reasoning_content", None)
        finish = getattr(choice, "finish_reason", "") or ""
        usage = getattr(resp, "usage", None)
    except (AttributeError, IndexError, TypeError) as exc:
        raise RuntimeError("模型响应结构异常：%s" % _short(exc, 60)) from exc

    text = _flatten_content(content).strip()
    if text:
        return text

    # 思考模式下正文为空：思维链里往往已经写出了 JSON，先救回来
    cot = _flatten_content(reasoning).strip()
    if cot:
        LOG.info("正文为空，改用 reasoning_content（%d 字, finish=%s）", len(cot), finish)
        return cot

    reason = "finish=%s" % (finish or "?")
    try:
        if usage is not None:
            reason += ", tokens=%s/%s" % (getattr(usage, "completion_tokens", "?"),
                                          getattr(usage, "total_tokens", "?"))
    except Exception:
        pass
    raise _EmptyContent("模型返回空内容（%s）" % reason, finish_reason=finish,
                        usage=usage, raw=_short(repr(resp), 200))


def _extra_body(cfg: AIConfig) -> dict:
    """DeepSeek 的思考模式开关走 extra_body（OpenAI SDK 的扩展字段）。"""
    thinking = str(getattr(cfg, "thinking", "disabled") or "disabled").lower()
    if thinking in ("enabled", "on", "true", "1"):
        return {"thinking": {"type": "enabled"}}
    return {"thinking": {"type": "disabled"}}


def _chat_once(client: Any, cfg: AIConfig, prompt: str, data_url: str) -> str:
    """One vision request; retries once with a bigger budget if the answer was cut."""
    max_tokens = max(64, int(getattr(cfg, "max_tokens", 512) or 512))
    last_err: Optional[BaseException] = None
    for attempt in range(2):
        resp = client.chat.completions.create(
            model=cfg.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url, "detail": cfg.detail}},
                ]},
            ],
            temperature=0.0,
            max_tokens=max_tokens,
            extra_body=_extra_body(cfg),
            stream=False,
        )
        try:
            return _message_text(resp)
        except _EmptyContent as exc:
            last_err = exc
            finish = str(getattr(exc, "finish_reason", "") or "")
            LOG.warning("模型返回空内容（第 %d 次, finish=%s, max_tokens=%d）", attempt + 1, finish, max_tokens)
            if attempt == 0 and (finish == "length" or max_tokens < 2048):
                max_tokens = min(4096, max_tokens * 4)
                continue
            raise
    if last_err is not None:
        raise last_err
    raise RuntimeError("模型未返回任何内容")


_RETRY_STATUS = (408, 425, 429, 500, 502, 503, 504)


def _status_code(exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(exc, "response", None)
    v = getattr(resp, "status_code", None)
    if isinstance(v, int):
        return v
    return None


def _is_retryable(exc: BaseException) -> bool:
    status = _status_code(exc)
    if status is not None:
        return status in _RETRY_STATUS
    name = type(exc).__name__
    if name in ("RateLimitError", "APIConnectionError", "APITimeoutError", "InternalServerError"):
        return True
    return ("Timeout" in name) or ("Connection" in name)


def _sleep_cancelable(delay: float, cancel_evt: Optional[threading.Event]) -> None:
    end = time.monotonic() + max(0.0, float(delay))
    while True:
        if cancel_evt is not None and cancel_evt.is_set():
            raise _CancelSignal()
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(0.05, left))


def _request_with_retries(fn: Callable[[], Any], cfg: AIConfig,
                          cancel_evt: Optional[threading.Event] = None) -> Any:
    attempts = max(0, int(cfg.retries)) + 1
    last: Optional[BaseException] = None
    for i in range(attempts):
        if cancel_evt is not None and cancel_evt.is_set():
            raise _CancelSignal()
        try:
            return fn()
        except Exception as exc:       # openai 异常类型跨版本不稳定：按状态码/名字判定
            last = exc
            LOG.warning("AI 请求失败（第 %d/%d 次）：%s", i + 1, attempts, _short(exc, 160))
            if i + 1 >= attempts or not _is_retryable(exc):
                raise
            _sleep_cancelable(min(float(cfg.retry_backoff) * (2 ** i), 8.0), cancel_evt)
    if last is not None:
        raise last
    raise RuntimeError("AI 请求未发出")


# ---------------------------------------------------------------- Proposal 组装
def _box_dict(box: Sequence[float]) -> dict:
    x0, y0, x1, y1 = (int(round(float(v))) for v in box[:4])
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1}


def _proposal(box_id: int, action: str, before: Sequence[float], after: Sequence[Sequence[float]],
              confidence: float, reason: str) -> dict:
    return {
        "box_id": int(box_id),
        "action": action,
        "before": _box_dict(before),
        "after": [_box_dict(b) for b in after],
        "confidence": round(_clamp01(confidence), 3),
        "reason": _clean_reason(reason),
        "status": "pending",
    }


def _keep_proposal(box_id: int, before: Sequence[float], reason: str,
                   confidence: float = 0.0) -> dict:
    return _proposal(box_id, "keep", before, [], confidence, reason)


_DEFAULT_REASON = {
    "tighten": "收紧字框",
    "split": "拆成两个字",
    "merge": "补全字框",
    "keep": "保留原框",
}


def _proposal_from_model(data: dict, before: Sequence[float], crop: Sequence[int],
                         page_w: int, page_h: int, box_id: int) -> dict:
    """模型 JSON -> Proposal（含全部校验；不过就退回 keep 并写原因）。"""
    action_raw = data.get("action")
    action = str(action_raw).strip().lower() if action_raw is not None else ""
    reason = _clean_reason(data.get("reason"))
    conf = _clamp01(_to_float(data.get("confidence"), 0.5))
    if action not in ACTIONS:
        return _keep_proposal(box_id, before, "未知动作:" + (reason or _short(action_raw, 10)), conf)
    if action == "keep":
        return _proposal(box_id, "keep", before, [], conf, reason or _DEFAULT_REASON["keep"])
    raw = data.get("bboxes")
    if raw is None:
        raw = data.get("boxes")
    norm_boxes = _coerce_norm_boxes(raw)
    if not norm_boxes:
        return _keep_proposal(box_id, before, "模型未给出坐标", conf)
    boxes = [denorm_bbox(nb, crop) for nb in norm_boxes]
    checked, why = validate_proposal(before, action, boxes, page_w, page_h)
    if checked != action:
        return _keep_proposal(box_id, before, why, conf)
    boxes = [_clamp_box(b, page_w, page_h) for b in boxes]
    return _proposal(box_id, action, before, boxes, conf, reason or _DEFAULT_REASON[action])


def _mock_data(before: Sequence[float], crop: Sequence[int]) -> dict:
    """确定性 mock：四边各收 2%，归一化后交给正常校验/换算管线。"""
    w = float(before[2]) - float(before[0])
    h = float(before[3]) - float(before[1])
    nb = [before[0] + 0.02 * w, before[1] + 0.02 * h,
          before[2] - 0.02 * w, before[3] - 0.02 * h]
    return {
        "action": "tighten",
        "bboxes": [norm_bbox(nb, crop)],
        "confidence": 0.9,
        "reason": "[mock] 收紧 2%",
    }


def _live_proposal(client: Any, cfg: AIConfig, page_rgb: np.ndarray, before: Sequence[float],
                   box_id: int, cancel_evt: threading.Event) -> dict:
    page_h, page_w = page_rgb.shape[:2]
    crop = crop_context(page_w, page_h, before)
    payload = build_request_image(
        page_rgb, before, crop,
        target_h=int(cfg.target_glyph_px), max_side=int(cfg.max_image_side),
        quality=int(cfg.jpeg_quality),
    )
    prompt = build_prompt(before, crop, payload["width"], payload["height"], page_w, page_h)
    text = _request_with_retries(
        lambda: _chat_once(client, cfg, prompt, payload["data_url"]), cfg, cancel_evt)
    data = parse_model_json(text)
    return _proposal_from_model(data, before, crop, page_w, page_h, box_id)


# ---------------------------------------------------------------- 选框
def select_target_indices(boxes: Sequence[Any], ids: Optional[Sequence[int]] = None,
                          mode: str = "all") -> list:
    """挑出要复核的 box 下标。

    ids  : 优先按 box["id"] 匹配，其次 1 起序号，再 0 起下标（前者落空才用后者）。
           给了 ids 就完全按 ids 来（app-api 已用 pipeline.suspect_ids 解析过 suspect）。
    mode : 仅在 ids 为空时生效："suspect" 只留 flags 非空 / review==2 / suspect 的框；
           其他值（all/quick）全留。
    """
    idxs = list(range(len(boxes)))
    explicit = bool(ids)
    if explicit:
        want = set()
        for v in ids:
            try:
                want.add(int(v))
            except (TypeError, ValueError):
                continue
        hit = [i for i in idxs if (_box_id(boxes[i], i) in want)]
        if not hit:
            hit = [i for i in idxs if (i + 1) in want]
        if not hit and all(0 <= v < len(boxes) for v in want):
            hit = [i for i in idxs if i in want]
        idxs = hit
    if not explicit and str(mode or "all").lower() == "suspect":
        def _suspect(box: Any) -> bool:
            if not isinstance(box, dict):
                return False
            flags = box.get("flags") or []
            if isinstance(flags, str):
                flags = [flags]
            if flags:
                return True
            if box.get("suspect"):
                return True
            try:
                return int(box.get("review") or 0) == 2
            except (TypeError, ValueError):
                return False
        idxs = [i for i in idxs if _suspect(boxes[i])]
    return idxs


# ---------------------------------------------------------------- 主入口
def refine_boxes(page_rgb: Any, boxes: Sequence[Any], *, ids: Optional[Sequence[int]] = None,
                 mode: str = "all", cfg: Optional[AIConfig] = None,
                 progress_cb: Optional[Callable] = None,
                 cancel_cb: Optional[Callable] = None) -> list:
    """对 boxes 逐个向视觉模型要修正建议，返回 Proposal 列表（输入顺序）。

    page_rgb : HxWx3 uint8 RGB 整页图（BGR 会导致纸色偏蓝，调用方注意）
    boxes    : [{"id"?,"x0","y0","x1","y1",...}, ...]（原图像素，x1/y1 开区间）
    ids      : None=全部；否则按 box id（回退 1 起序号 / 0 起下标）筛
    mode     : "all" | "suspect"（有 flags / review==2 / suspect 才复核）
    cfg      : AIConfig；None 用默认（无 key -> mock）
    progress : progress_cb(done, total)，开始时报 (0,total)，之后每框一次
    cancel_cb: 每框前调用，返回 True 即取消：停止派发，返回已完成部分（不抛异常）
    """
    cfg = cfg if cfg is not None else AIConfig()
    page = _as_rgb(page_rgb)
    page_h, page_w = page.shape[:2]
    box_list = list(boxes or [])
    if not box_list:
        return []
    targets = select_target_indices(box_list, ids, mode)
    total = len(targets)
    if total == 0:
        return []

    mock = bool(cfg.mock) or not str(cfg.api_key or "").strip()
    client = None if mock else _make_client(cfg)
    cancel_evt = threading.Event()
    results: list = [None] * total
    state = {"done": 0}
    lock = threading.Lock()

    def _gone() -> bool:
        if cancel_evt.is_set():
            return True
        if cancel_cb is None:
            return False
        try:
            if cancel_cb():
                cancel_evt.set()
                return True
        except Exception as exc:     # 取消回调自己抛错不应中断复核
            LOG.warning("cancel_cb 抛错（已忽略）：%s", _short(exc, 120))
        return False

    def _report(done: int) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(done, total)
        except TypeError:
            try:
                progress_cb(done)
            except Exception as exc:
                LOG.warning("progress_cb 抛错（已忽略）：%s", _short(exc, 120))
        except Exception as exc:
            LOG.warning("progress_cb 抛错（已忽略）：%s", _short(exc, 120))

    _report(0)

    def _work(i: int) -> Optional[dict]:
        idx = targets[i]
        box = box_list[idx]
        box_id = _box_id(box, idx)
        bb = _box_xyxy(box)
        if bb is None:
            return _keep_proposal(box_id, [0, 0, 0, 0], "[无效] 坐标缺失")
        if _gone():
            return None
        try:
            if mock:
                if float(cfg.mock_delay) > 0:
                    time.sleep(float(cfg.mock_delay))
                if _gone():
                    return None
                crop = crop_context(page_w, page_h, bb)
                prop = _proposal_from_model(_mock_data(bb, crop), bb, crop,
                                            page_w, page_h, box_id)
            else:
                prop = _live_proposal(client, cfg, page, bb, box_id, cancel_evt)
        except _CancelSignal:
            return None
        except Exception as exc:
            LOG.warning("AI 复核失败 box_id=%s：%s", box_id, _short(exc, 160))
            prop = _keep_proposal(box_id, bb, "错误:" + _short(exc, 16))
        if cancel_evt.is_set():
            return prop
        with lock:
            state["done"] += 1
            done = state["done"]
        _report(done)
        return prop

    executor = ThreadPoolExecutor(max_workers=max(1, int(cfg.max_workers)),
                                  thread_name_prefix="ai-refine")
    futures: dict = {}
    canceled = False

    def _drain(done_futs) -> None:
        for fut in done_futs:
            i = futures.get(fut)
            if i is None:
                continue
            if fut.cancelled():
                continue
            exc = fut.exception()
            if exc is not None:
                LOG.warning("AI 复核 worker 异常：%s", _short(exc, 160))
                continue
            res = fut.result()
            if res is not None:
                results[i] = res

    try:
        for i in range(total):
            if _gone():
                canceled = True
                break
            futures[executor.submit(_work, i)] = i
        pending = set(futures)
        while pending:
            if _gone():
                canceled = True
                break
            done_futs, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
            _drain(done_futs)
    finally:
        executor.shutdown(wait=not canceled, cancel_futures=canceled)

    if canceled:
        _drain([f for f in futures if f.done() and not f.cancelled()])
        LOG.info("AI 复核已取消：完成 %d/%d", int(state["done"]), total)
        return [r for r in results if r is not None]

    _report(total)
    return [r for r in results if r is not None]


# ---------------------------------------------------------------- 连通性自测
def _friendly_error(exc: BaseException, cfg: AIConfig) -> str:
    status = _status_code(exc)
    name = type(exc).__name__
    if status == 401:
        return "API Key 无效或已过期（401）"
    if status == 403:
        return "该 Key 无权访问此模型（403）"
    if status == 404:
        return "base_url 或 model 不存在（404），请检查 %s / %s" % (cfg.base_url, cfg.model)
    if status == 429:
        return "请求被限流（429），稍后重试"
    if status is not None and status >= 500:
        return "服务端错误（%d），稍后重试" % status
    if "Timeout" in name:
        return "请求超时（%.0fs），检查网络或调大 timeout" % float(cfg.timeout)
    if "Connection" in name:
        return "无法连接 base_url（%s），检查网络/代理/地址" % cfg.base_url
    return _short(exc, 100)


def _tiny_test_image() -> str:
    """A 200x140 JPEG with a red rectangle - verifies that the model really sees images."""
    im = Image.new("RGB", (200, 140), (238, 236, 230))
    d = ImageDraw.Draw(im)
    d.rectangle([50, 30, 150, 110], outline=(200, 0, 0), width=3)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def test_connection(cfg: Optional[AIConfig] = None) -> dict:
    """一次带图的真实请求验证 key / base_url / model（不含 key 明文）。

    特意发一张图，因为"文本能通"不代表"视觉能用"；也顺便暴露
    "思考模式把 max_tokens 吃光导致正文为空"这类问题。
    """
    cfg = cfg if cfg is not None else AIConfig()
    if cfg.mock:
        return {"ok": True, "message": "[mock] 未发网络请求：%s / %s" % (cfg.base_url, cfg.model)}
    if not str(cfg.api_key or "").strip():
        return {"ok": False, "message": "未配置 API Key（可在设置里填，或设环境变量 DEEPSEEK_API_KEY）"}
    try:
        client = _make_client(cfg)
        t0 = time.time()
        resp = client.chat.completions.create(
            model=cfg.model,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "这张图里有一个红色矩形框。请只回 JSON：{\"ok\":true}"},
                {"type": "image_url", "image_url": {"url": _tiny_test_image(), "detail": "low"}},
            ]}],
            temperature=0.0,
            max_tokens=64,
            extra_body=_extra_body(cfg),
            stream=False,
        )
        dt = time.time() - t0
        try:
            text = _message_text(resp)
        except _EmptyContent as exc:
            return {"ok": False, "message":
                    "视觉请求通了但正文为空（%s，%.1fs）。常见原因：思考模式吃光 max_tokens"
                    "（本程序默认已关闭思考）、或该模型不接受图片输入。" % (exc, dt)}
        finish = ""
        try:
            finish = resp.choices[0].finish_reason or ""
        except Exception:
            pass
        return {"ok": True, "message": "视觉接口可用：%s · %s · %.1fs · finish=%s · 回复「%s」"
                % (cfg.base_url, cfg.model, dt, finish or "?", _short(text, 40))}
    except Exception as exc:
        LOG.warning("test_connection 失败：%s", _short(exc, 200))
        return {"ok": False, "message": _friendly_error(exc, cfg)}


# ---------------------------------------------------------------- CLI
def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="app.ai_refine", description="DeepSeek 视觉复核（自测入口）")
    ap.add_argument("--selftest", action="store_true",
                    help="离线自测：mock + 归一化换算 + 校验 + 本地假 OpenAI 服务")
    ap.add_argument("--fake-server", action="store_true",
                    help="起本地假 OpenAI 兼容服务，打印服务端记录的请求体证据（真带图）")
    args = ap.parse_args(argv)
    try:                              # Windows 控制台默认 cp936，中文/日志统一走 UTF-8
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if args.selftest:
        from .ai_selftest import run_selftest
        return run_selftest()
    if args.fake_server:
        from .ai_selftest import run_fake_server_evidence
        return run_fake_server_evidence()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
