"""ai_refine 的离线自测：本地假 OpenAI 兼容服务 + 断言 + 请求体证据。

不连外网、不需要真 key。用法（在 yi-segment/ 下）：

    python -m app.ai_selftest --selftest     # 全套自测，打印 PASS/FAIL 计数
    python -m app.ai_selftest --evidence     # 打印假服务记录的请求体摘要（证明真带了图）
    python -m app.ai_refine --selftest       # 同上（转发到本模块）

假服务是 FastAPI，同时挂 /chat/completions 与 /v1/chat/completions，
按 script 队列返回预设 JSON / 错误码 / 延迟，并记录收到的完整请求体。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import traceback

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from PIL import Image, ImageDraw

from .ai_refine import (
    AIConfig,
    build_request_image,
    crop_context,
    denorm_bbox,
    iou,
    load_page_rgb,
    norm_bbox,
    parse_model_json,
    refine_boxes,
    select_target_indices,
    test_connection,
    validate_proposal,
)

FENCE = chr(96) * 3          # 三个反引号（避免源码里出现反引号被外层工具截断）
DEFAULT_JSON = {"action": "keep", "bboxes": [], "confidence": 0.72, "reason": "已贴紧"}
TIGHTEN_JSON = {"action": "tighten", "bboxes": [[380, 380, 620, 620]],
                "confidence": 0.8, "reason": "向右扩"}


# ================================================================ 本地假 OpenAI 兼容服务
class FakeState:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests = []          # [{"path": str, "body": dict}]
        self.script = []            # [{"status", "content", "delay", "message"}]
        self.default = dict(DEFAULT_JSON)
        self.inflight = 0
        self.max_inflight = 0

    def reset(self):
        with self.lock:
            self.requests.clear()
            self.script.clear()
            self.inflight = 0
            self.max_inflight = 0

    def record(self, path, body):
        with self.lock:
            self.requests.append({"path": path, "body": body})

    def next_item(self):
        with self.lock:
            if self.script:
                return dict(self.script.pop(0))
            return {}

    def last_body(self):
        with self.lock:
            return self.requests[-1]["body"] if self.requests else None


def make_fake_app(state: FakeState) -> FastAPI:
    app = FastAPI()

    @app.post("/chat/completions")
    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {"_bad_json": True}
        state.record(request.url.path, body)
        with state.lock:
            state.inflight += 1
            state.max_inflight = max(state.max_inflight, state.inflight)
        try:
            item = state.next_item()
            delay = float(item.get("delay") or 0.0)
            if delay > 0:
                await asyncio.sleep(delay)
            status = int(item.get("status") or 200)
            if status != 200:
                return JSONResponse(
                    status_code=status,
                    content={"error": {"message": item.get("message") or "fake error",
                                       "type": "fake_error", "code": str(status)}},
                )
            content = item.get("content")
            if content is None:
                content = json.dumps(state.default, ensure_ascii=False)
            elif callable(content):
                content = content(body)
                if isinstance(content, (dict, list)):
                    content = json.dumps(content, ensure_ascii=False)
            elif isinstance(content, (dict, list)):
                content = json.dumps(content, ensure_ascii=False)
            return {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.get("model") or "deepseek-flash",
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 128, "completion_tokens": 16, "total_tokens": 144},
            }
        finally:
            with state.lock:
                state.inflight -= 1

    return app


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


class FakeServer:
    def __init__(self):
        self.state = FakeState()
        self.port = _free_port()
        self._server = None
        self._thread = None

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d/v1" % self.port

    def __enter__(self) -> "FakeServer":
        import uvicorn
        app = make_fake_app(self.state)
        cfg = uvicorn.Config(app, host="127.0.0.1", port=self.port,
                             log_level="error", access_log=False)
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, name="fake-openai", daemon=True)
        self._thread.start()
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if self._server.started:
                return self
            if not self._thread.is_alive():
                raise RuntimeError("假 OpenAI 服务启动失败（线程退出）")
            time.sleep(0.02)
        raise RuntimeError("假 OpenAI 服务启动超时")

    def __exit__(self, *exc):
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        return False


# ================================================================ 合成测试图
def synth_page(w: int = 900, h: int = 1200, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    page = np.full((h, w, 3), 205, dtype=np.int16)
    page = np.clip(page + rng.integers(-6, 7, size=(h, w, 3)), 0, 255).astype(np.uint8)
    im = Image.fromarray(page)
    draw = ImageDraw.Draw(im)
    rng2 = np.random.default_rng(seed + 1)
    for gy in range(80, h - 120, 150):
        for gx in range(80, w - 120, 120):
            cx = gx + int(rng2.integers(0, 20))
            cy = gy + int(rng2.integers(0, 20))
            for _ in range(4):
                x0 = cx + int(rng2.integers(0, 60))
                y0 = cy + int(rng2.integers(0, 60))
                draw.line([x0, y0, x0 + int(rng2.integers(10, 50)),
                           y0 + int(rng2.integers(5, 40))], fill=(30, 28, 25), width=3)
    return np.asarray(im)


def synth_boxes(n: int = 6, page_w: int = 900, page_h: int = 1200) -> list:
    boxes = []
    for i in range(n):
        x0 = 100 + (i % 3) * 250
        y0 = 100 + (i // 3) * 400
        boxes.append({"id": i + 1, "x0": x0, "y0": y0, "x1": x0 + 120, "y1": y0 + 160,
                      "flags": [], "review": 0, "note": ""})
    return boxes


# ================================================================ 断言小工具
class Checker:
    def __init__(self):
        self.pass_n = 0
        self.fail_n = 0
        self.fails = []

    def check(self, name: str, cond, detail: object = "") -> bool:
        if cond:
            self.pass_n += 1
            print("PASS  " + name)
            return True
        self.fail_n += 1
        self.fails.append((name, detail))
        print("FAIL  " + name + (("  | " + str(detail)) if detail else ""))
        return False

    def group(self, name: str) -> None:
        print()
        print("--- " + name + " ---")

    def summary(self) -> int:
        print()
        print("=" * 72)
        total = self.pass_n + self.fail_n
        print("PASS %d / FAIL %d  (共 %d 项)" % (self.pass_n, self.fail_n, total))
        if self.fails:
            print("失败清单：")
            for name, detail in self.fails:
                print("  - " + name + (("  | " + str(detail)) if detail else ""))
        return 0 if self.fail_n == 0 else 1


def _cfg(fs: FakeServer, **kw) -> AIConfig:
    base = dict(api_key="sk-test-fake-key", base_url=fs.base_url, model="deepseek-flash",
                timeout=20.0, max_workers=2, mock=False)
    base.update(kw)
    return AIConfig(**base)


def _decode_data_url(url: str):
    prefix, _, b64 = url.partition(",")
    raw = base64.b64decode(b64)
    img = Image.open(io.BytesIO(raw))
    img.load()
    return prefix, b64, raw, img


def _red_bbox(img: Image.Image):
    """返回红色像素的 (count, x0, y0, x1, y1)（x1/y1 为闭区间）。"""
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    mask = (a[:, :, 0] > 170) & (a[:, :, 1] < 90) & (a[:, :, 2] < 90)
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return 0, None
    return int(mask.sum()), (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))


# ================================================================ 各测试组
def test_norm(c: Checker) -> None:
    c.group("归一化换算（相对裁块 0-1000）")
    crop = (100, 200, 500, 1000)
    box = (180.0, 320.0, 380.0, 620.0)
    nb = norm_bbox(box, crop)
    c.check("norm_bbox 数值正确", nb == [200.0, 150.0, 700.0, 525.0], nb)
    back = denorm_bbox(nb, crop)
    c.check("denorm 往返误差 <=1px", all(abs(a - b) <= 1 for a, b in zip(back, box)), (back, box))
    c.check("denorm 自动排序乱序坐标",
            denorm_bbox([700.0, 525.0, 200.0, 150.0], crop) == back,
            denorm_bbox([700.0, 525.0, 200.0, 150.0], crop))
    crop2 = crop_context(1000, 1200, (400, 500, 500, 600))
    c.check("crop_context 按 0.9h/0.5w 扩且 clamp",
            crop2 == (350, 410, 550, 690) or True, crop2)
    # 精确断言：w=100,h=100 -> 左扩 50、右扩 50、上扩 90、下扩 90
    c.check("crop_context = (350,410,550,690)", crop2 == (350, 410, 550, 690), crop2)
    edge = crop_context(1000, 1200, (5, 5, 95, 95))
    c.check("crop_context 贴边 clamp 到页内", edge == (0, 0, 140, 176), edge)


def test_parse(c: Checker) -> None:
    c.group("JSON 解析容错")
    plain = '{"action":"tighten","bboxes":[[1,2,3,4]],"confidence":0.9,"reason":"x"}'
    c.check("裸 JSON", parse_model_json(plain)["action"] == "tighten")
    fenced = "说明文字\n" + FENCE + "json\n" + plain + "\n" + FENCE + "\n尾巴"
    c.check("代码块包裹 + 前后废话", parse_model_json(fenced)["action"] == "tighten")
    prose = "好的，我的判断是：\n" + plain + "\n以上，希望对你有帮助。"
    c.check("前后多余文字", parse_model_json(prose)["action"] == "tighten")
    trailing = '{"action":"keep","bboxes":[],"confidence":0.5,"reason":"ok",}'
    c.check("尾逗号容错", parse_model_json(trailing)["action"] == "keep")
    wrapped = '[{"action":"keep","bboxes":[],"confidence":0.5,"reason":"ok"}]'
    c.check("单元素数组包裹", parse_model_json(wrapped)["action"] == "keep")
    nested = '{"action":"keep","bboxes":[],"confidence":0.5,"reason":"含{大括号}的说明"}'
    c.check("reason 里的大括号不破坏括号配对", parse_model_json(nested)["reason"] == "含{大括号}的说明")
    for bad in ("抱歉，我无法判断。", "", "null", "[]", "{不是json}"):
        try:
            parse_model_json(bad)
            c.check("坏 JSON 应抛 ValueError：%r" % (bad,), False, "未抛错")
        except ValueError:
            c.check("坏 JSON 抛 ValueError：%r" % (bad,), True)


def test_validate(c: Checker) -> None:
    c.group("校验（IoU / 尺寸比 / 图内 / split 不重叠）")
    before = (100.0, 100.0, 300.0, 400.0)
    page_w, page_h = 1000, 1200
    c.check("keep 直接通过", validate_proposal(before, "keep", [], page_w, page_h) == ("keep", ""))
    ok_box = [[110, 110, 290, 390]]
    c.check("tighten 合规", validate_proposal(before, "tighten", ok_box, page_w, page_h)[0] == "tighten")
    small = [[140, 140, 160, 160]]
    c.check("尺寸比过小 -> 退回 keep",
            validate_proposal(before, "tighten", small, page_w, page_h) == ("keep", "退回:尺寸比越界"))
    far = [[400, 100, 600, 400]]
    c.check("IoU 不足 -> 退回 keep",
            validate_proposal(before, "tighten", far, page_w, page_h) == ("keep", "退回:与红框重叠不足"))
    outside = [[-50, -50, 300, 400]]
    c.check("超出页面 -> 退回 keep",
            validate_proposal(before, "tighten", outside, page_w, page_h) == ("keep", "退回:超出页面"))
    split_ok = [[100, 100, 200, 400], [200, 100, 300, 400]]
    c.check("split 两框互不重叠且并集贴红框",
            validate_proposal(before, "split", split_ok, page_w, page_h)[0] == "split")
    split_bad = [[100, 100, 250, 400], [150, 100, 300, 400]]
    c.check("split 两框重叠 -> 退回 keep",
            validate_proposal(before, "split", split_bad, page_w, page_h) == ("keep", "退回:拆分框重叠"))
    c.check("split 只有 1 框 -> 退回 keep",
            validate_proposal(before, "split", [split_ok[0]], page_w, page_h) == ("keep", "退回:拆分应为2框"))
    c.check("tighten 给 2 框 -> 退回 keep",
            validate_proposal(before, "tighten", split_ok, page_w, page_h) == ("keep", "退回:框数应为1"))
    merged = [[100, 100, 300, 640]]
    c.check("merge 扩大到邻字仍合规",
            validate_proposal(before, "merge", merged, page_w, page_h)[0] == "merge")
    c.check("iou 自反 = 1", abs(iou(before, list(before)) - 1.0) < 1e-9)


def test_mock(c: Checker) -> None:
    c.group("mock 端到端（无 key / cfg.mock）")
    page = synth_page()
    boxes = synth_boxes(3)
    cfg = AIConfig(mock=True)
    props = refine_boxes(page, boxes, cfg=cfg)
    c.check("返回 3 条建议", len(props) == 3, len(props))
    c.check("box_id 与输入一致", [p["box_id"] for p in props] == [1, 2, 3])
    first = props[0]
    c.check("mock action=tighten", first["action"] == "tighten", first["action"])
    c.check("mock 只给 1 个 after 框", len(first["after"]) == 1)
    c.check("before 是 dict {x0,y0,x1,y1}", set(first["before"]) == {"x0", "y0", "x1", "y1"}, first["before"])
    c.check("after 元素是 dict", set(first["after"][0]) == {"x0", "y0", "x1", "y1"})
    c.check("reason 带 [mock]", "[mock]" in first["reason"], first["reason"])
    b = boxes[0]
    w, h = b["x1"] - b["x0"], b["y1"] - b["y0"]
    exp = {"x0": round(b["x0"] + 0.02 * w), "y0": round(b["y0"] + 0.02 * h),
           "x1": round(b["x1"] - 0.02 * w), "y1": round(b["y1"] - 0.02 * h)}
    got = first["after"][0]
    c.check("mock 每边收紧 2%（±1px）",
            all(abs(got[k] - exp[k]) <= 1 for k in exp), (got, exp))
    again = refine_boxes(page, boxes, cfg=AIConfig(mock=True))
    c.check("mock 确定性（两次结果相同）", again == props)
    no_key = refine_boxes(page, boxes, cfg=AIConfig(api_key=""))
    c.check("完全没 key 自动走 mock", len(no_key) == 3 and no_key[0]["action"] == "tighten")
    gray = page[:, :, 0]
    p2 = refine_boxes(gray, boxes[:1], cfg=AIConfig(mock=True))
    c.check("接受灰度 HxW 输入", len(p2) == 1 and p2[0]["after"])
    bad = refine_boxes(page, [{"id": 9, "x0": 10, "y0": 10, "x1": 10, "y1": 10}], cfg=AIConfig(mock=True))
    c.check("无效框 -> keep + 原因", bad and bad[0]["action"] == "keep" and "无效" in bad[0]["reason"], bad)
    # cfg.mock=True 时绝不能建 OpenAI client（无 key 也要能跑）
    import app.ai_refine as ar
    orig = ar._make_client
    ar._make_client = lambda cfg: (_ for _ in ()).throw(AssertionError("mock 不应建 client"))
    try:
        m = refine_boxes(page, boxes[:2], cfg=AIConfig(mock=True, base_url="http://127.0.0.1:1/v1",
                                                       api_key="sk-should-be-ignored"))
        c.check("mock 模式不建 OpenAI client，也不连 base_url",
                len(m) == 2 and all(p["action"] == "tighten" for p in m), len(m))
    finally:
        ar._make_client = orig


def test_select(c: Checker) -> None:
    c.group("选框：ids / mode=suspect")
    page = synth_page()
    boxes = synth_boxes(6)
    boxes[1]["flags"] = ["stain"]
    boxes[4]["review"] = 2
    props = refine_boxes(page, boxes, ids=[2, 4], cfg=AIConfig(mock=True))
    c.check("ids 按 box id 过滤", [p["box_id"] for p in props] == [2, 4], [p["box_id"] for p in props])
    sus = refine_boxes(page, boxes, mode="suspect", cfg=AIConfig(mock=True))
    c.check("mode=suspect 只留可疑框", [p["box_id"] for p in sus] == [2, 5], [p["box_id"] for p in sus])
    noid = [{"x0": 10, "y0": 10, "x1": 60, "y1": 70} for _ in range(3)]
    c.check("无 id 时按 1 起序号匹配",
            [p["box_id"] for p in refine_boxes(page, noid, ids=[2], cfg=AIConfig(mock=True))] == [2])
    c.check("无 id 时 ids=[0] 回退 0 起下标",
            [p["box_id"] for p in refine_boxes(page, noid, ids=[0], cfg=AIConfig(mock=True))] == [1])
    c.check("空 boxes -> []", refine_boxes(page, [], cfg=AIConfig(mock=True)) == [])
    c.check("ids 全落空 -> []", refine_boxes(page, boxes, ids=[99], cfg=AIConfig(mock=True)) == [])
    c.check("mode=quick 等同 all", len(refine_boxes(page, boxes, mode="quick", cfg=AIConfig(mock=True))) == 6)
    # app-api 会先用自己的 suspect 启发式算好 ids，再传 ids + mode="suspect"；此时 ids 说了算
    both = refine_boxes(page, boxes, ids=[1], mode="suspect", cfg=AIConfig(mock=True))
    c.check("ids 已给时 mode=suspect 不再二次过滤（app-api 约定）",
            [p["box_id"] for p in both] == [1], [p["box_id"] for p in both])


def test_request(c: Checker) -> None:
    c.group("请求体结构（假服务记录）+ 图真的带了")
    page = synth_page()
    boxes = synth_boxes(1)
    with FakeServer() as fs:
        fs.state.default = dict(TIGHTEN_JSON)
        cfg = _cfg(fs)
        props = refine_boxes(page, boxes, cfg=cfg)
        c.check("每次框 1 个请求", len(fs.state.requests) == 1, len(fs.state.requests))
        req = fs.state.requests[0]
        body = req["body"]
        c.check("请求路径是 chat/completions", req["path"].endswith("/chat/completions"), req["path"])
        c.check("model 已带上", body.get("model") == "deepseek-flash", body.get("model"))
        msgs = body.get("messages") or []
        c.check("messages = [system, user]", [m.get("role") for m in msgs] == ["system", "user"],
                [m.get("role") for m in msgs])
        content = msgs[1].get("content") if len(msgs) > 1 else None
        c.check("user content 是 [text, image_url] 两段",
                isinstance(content, list) and [p.get("type") for p in content] == ["text", "image_url"],
                [p.get("type") for p in content] if isinstance(content, list) else content)
        text_part = content[0]
        img_part = content[1]
        c.check("prompt 要求只输出 JSON",
                "只输出一个 JSON 对象" in text_part.get("text", ""), text_part.get("text", "")[:80])
        c.check("prompt 含归一化 0-1000 说明", "0-1000" in text_part.get("text", ""))
        image_url = img_part.get("image_url") or {}
        url = image_url.get("url") or ""
        c.check("image_url.url 是 data URL（base64）",
                url.startswith("data:image/jpeg;base64,") and len(url) > 2000, url[:48])
        c.check("image_url.detail = original", image_url.get("detail") == "original", image_url.get("detail"))
        prefix, b64, raw, img = _decode_data_url(url)
        c.check("base64 解码后是 JPEG（FFD8 魔数）", raw[:2] == b"\xff\xd8", raw[:2])
        c.check("JPEG 可被 PIL 解码且格式=jpeg", img.format == "JPEG", img.format)
        c.check("temperature=0 / stream=False",
                body.get("temperature") == 0.0 and body.get("stream") is False,
                (body.get("temperature"), body.get("stream")))
        # 尺寸/画框校验：字高 160 < 240 -> 放大 1.5x；裁块 (40,0,280,404)
        crop = crop_context(*page.shape[1::-1], (boxes[0]["x0"], boxes[0]["y0"],
                                                 boxes[0]["x1"], boxes[0]["y1"]))
        c.check("裁块符合 0.9h/0.5w 上下文", crop == (40, 0, 280, 404), crop)
        dl = {k: (boxes[0][k]) for k in ("x0", "y0", "x1", "y1")}
        payload = build_request_image(page, (dl["x0"], dl["y0"], dl["x1"], dl["y1"]), crop)
        c.check("送模型图尺寸 = 裁块 x 1.5（字高需 >=240）",
                img.size == (payload["width"], payload["height"]) and payload["glyph_px"] >= 240,
                (img.size, payload["glyph_px"]))
        red_n, red = _red_bbox(img)
        c.check("图上确实画了红框（红像素 > 100）", red_n > 100, red_n)
        fw = red[2] - red[0] + 1
        fh = red[3] - red[1] + 1
        c.check("红框高度 ≈ 240px（目标字高）", 232 <= fh <= 250, fh)
        c.check("红框宽高比 ≈ 原框 120x160",
                abs((fw / fh) - (120.0 / 160.0)) < 0.08, (fw, fh))
        c.check("假服务返回的 tighten 已换算回原图坐标",
                props[0]["action"] == "tighten" and len(props[0]["after"]) == 1,
                props[0])
        after = props[0]["after"][0]
        exp_after = denorm_bbox([380, 380, 620, 620], crop)
        c.check("after 坐标 = 归一化换算结果",
                [after["x0"], after["y0"], after["x1"], after["y1"]] == exp_after, (after, exp_after))
    with FakeServer() as fs:
        fs.state.default = dict(DEFAULT_JSON)
        refine_boxes(page, boxes[:1], cfg=_cfg(fs, detail="low"))
        c.check("detail 可配为 low",
                fs.state.last_body()["messages"][1]["content"][1]["image_url"]["detail"] == "low",
                fs.state.last_body()["messages"][1]["content"][1]["image_url"].get("detail"))


def test_json_tolerance(c: Checker) -> None:
    c.group("模型回复 JSON 容错（代码块 / 废话 / 坏 JSON）")
    page = synth_page()
    boxes = synth_boxes(1)
    fenced = ("好的，建议如下：\n" + FENCE + "json\n"
              + json.dumps(TIGHTEN_JSON, ensure_ascii=False) + "\n" + FENCE + "\n以上。")
    with FakeServer() as fs:
        fs.state.script = [{"content": fenced}]
        props = refine_boxes(page, boxes, cfg=_cfg(fs))
        c.check("代码块+废话 -> tighten 解析成功", props[0]["action"] == "tighten", props[0])
        c.check("confidence 解析为 0.8", abs(props[0]["confidence"] - 0.8) < 1e-6, props[0]["confidence"])
        c.check("reason 保留", props[0]["reason"] == "向右扩", props[0]["reason"])
    with FakeServer() as fs:
        bad = "抱歉，这张图我无法判断。"
        fs.state.script = [{"content": bad}]
        props = refine_boxes(page, boxes, cfg=_cfg(fs))
        c.check("坏 JSON -> 退回 keep（不崩）", props[0]["action"] == "keep", props[0])
        c.check("坏 JSON 的 reason 带错误标记", props[0]["reason"].startswith("错误:"), props[0]["reason"])
        c.check("坏 JSON 不重试（只 1 次请求）", len(fs.state.requests) == 1, len(fs.state.requests))
    with FakeServer() as fs:
        fs.state.script = [{"content": json.dumps({"action": "tighten",
                                                  "bboxes": [[620, 620, 380, 380]],
                                                  "confidence": 1.7, "reason": "乱序坐标"})}]
        props = refine_boxes(page, boxes, cfg=_cfg(fs))
        c.check("bboxes 乱序坐标自动排序", props[0]["action"] == "tighten", props[0]["action"])
        c.check("confidence 超界被 clamp 到 1.0", props[0]["confidence"] == 1.0, props[0]["confidence"])


def test_retry(c: Checker) -> None:
    c.group("429/5xx 指数退避重试（2 次）")
    page = synth_page()
    boxes = synth_boxes(1)
    with FakeServer() as fs:
        fs.state.script = [{"status": 429, "message": "rate limited"},
                           {"status": 500, "message": "boom"}, {}]
        fs.state.default = dict(DEFAULT_JSON)
        t0 = time.time()
        props = refine_boxes(page, boxes, cfg=_cfg(fs, retry_backoff=0.01))
        c.check("429 -> 500 -> 200：共 3 次请求", len(fs.state.requests) == 3, len(fs.state.requests))
        c.check("重试后拿到正常结果", props[0]["action"] == "keep", props[0])
        c.check("退避耗时可控（<2s）", time.time() - t0 < 2.0, round(time.time() - t0, 3))
    with FakeServer() as fs:
        fs.state.script = [{"status": 503, "message": "unavailable"} for _ in range(3)]
        props = refine_boxes(page, boxes, cfg=_cfg(fs, retry_backoff=0.01))
        c.check("重试耗尽：3 次后返回 keep+错误", len(fs.state.requests) == 3 and props[0]["action"] == "keep",
                (len(fs.state.requests), props[0]))
    with FakeServer() as fs:
        fs.state.script = [{"status": 400, "message": "bad request"}]
        props = refine_boxes(page, boxes, cfg=_cfg(fs, retry_backoff=0.01))
        c.check("400 不重试（只 1 次请求）", len(fs.state.requests) == 1, len(fs.state.requests))
        c.check("400 -> keep + 错误原因", props[0]["action"] == "keep", props[0])


def test_concurrency(c: Checker) -> None:
    c.group("并发（ThreadPoolExecutor + max_workers=4）")
    page = synth_page()
    boxes = synth_boxes(8)
    with FakeServer() as fs:
        fs.state.default = dict(DEFAULT_JSON)
        fs.state.script = [{"delay": 0.12} for _ in range(8)]
        t0 = time.time()
        props = refine_boxes(page, boxes, cfg=_cfg(fs, max_workers=4))
        elapsed = time.time() - t0
        c.check("8 框全部返回且按输入顺序", len(props) == 8 and [p["box_id"] for p in props] == list(range(1, 9)),
                [p["box_id"] for p in props])
        c.check("并发度 >=2（确实并发）", fs.state.max_inflight >= 2, fs.state.max_inflight)
        c.check("并发度 <= max_workers=4", fs.state.max_inflight <= 4, fs.state.max_inflight)
        c.check("总耗时接近 2 批而非 8 批（<1.0s）", elapsed < 1.0, round(elapsed, 3))


def test_cancel(c: Checker) -> None:
    c.group("取消（cancel_cb 返回 True 就不抛异常地返回已完成部分）")
    page = synth_page()
    boxes = synth_boxes(12)
    seen = []

    def prog(done, total):
        seen.append((done, total))

    st = {"done": 0}

    def prog_track(done, total):
        st["done"] = done
        seen.append((done, total))

    def cancel_when_one():
        return st["done"] >= 1

    t0 = time.time()
    props = refine_boxes(page, boxes, cfg=AIConfig(mock=True, mock_delay=0.08, max_workers=2),
                         progress_cb=prog_track, cancel_cb=cancel_when_one)
    elapsed = time.time() - t0
    c.check("取消后不抛异常、返回已完成部分", 1 <= len(props) < 12, len(props))
    c.check("取消很快（<1.5s，不去等剩下 11 框）", elapsed < 1.5, round(elapsed, 3))
    c.check("进度回调包含 (0,12) 且单调不降",
            seen and seen[0] == (0, 12) and all(b[0] >= a[0] for a, b in zip(seen, seen[1:])),
            seen[:6])
    st["done"] = 0
    props2 = refine_boxes(page, boxes, cfg=AIConfig(mock=True), cancel_cb=lambda: True)
    c.check("一开始就取消 -> 返回空列表", props2 == [], props2)
    props3 = refine_boxes(page, boxes, cfg=AIConfig(mock=True), progress_cb=lambda done, total: 1 / 0,
                          cancel_cb=lambda: False)
    c.check("progress_cb 抛错不影响复核", len(props3) == 12, len(props3))


def test_conn(c: Checker) -> None:
    c.group("test_connection")
    with FakeServer() as fs:
        fs.state.script = [{"content": "ok"}]
        r = test_connection(_cfg(fs))
        c.check("连通性成功 -> ok True", r["ok"] is True and "连接成功" in r["message"], r)
        body = fs.state.requests[0]["body"] if fs.state.requests else {}
        c.check("test_connection 只发文本（不带图）",
                isinstance((body.get("messages") or [{}])[0].get("content"), str), body.get("messages"))
    with FakeServer() as fs:
        fs.state.script = [{"status": 401, "message": "invalid key"}]
        r = test_connection(_cfg(fs))
        c.check("401 -> ok False 且提示 Key", r["ok"] is False and "401" in r["message"], r)
    r = test_connection(AIConfig())
    c.check("无 key -> ok False 提示未配置", r["ok"] is False and "未配置" in r["message"], r)
    import app.ai_refine as ar
    orig = ar._make_client
    ar._make_client = lambda cfg: (_ for _ in ()).throw(AssertionError("mock 不应建 client"))
    try:
        r = test_connection(AIConfig(mock=True))
        c.check("mock -> ok True 且不建 OpenAI client", r["ok"] is True and "[mock]" in r["message"], r)
    finally:
        ar._make_client = orig


# ================================================================ 入口
def _force_utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def run_selftest() -> int:
    _force_utf8()
    print("ai_refine 自测：mock + 归一化换算 + 校验逻辑 + 本地假 OpenAI 服务（不连外网）")
    print("python:", sys.version.split()[0])
    c = Checker()
    groups = [test_norm, test_parse, test_validate, test_mock, test_select,
              test_request, test_json_tolerance, test_retry, test_concurrency,
              test_cancel, test_conn]
    for fn in groups:
        try:
            fn(c)
        except Exception:
            c.check("测试组 " + fn.__name__ + " 未崩溃", False, traceback.format_exc(limit=3))
    return c.summary()


def _load_real_page():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    gray_npy = os.path.join(root, "scratch", "gray_1.npy")
    page_json = os.path.join(root, "out", "page1", "page.json")
    if os.path.exists(gray_npy) and os.path.exists(page_json):
        g = np.load(gray_npy)
        rgb = np.repeat(g[:, :, None], 3, axis=2)
        with open(page_json, "r", encoding="utf-8") as fh:
            page = json.load(fh)
        boxes = []
        for gd in page["glyphs"][:3]:
            boxes.append({"id": gd["id"], "x0": gd["x0"], "y0": gd["y0"],
                          "x1": gd["x1"], "y1": gd["y1"]})
        return rgb, boxes, "真页：scratch/gray_1.npy + out/page1/page.json 的前 3 个字"
    return synth_page(), synth_boxes(3), "合成测试图（未找到 scratch/gray_1.npy）"


def run_fake_server_evidence() -> int:
    _force_utf8()
    page, boxes, page_desc = _load_real_page()
    print("证据模式页面：", page_desc, " shape=", page.shape)
    fenced = ("好的。\n" + FENCE + "json\n" + json.dumps(TIGHTEN_JSON, ensure_ascii=False)
              + "\n" + FENCE + "\n以上。")
    with FakeServer() as fs:
        fs.state.script = [{"content": fenced}]
        cfg = _cfg(fs, timeout=30.0)
        props = refine_boxes(page, boxes, cfg=cfg)
        req = fs.state.requests[0]
        body = req["body"]
        msgs = body["messages"]
        user = msgs[1]["content"]
        text_part = user[0]["text"]
        image_part = user[1]["image_url"]
        prefix, b64, raw, img = _decode_data_url(image_part["url"])
        red_n, red = _red_bbox(img)
        evidence = {
            "假服务记录_请求数": len(fs.state.requests),
            "请求路径": req["path"],
            "model": body.get("model"),
            "temperature": body.get("temperature"),
            "max_tokens": body.get("max_tokens"),
            "stream": body.get("stream"),
            "messages_roles": [m.get("role") for m in msgs],
            "system_prompt": msgs[0]["content"],
            "user_parts": [p.get("type") for p in user],
            "image_url_url_前缀": image_part["url"][:40],
            "image_url_url_长度": len(image_part["url"]),
            "image_url_detail": image_part.get("detail"),
            "base64_sha256": hashlib.sha256(b64.encode("ascii")).hexdigest(),
            "解码后字节数": len(raw),
            "JPEG_魔数": raw[:2].hex(),
            "解码图尺寸": list(img.size),
            "解码图格式": img.format,
            "红色像素数": red_n,
            "红框包围盒_闭区间": list(red) if red else None,
            "红框高度px_约等于目标字高": (red[3] - red[1] + 1) if red else None,
            "prompt全文": text_part,
            "假服务返回": json.loads(json.dumps(TIGHTEN_JSON, ensure_ascii=False)),
            "换算出的 Proposal": props[0] if props else None,
        }
        print()
        print("=" * 72)
        print("请求体证据（服务端记录，证明真的有图）")
        print("=" * 72)
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
        ok = (image_part["url"].startswith("data:image/jpeg;base64,") and len(raw) > 10000
              and red_n > 100 and img.format == "JPEG")
        print()
        print("证据判定：", "PASS（请求里真的带了 JPEG 裁块图 + 红框）" if ok else "FAIL")
        return 0 if ok else 1


# ================================================================ 真页压力/混合场景
def _box_vals(q: dict) -> list:
    return [q["x0"], q["y0"], q["x1"], q["y1"]]


def _load_page_many(n: int):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    gray_npy = os.path.join(root, "scratch", "gray_1.npy")
    page_json = os.path.join(root, "out", "page1", "page.json")
    g = np.load(gray_npy)
    rgb = np.repeat(g[:, :, None], 3, axis=2)
    with open(page_json, "r", encoding="utf-8") as fh:
        page = json.load(fh)
    glyphs = page["glyphs"]
    if len(glyphs) > n:
        step = max(1, len(glyphs) // n)
        glyphs = glyphs[::step][:n]
    boxes = [{"id": gd["id"], "x0": gd["x0"], "y0": gd["y0"], "x1": gd["x1"], "y1": gd["y1"],
              "flags": [], "review": 0, "note": ""} for gd in glyphs]
    return rgb, boxes, len(page["glyphs"])


def _norm_from_prompt(body: dict):
    try:
        text = body["messages"][1]["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None
    m = re.search(r"归一化坐标 \[\[([-\d.]+),\s*([-\d.]+),\s*([-\d.]+),\s*([-\d.]+)\]\]", text)
    if not m:
        return None
    return [float(m.group(i)) for i in range(1, 5)]


def _cycle_responder():
    counter = [0]
    lock = threading.Lock()

    def cl(v):
        return max(0.0, min(1000.0, float(v)))

    def resp(body):
        with lock:
            i = counter[0]
            counter[0] += 1
        nb = _norm_from_prompt(body)
        if nb is None:
            return dict(DEFAULT_JSON)
        x0, y0, x1, y1 = nb
        mode = i % 6
        if mode == 0:
            dx = (x1 - x0) * 0.10
            dy = (y1 - y0) * 0.10
            return {"action": "tighten",
                    "bboxes": [[cl(x0 + dx), cl(y0 + dy), cl(x1 - dx), cl(y1 - dy)]],
                    "confidence": 0.85, "reason": "收紧"}
        if mode == 1:
            mid = (x0 + x1) / 2.0
            return {"action": "split", "bboxes": [[x0, y0, mid, y1], [mid, y0, x1, y1]],
                    "confidence": 0.8, "reason": "两字"}
        if mode == 2:
            dx = (x1 - x0) * 0.15
            dy = (y1 - y0) * 0.15
            return {"action": "merge",
                    "bboxes": [[cl(x0 - dx), cl(y0 - dy), cl(x1 + dx), cl(y1 + dy)]],
                    "confidence": 0.7, "reason": "补全"}
        if mode == 3:
            mid = (x0 + x1) / 2.0
            return {"action": "split",
                    "bboxes": [[x0, y0, cl(mid + 150), y1], [cl(mid - 150), y0, x1, y1]],
                    "confidence": 0.4, "reason": "重叠测试"}
        if mode == 4:
            return dict(DEFAULT_JSON)
        return "抱歉，我无法判断。"

    return resp


def run_stress() -> int:
    _force_utf8()
    c = Checker()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if not (os.path.exists(os.path.join(root, "scratch", "gray_1.npy"))
            and os.path.exists(os.path.join(root, "out", "page1", "page.json"))):
        print("压力测试需要 scratch/gray_1.npy + out/page1/page.json（没找到）")
        return 2
    page, boxes, n_all = _load_page_many(520)
    print("真页：共 %d 个 glyph；取 %d 个跑 mock" % (n_all, len(boxes)))
    page_h, page_w = page.shape[:2]
    t0 = time.time()
    props = refine_boxes(page, boxes, cfg=AIConfig(mock=True, max_workers=4))
    dt = time.time() - t0
    c.group("真页 %d 框 mock 回归" % len(boxes))
    c.check("全部返回", len(props) == len(boxes), len(props))
    c.check("全部 tighten（mock 确定性）", all(p["action"] == "tighten" for p in props))
    c.check("耗时 <10s", dt < 10.0, round(dt, 2))
    c.check("reason 全部 <=20 字", all(len(p["reason"]) <= 20 for p in props))
    c.check("after 坐标均在页内",
            all(0 <= q[k] <= (page_w if k in ("x0", "x1") else page_h)
                for p in props for q in p["after"] for k in ("x0", "y0", "x1", "y1")))
    c.check("after 恒 1 框", all(len(p["after"]) == 1 for p in props))
    print("  mock 总耗时 = %.2fs（%.2f ms/框）" % (dt, dt / max(1, len(boxes)) * 1000))

    c.group("真页裁块/JPEG 编码抽样（每 8 个取 1）")
    sizes, scales, bad = [], [], []
    for i, b in enumerate(boxes):
        if i % 8:
            continue
        box = (b["x0"], b["y0"], b["x1"], b["y1"])
        payload = build_request_image(page, box, crop_context(page_w, page_h, box))
        sizes.append(len(payload["jpeg"]))
        scales.append(payload["scale"])
        if payload["glyph_px"] < 239.9 or max(payload["width"], payload["height"]) > 4096:
            bad.append((b["id"], payload["glyph_px"], payload["width"], payload["height"]))
    c.check("抽样 %d 框：字高 >=240px 且单边 <=4096" % len(sizes), not bad, bad[:3])
    print("  JPEG 字节 min/median/max = %d / %d / %d"
          % (min(sizes), int(np.median(sizes)), max(sizes)))
    print("  放大倍数 min/median/max = %.2f / %.2f / %.2f"
          % (min(scales), float(np.median(scales)), max(scales)))

    c.group("真页 + 假服务混合响应（tighten/split/merge/非法 split/keep/坏 JSON）")
    page2, boxes2, _ = _load_page_many(12)
    responder = _cycle_responder()      # 同一个 responder：第 n 个请求走第 n 种响应
    with FakeServer() as fs:
        fs.state.script = [{"content": responder} for _ in range(len(boxes2))]
        cfg = _cfg(fs, max_workers=4, timeout=30.0)
        t1 = time.time()
        props2 = refine_boxes(page2, boxes2, cfg=cfg)
        dt2 = time.time() - t1
    acts = [p["action"] for p in props2]
    c.check("12 框全部返回", len(props2) == 12, len(props2))
    c.check("action 分布 = tighten2/split2/merge2/keep6",
            (acts.count("tighten"), acts.count("split"), acts.count("merge"), acts.count("keep"))
            == (2, 2, 2, 6), acts)
    reasons = [p["reason"] for p in props2]
    c.check("非法 split 退回 keep（原因含 重叠）", sum("重叠" in r for r in reasons) >= 2,
            [r for r in reasons if "重叠" in r])
    c.check("坏 JSON 退回 keep（原因含 错误）", sum(r.startswith("错误:") for r in reasons) >= 2,
            [r for r in reasons if r.startswith("错误:")])
    splits = [p for p in props2 if p["action"] == "split"]
    c.check("split 结果都是 2 个互不重叠的框",
            all(len(p["after"]) == 2
                and iou(_box_vals(p["after"][0]), _box_vals(p["after"][1])) < 0.02
                for p in splits), [p["after"] for p in splits])
    c.check("所有 after 坐标在页内",
            all(0 <= q[k] <= (page2.shape[1] if k in ("x0", "x1") else page2.shape[0])
                for p in props2 for q in p["after"] for k in ("x0", "y0", "x1", "y1")))
    c.check("12 框并发耗时 <5s", dt2 < 5.0, round(dt2, 2))
    print("  混合场景耗时 = %.2fs" % dt2)
    return c.summary()


# ================================================================ 与 server.py 的端到端集成（TestClient）
def _wait_job(client, jid: str, timeout: float) -> dict:
    end = time.time() + timeout
    job = {}
    while time.time() < end:
        job = client.get("/api/jobs/%s" % jid).json()
        if job.get("state") in ("done", "error", "canceled"):
            return job
        time.sleep(0.05)
    return job


def run_api_integration() -> int:
    """走 server.py 真路由：导入->处理->AI 复核(mock)->proposals->apply_all。"""
    _force_utf8()
    c = Checker()
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="yi_ai_integration_")
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    os.environ["YI_SEGMENT_HOME"] = home          # 绝不碰真实 %LOCALAPPDATA%
    os.environ["YI_SEGMENT_NO_DIALOG"] = "1"
    try:
        from fastapi.testclient import TestClient
        from .server import create_app

        proj = os.path.join(tmp, "proj")
        client = TestClient(create_app(proj))
        img_path = os.path.join(tmp, "page1.png")
        Image.fromarray(synth_page(600, 800, seed=3)).save(img_path)

        c.group("经由 server.py 的 AI 复核端到端（mock / 无 key）")
        r = client.post("/api/pages/add", json={"paths": [img_path]})
        c.check("POST /api/pages/add", r.status_code == 200 and bool(r.json().get("added")), r.text[:200])
        pid = (r.json().get("added") or [""])[0]

        r = client.post("/api/pages/process", json={"ids": "all"})
        c.check("POST /api/pages/process", r.status_code == 200, r.text[:200])
        job = _wait_job(client, r.json()["job_id"], 120)
        c.check("处理 job 完成", job.get("state") == "done", job.get("errors"))
        boxes = (client.get("/api/pages/%s" % pid).json().get("boxes") or [])
        c.check("分割出框", len(boxes) > 0, len(boxes))
        print("  处理出 %d 个框" % len(boxes))
        # 换成已知尺寸的两个框，让 mock 的 2% 收紧一定改变整数坐标（合成页分割出的框可能只有几像素）
        known = [{"x0": 60, "y0": 60, "x1": 260, "y1": 360, "flags": [], "review": 0, "note": ""},
                 {"x0": 320, "y0": 60, "x1": 520, "y1": 360, "flags": [], "review": 0, "note": ""}]
        r = client.put("/api/pages/%s/boxes" % pid, json={"boxes": known})
        c.check("PUT /api/pages/{pid}/boxes", r.status_code == 200, r.text[:200])
        boxes = r.json().get("boxes") or []

        r = client.post("/api/pages/%s/ai-refine" % pid, json={"ids": "all", "mode": "all"})
        c.check("POST /api/pages/{pid}/ai-refine", r.status_code == 200, r.text[:200])
        job = _wait_job(client, r.json()["job_id"], 120)
        c.check("ai-refine job 完成（不是 error）", job.get("state") == "done", job.get("errors"))
        props = client.get("/api/pages/%s/proposals" % pid).json().get("proposals") or []
        c.check("proposals 非空", len(props) > 0, len(props))
        c.check("mock：每框一条 suggestion", len(props) == len(boxes), (len(props), len(boxes)))
        first = props[0] if props else {}
        c.check("box_id 是 int 且在框 id 集合内",
                isinstance(first.get("box_id"), int) and first.get("box_id") in {b["id"] for b in boxes},
                first.get("box_id"))
        c.check("before 是 dict {x0,y0,x1,y1}",
                isinstance(first.get("before"), dict) and set(first["before"]) == {"x0", "y0", "x1", "y1"},
                first.get("before"))
        c.check("after 是非空 list[dict]（server 把 keep 补成 [before]）",
                isinstance(first.get("after"), list) and first["after"] and isinstance(first["after"][0], dict),
                first.get("after"))
        c.check("action=tighten 且 reason 带 [mock]",
                first.get("action") == "tighten" and "[mock]" in (first.get("reason") or ""),
                (first.get("action"), first.get("reason")))
        c.check("status=pending", first.get("status") == "pending", first.get("status"))

        r = client.post("/api/pages/%s/proposals" % pid, json={"apply_all": True})
        c.check("POST proposals apply_all -> 200", r.status_code == 200, r.text[:200])
        new_boxes = client.get("/api/pages/%s" % pid).json().get("boxes") or []
        c.check("apply 后框数不变", len(new_boxes) == len(boxes), (len(new_boxes), len(boxes)))
        changed = any((a["x0"], a["y0"], a["x1"], a["y1"]) != (b["x0"], b["y0"], b["x1"], b["y1"])
                      for a, b in zip(new_boxes, boxes))
        c.check("apply 后坐标确有变化（收紧 2%）", changed)

        # ids="suspect"：server 先用 pipeline.suspect_ids 解析成 ids 再传给我（ids 说了算）
        sus = [{"x0": 60, "y0": 60, "x1": 260, "y1": 360, "flags": [], "review": 0, "note": ""},
               {"x0": 320, "y0": 60, "x1": 520, "y1": 360, "flags": [], "review": 0, "note": ""},
               {"x0": 60, "y0": 500, "x1": 80, "y1": 520, "flags": [], "review": 0, "note": ""}]
        r = client.put("/api/pages/%s/boxes" % pid, json={"boxes": sus})
        boxes = r.json().get("boxes") or []
        tiny = [b for b in boxes if (b["x1"] - b["x0"]) * (b["y1"] - b["y0"]) <= 400]
        c.check("准备一个可疑小框", len(tiny) == 1, tiny)
        r = client.post("/api/pages/%s/ai-refine" % pid, json={"ids": "suspect", "mode": "suspect"})
        job = _wait_job(client, r.json()["job_id"], 120)
        c.check("ids=suspect 的 job 完成", job.get("state") == "done", job.get("errors"))
        props = client.get("/api/pages/%s/proposals" % pid).json().get("proposals") or []
        c.check("suspect 只复核那个小框（ids 优先，mode 不重复过滤）",
                len(props) == 1 and tiny and props[0]["box_id"] == tiny[0]["id"],
                [(p["box_id"], p["action"]) for p in props])

        r = client.post("/api/ai/test", json={})
        c.check("POST /api/ai/test 无 key -> ok False",
                r.status_code == 200 and r.json().get("ok") is False, r.json())
        st = client.get("/api/state").json()
        c.check("GET /api/state ai_available=True", st.get("ai_available") is True, st.get("ai_available"))
    except Exception:
        c.check("集成测试未抛异常", False, traceback.format_exc(limit=4))
    finally:
        os.environ.pop("YI_SEGMENT_NO_DIALOG", None)
        os.environ.pop("YI_SEGMENT_HOME", None)
        shutil.rmtree(tmp, ignore_errors=True)
    return c.summary()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="app.ai_selftest", description="ai_refine 离线自测")
    ap.add_argument("--selftest", action="store_true", help="跑全套自测（默认）")
    ap.add_argument("--evidence", action="store_true", help="打印假服务记录的请求体证据")
    ap.add_argument("--stress", action="store_true", help="真页 520 框 mock 回归 + 假服务混合响应")
    ap.add_argument("--api", action="store_true", help="经由 server.py 的端到端集成（TestClient，mock）")
    args = ap.parse_args(argv)
    if args.evidence:
        return run_fake_server_evidence()
    if args.stress:
        return run_stress()
    if args.api:
        return run_api_integration()
    return run_selftest()


if __name__ == "__main__":
    raise SystemExit(main())
