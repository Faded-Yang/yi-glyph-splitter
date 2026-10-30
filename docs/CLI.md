# CLI 使用说明 — tools/yi_segment.py

（本文只讲怎么用；算法见 segment_v1.py，总说明见 README.md）

## 一条命令

    python yi-segment/tools/yi_segment.py --input <TIF 或目录> --out <输出根目录>

单页 TIF 默认 jobs=1（一页整页处理峰值内存约 1.6 GB）；目录默认 jobs<=4。

## 一页会产出什么

    out/<页名>/
      page.json            # 本页 page dict（含 source/width/height/columns/glyphs/params）
      export_params.json   # 本次导出的参数与计数（pad_ratio/order/fmt/scale/glyphs/tiles）
      tiles/
        0001.png ...       # 一个字一张，原纸色矩形裁切，不二值化
        index.csv          # 每张 tile 的坐标与质量字段（见下）
        sheet.png          # 13 列拼版缩略图（样式对齐 docs/style/style_ref_tiles.jpg）

加 --flat 时，tiles 目录的内容直接写在 out/<页名>/ 下（不再有 tiles/ 子目录）。
加 --jpg（等价 --fmt jpg）则 tile 后缀为 .jpg（quality 95）；--no-tiles 只写 page.json。

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| --input | 必填 | 单个 TIF/PNG 或一个目录（目录按扩展名收集，自然排序） |
| --out | 必填 | 输出根目录 |
| --jobs N | 单文件 1，目录 <=4 | spawn 多进程；每 worker 峰值约 1.6 GB |
| --limit N | 0(不限) | 只处理前 N 页（冒烟测试用） |
| --resume | off | page.json + tiles/index.csv 都在就跳过 |
| --scale F | 1.0 | 在 1/F 尺寸上做分割，坐标自动映射回原图（tile 仍从原分辨率裁） |
| --order | reading | reading=列自右向左 / visual=列自左向右 / id=跟随 page dict 的 glyph id |
| --pad-ratio | 0.10 | 留白 = max(min_pad, pad_ratio * max(字宽,字高)) |
| --min-pad | 6 | 留白下限（原图像素） |
| --fmt | png | png/jpg/webp；--jpg 为其简写 |
| --flat | off | tile 直接写在页目录下 |
| --no-sheet | off | 不写 sheet.png |
| --sheet-cols / --cell | 13 / 150 | 拼版列数 / 单格槽宽（缩略图最大边 = 0.7*cell） |
| --sheet-header | off | 在 sheet 顶部加一行「页名 + 字数 + order」 |
| --page-json | 无 | 跳过分割，直接用已有 page dict（文件，或目录按 <stem>.json 匹配） |
| --page-name | 无 | 单页时强制页目录名（如 page1 产出 out/page1） |
| --segment-module | yi_core.segment_v1 | 算法模块：点号模块名，或 .py 文件路径 |
| --pattern | 无 | 目录输入时用正则筛选文件名 |

## 编号顺序 order

- reading：列自右向左、列内自上而下（SPEC 的定义）
- visual：列自左向右、列内自上而下
- id：tile 序号 = page dict 的 glyph id（推荐）

为什么默认值用 reading 而实跑建议 id：page dict 的 id 才是「唯一契约」的阅读顺序，
而 2026-10-31 的 page_v1_1.json 里 id 实际是**左→右列**编号（id1 在最左列 x≈103），
与用户样式图（001 在最左列）一致。用 id 排序可保证 0001.png ↔ id 1 一一对应；
用 reading 会得到「0001.png ↔ 最右列第一个字」。两种都能用 --order 切换。

## 断点续跑与旧 tiles 清理

- --resume 判据：page.json 存在，且（--no-tiles 或 tiles/index.csv 存在）→ 跳过。
- 只有 page.json 而没有 index.csv（tiles 写一半崩了）→ 只重做导出，不重跑分割。
- page.json 的 params.scale / params.segment_module 与本次参数不一致 → 视为过期并重算。
- 每次真正写 tiles 之前先清理旧 tiles：只删匹配 ^\d+\.(png|jpg|jpeg|webp|bmp|tif)$ 的文件
  以及 index.csv / sheet.png；page.json、overlay.png、error.log、export_params.json 一律不动。
  进度行会打印 (cleaned N stale tile files)。
- 写完后会核对目录里 tile 文件数，与本次字数不符会打 [WARN dir has X tile files for Y glyphs]。

## index.csv 字段（20 列）

filename,id,col,row,x0,y0,x1,y1,w,h,ink,pad,gx0,gy0,gx1,gy1,flags,flat,ink_frac,core_frac

- x0..y1/w/h：**实际裁出来的窗口**（已加留白并 clip 到图边界），w/h 与 tile 像素尺寸一致；
  gx0..gy1 是 page dict 里的原始字形框；pad 是本次用的留白。
- ink：page dict 的 ink；flags：page dict 的 flags（clipped/oversize 等，分号分隔）。
- flat / ink_frac / core_frac：page dict 的质量字段（缺失时为空）。
  参考判据：core_frac < 0.15 → 霉斑/透字假框；ink_frac > 0.55 → 污渍糊块。

## sheet.png 规格（对齐 docs/style/style_ref_tiles.jpg）

13 列；格子槽宽 165 px（cell 150 + 间距 15）；缩略图按比例缩到最大边 105 px，
原纸色直接贴在白底上（没有白格、没有边框）；每格下方居中写**文件名**（如 001.png，13 px 深灰）；
行距 185 px。默认无标题行（--sheet-header 可加）。

## 常用命令（都已实跑）

    # 1) 已有 page dict，只重导出（最快，5.9 s/页）
    python yi-segment/tools/yi_segment.py --input "<TIF>" --out yi-segment/out \
        --page-name page1 --page-json yi-segment/scratch/page_v1_1.json --order id

    # 2) 原始 TIF 全流程（分割 + 导出），单页
    python yi-segment/tools/yi_segment.py --input "<TIF>" --out yi-segment/out/page1_tif --limit 1

    # 3) 用 .py 桩/其它算法（--segment-module 支持文件路径）
    python yi-segment/tools/yi_segment.py --input yi-segment/scratch/crop_center.png \
        --out /tmp/out --segment-module yi-segment/out/_exporter_test/stub_segment.py

    # 4) 目录批量 + 4 进程 + 断点续跑
    python yi-segment/tools/yi_segment.py --input D:\scans --out out --jobs 4 --resume

## 性能参考（本机 32 核，6354x11042 单页，--jobs 1）

| 场景 | 耗时 | 备注 |
|---|---|---|
| 全流程（segment_v1 + 导出 520 字） | 13.3 s | 峰值工作集 1644 MB |
| 仅导出（--page-json，520 字） | 5.9 s | 需要 210 MB RGB 原图 |
| 拼版 sheet.png（520 字 → 2170x7425） | 含在上面 | 13 列 40 行 |

## 注意事项

- 不要两个进程同时往同一个 out 目录写同一页：实测会混 tile（36/520 张尺寸错配）。
  并行请分不同 out 根目录，或串行执行。
- page dict 的 json 编码可能是 utf-8-sig / utf-8 / gbk（Lead 写入 cp936 时踩过），
  本 CLI 用 load_json_any 兼容三种；自己写 json 请显式 encoding="utf-8"。
- CLI 内部把 stdout/stderr 设为 utf-8，页名含中文时进度行不会乱码（cp936 控制台会乱）。
- --page-json FILE 与 --page-name 仅用于单页输入；批量请用目录形式的 --page-json。

## 模块 API（给 qc 等调用方）

    from yi_core import export
    recs = export.export_tiles(rgb, page, out_dir,
                               pad_ratio=0.10, min_pad=6, prefix="",
                               order="reading", fmt="png")
    # 返回 list[dict]，字段同 index.csv；同时写 out_dir/index.csv 与 out_dir/sheet.png
