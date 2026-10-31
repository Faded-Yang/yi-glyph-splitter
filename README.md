# 彝文古籍切字工具 · Yi Glyph Splitter

把整页扫描的彝文古籍（竖排、右起）切成**一字一图**，并提供人工复核界面、离线的「规范字校验」
（对照《规范彝文单字图形识别手册》字库打分）以及可批量跑的 CLI。

![界面](samples/ui_main.svg)

选中某个字框时，右栏给出该框的 3× 细节图（含上下邻字上下文）、规范字相似度与最像的 3 个标准字形：

![对照标准字形](samples/ui_selected.svg)

## 安装（推荐）

下载 `YiGlyphSplit-1.0.0-Setup.exe`，双击 → 下一步 → 完成（中文向导）。

- 默认装到 `%LOCALAPPDATA%\Programs\YiGlyphSplit`（**不需要管理员**；想装到别的盘，向导里改目录即可）
- 开始菜单 / 桌面快捷方式、「设置 → 应用」里的卸载项都会建好
- 安装目录长这样：

      YiGlyphSplit\
        YiGlyphSplit.exe      启动器（约 10 MB）
        runtime\              Python 运行时 + OpenCV / numpy / scipy 等（约 286 MB，1860 多个文件）
        README-install.txt
        unins000.exe          卸载程序

**为什么运行时要 280 多 MB？** 因为它自带完整的 Python 运行时和数值计算库（OpenCV、numpy、scipy、
Pillow、FastAPI…），不需要用户先装 Python——这也是 exe 可以「双击就跑」的原因。真正的**启动器只有
10 MB**，和网上那些几 MB 的单文件小工具比不了，但比之前的「单文件 119 MB exe」在启动速度上更快
（不用每次解压到临时目录）。

## 便携版 / 从源码跑

**A. 便携版**（不安装，绿色）：解压 `dist\YiGlyphSplit\` 整个文件夹，双击里面的 `YiGlyphSplit.exe`。
换机器整体拷走即可。

**B. 源码运行界面**：

    pip install fastapi uvicorn numpy opencv-python scipy pillow pywebview pythonnet
    python -m app.run --project D:\work\book1

**C. 只用命令行批量切图**（无界面，适合几百页）：

    python tools\yi_segment.py --input "D:\scans" --out out\batch --jobs 4 --resume

见 `docs/CLI.md`。

## 流程（桌面程序）

| 步骤 | 操作 | 说明 |
|---|---|---|
| 1 导入 | 导入 TIF（多选）/ 导入文件夹 | 首次让你选一个**项目目录**存放中间数据 |
| 2 一键处理 | 选中页 → 一键处理 | 只在页面上**画框，不切图**；约 13 s/页 |
| 3 规范字校验（离线，自动） | 无需操作：处理完自动跑 | 绿色实线 = 很符合规范字，红色实线 = 可疑，青色 = 一般；**颜色常驻**；约 1 s/页 |
| 4 人工复核 | 拖拽 / 缩放 / 新增 / 删除 / 拆分 / 合并 / 确认 / 存疑 / 手动画色 | 改或新建一个框会**只重算这一框**；改动自动保存 |
| 5 一键切图 | 选导出目录 | **按所有框**输出 `001.png…`、`index.csv`、`sheet.png`（可疑框也照切，只是颜色提示） |

界面细节（都像专业软件）：

- **左右侧栏可折叠、可拖宽**：顶栏两个侧栏图标 / `Ctrl+B`（左）、`Ctrl+Shift+B`（右）；
  拖侧栏之间的分隔条改宽度，双击分隔条恢复默认。宽度和折叠状态会记住（localStorage）。
- 颜色常驻：绿色（≥0.62 很标准）、青色（一般）、红色（<0.38 可疑）、绿/琥珀（人工确认/存疑）、自选色。
  撤销 / 重做 / 保存 / 切页都不会掉色。
- 新建框时指针带**十字延长参考虚线**（拖拽时还有锚点延长线），方便对齐字的边缘。

快捷键：滚轮缩放 / 空格拖动平移 · `A` 新建框 · `Del` 删除 · `S` 以鼠标 y 拆分 · `M` / `Shift+M`
与下/上一框合并 · `1`/`2`/`0` 确认/存疑/复位 · `←/→` 切换 · `Ctrl+Z` 撤销 · `Ctrl+Y` 重做 ·
`Ctrl+B` 左栏 · `Ctrl+Shift+B` 右栏。

> 删框后画布**不会**自动跳到下一个框（只换选中态），连续删框时视角不动。

## 输出

    <项目>/exports/<页名>/
      001.png … NNN.png        单字裁切（原纸色，不二值化；位数按总数补零）
      index.csv                filename,id,col,row,x0..y1,w,h,ink,pad + flags（confirmed/suspect/lowref）
      sheet.png                拼版缩略图（13 列、白底、格下编号）

CLI 还会额外写 `page.json`（全部坐标）与 `overlay.png`（整页叠加图）。

## 算法（纯 OpenCV，无 torch 依赖）

1. 匀光：大核 box blur 除法，压平纸色梯度（霉斑变中性灰）
2. Sauvola 二值化（窗口 ≈ 1 个字宽）
3. 连通域按 面积 / 暗度 / bbox 尺寸 过滤：去灰尘、背面透字、装订黑边、长横线框
4. **横向分块**：整行空白走廊把页面切成 block，正文与顶部批注各自独立分列
5. 列带：y 向闭运算 → x 投影**峰值 + 谷底**（污渍会让固定阈值把全页并成一列）
6. 列内 **1-D 动态规划**切字：代价 = 8·(log(h/H))² + 每刀固定费 + 切口墨量；两遍用全页 H 中位数夹逼
7. 断笔 / 点划按邻近关系并回本体
8. 后处理：只在**真空带**补切「框里还有两个字」；再用 core_frac / ink_frac 剔除霉斑假框

细节见 `docs/SPEC.md`；独立第二实现 `tools/yi_core/segment_alt.py`（纯形态学）用于交叉验证。

## 规范字校验（离线字库）

字库来自 **《规范彝文单字图形识别手册》**（129 页目录式 PDF）：抽出 **1428 个类别、5716 张标准字形**，
归一化成 48×48 墨迹图，打包成 `data/glyphbank/bank.npz`（约 6 MB，随程序分发）。

对每个框：裁墨迹 → 居中缩放到 48×48 → 与 5716 张标准字形算余弦相似度（16×16 粗筛 + 48×48 精选）。

| 分数 | 含义 | 界面 |
|---|---|---|
| ≥ 0.62 | 与某个规范字高度吻合 | **绿色实线框** |
| ≥ 0.50 | 与某个规范字明显吻合 | 青色实线框 |
| 0.40–0.50 | 部分吻合 | 青色实线框（提示「一般」） |
| < 0.38 | **很可能不是一个规范彝文字** | **红色实线框** + 「下一个低分」逐个核对 |

实测本页 520 框：中位 0.49、23 个 ≥0.62、57 个低分。低分框目视核对基本就是「两字粘连」「只剩笔画碎片」
或页面边角的霉斑残留。重建字库：`python scripts/build_glyphbank.py --pdf <手册.pdf>`。

## 准确率（怎么核的）

- 与独立算法交叉验证：匹配框 **IoU 中位 0.991**；分歧集中在「对方把相邻两字并成一个 tall 框」
- 行距实测 ≈260–320 px、14 列 ×≈37 字 = 520 字，与原卷节奏吻合（不是过切）
- 原分辨率逐列长条图（`qc/strips/`）人眼核对：一字一框、框贴字形

## 自己打包

    powershell -ExecutionPolicy Bypass -File packaging\build.ps1

两步：PyInstaller（onedir，约 2–3 分钟）→ Inno Setup 打安装包（约 2–4 分钟，需先装
`winget install JRSoftware.InnoSetup`）。产物：

- `dist\YiGlyphSplit\` —— 绿色便携版（启动器 10 MB + `runtime\` 286 MB）
- `dist\YiGlyphSplit-1.0.0-Setup.exe` —— 安装包（约 78 MB，lzma2 压缩后）

只想要可运行目录、不打安装包：加 `-NoInstaller`。

## 项目结构

    app/                 FastAPI 后端 + 原生 JS 前端 + 入口 run.py
      static/            index.html / app.js / canvas.js / style.css
      yi_ref.py          规范字库匹配（离线打分）
    tools/yi_core/       分割 / 导出 / 质检（可独立当库用）
    data/glyphbank/      规范字库（bank.npz 或文本版 bank.npz.b64）
    packaging/           app.spec / build.ps1 / installer.iss / 图标 / 安装说明
    scripts/e2e_app.py   端到端验收（TestClient）
    docs/                SPEC / CLI / 桌面说明 / 后端契约 / 先例调研
    samples/             示例 tile / 拼版 / 界面截图

## 关于仓库里的二进制

安装包和 exe **不进 git**（体积太大）：用 `packaging\build.ps1` 在本机生成。
包内已含 `tools/` 算法源码、`app/static` 前端与 `data/glyphbank` 字库，运行时不依赖仓库路径。

## 环境与依赖

Python 3.10；numpy / opencv-python / scipy / pillow / fastapi / uvicorn /
pywebview(+pythonnet, 原生窗口)。不需要 GPU、不需要 torch、不需要联网。

## 常见问题

**安装时提示「已安装过」/ 想换目录**：先卸载旧版（开始菜单 →「卸载 彝文切字工具」），再安装。
用户数据在 `%LOCALAPPDATA%\yi-segment\`，卸载不会删除。

**启动慢（3–5 秒）**：启动器要加载 286 MB 的 runtime；装到 SSD 会明显快。别把 `runtime\` 目录单独挪走。

**程序一启动就弹 "Unable to configure formatter 'default'"**：窗口模式（`--noconsole`）下
`sys.stdout/stderr` 是 `None`，而 uvicorn 默认日志配置要写 `sys.stderr`。仓库里 `app/run.py` 已修
（重定向到 devnull + `log_config=None`），改入口时别改回去。

**画布一片空白、右栏「尺寸 0 × 0」**：预览图缺失/写坏，现在会自动重建（前端失败还会带 `?rebuild=1` 重试），
也可以点右栏「重新载入」。日志：`%LOCALAPPDATA%\yi-segment\logs\app.log`。

**可疑框很多**：低分只代表「不像标准字形」，连笔、残墨、边界字都会低分。点「下一个低分」看右栏那 3 个
标准字形：两字粘连按 `S` 拆，碎片 `Del` 删。整页普遍低分多半是扫描倾斜或字迹过淡。

**SmartScreen 拦截**：安装包/程序未签名，选「更多信息 → 仍要运行」。

## 许可证

MIT（见 `LICENSE`）。
