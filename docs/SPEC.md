# 彝文古籍切分工具 — 规格 (SPEC)

目标：把整页扫描（彝族手抄本，竖排，自右向左）切成**单个字**的小图，样式对齐用户给的样式图
（001.png, 002.png ...：紧贴字形的矩形裁切 + 少量底色留白，保留原始纸色，不二值化）。

输入：单页 8-bit RGB TIF，示例 `彝族扫描3-0002_right.tif`，6354 x 11042 px，纸色约 184–208 灰阶，墨色 5–100。
真正要支持"大规模"：一次喂目录 / 几百页，多进程、可断点续跑。

## 目录结构（全部相对 yi-segment/）

```
tools/
  yi_core/
    common.py        # [Lead] 共享：IO / 匀光 / 二值化 / 投影 / 画框 / dataclass  ← 已存在，勿改
    segment_v1.py    # [Lead] 主算法：segment_page(gray, **params) -> page dict
    segment_alt.py   # [alt-segmenter] 独立第二算法，同签名，用于交叉验证
    export.py        # [exporter] 裁图 + index.csv + 拼版缩略图
    qc.py            # [qc] 质检叠加图 / 缺陷统计
  yi_segment.py      # [exporter] CLI 入口（batch / resume / 多进程）
docs/SPEC.md, docs/prior-art.md
out/<scanname>/      # 产出
```

## page dict（JSON，模块之间唯一契约）

```json
{
  "source": "A:/.../彝族扫描3-0002_right.tif",
  "width": 6354, "height": 11042,
  "columns": [{"idx":0,"x0":int,"x1":int,"w":int,"ink":int}, ...],
  "glyphs": [
    {"id":1, "x0":int,"y0":int,"x1":int,"y1":int, "w":int,"h":int,
     "col":int, "row":int, "ink":int, "area":int, "flags":["..."]}, ...
  ],
  "params": {...}
}
```
坐标一律为**原图像素**，x1/y1 为开区间（Python slice 语义）。id 从 1 开始，按阅读顺序（默认：列自右向左，
列内自上而下）连续编号 —— 生成时可改，`col`/`row` 始终保留以便重排。

## 各模块签名

- `segment_v1.segment_page(gray: np.ndarray, **params) -> dict`（不含 source/width/height 时自动补）
- `export.export_tiles(rgb, page, out_dir, pad_ratio=0.10, min_pad=6, prefix="", order="reading", fmt="png") -> list[dict]`
  写 `0001.png...`（位数按总数补零，最少 3 位）+ `index.csv` + `sheet.png`（13 列拼版，带编号）
- `qc.write_qc(rgb, page, out_dir, strips=True, target_w=2400)` → overlay.png + 分带高清图 + defects.json
- `yi_segment.py --input X --out Y [--jobs N] [--resume] [--limit N]`

## 算法要点（已勘明的事实）
- 纸色不均 + 霉斑 + 背面透字 → 必须先匀光（大核 box blur 除法），再 Sauvola (win≈61@1x, k≈0.25–0.34)。
- 字符区约 300x350 px，列距 ≈400 px；笔画常有断开（一个字由 2–6 个连通域组成）。
- 不能靠"零墨行"直接切：字内有空行，相邻字也可能靠得很近 → 需要 DP + 字高先验 + 连通域不切断约束。
- 边缘/装订处有黑边、污渍 → 需按面积/长宽比/墨色深浅剔除，禁止产出噪声字块。
