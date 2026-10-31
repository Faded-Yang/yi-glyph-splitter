# yi-segment 台账（彝文古籍整页切字）

## 已验证事实
- 源扫描：A:\Downloads\xwechat_files\wxid_0vb7dipk5onr22_9677\msg\file\2026-10\彝族扫描3-0002_right.tif （6354x11042 RGB8 raw，210MB）
- 几何（原图像素）：纸色 184–208 灰阶、墨 5–100；列距≈400、行距≈310–320（≈35 字/列，14 列）。
  **别用 11042/400 估字数** —— 那是列距不是行距，会误判「过切 1.35x」。
- 列带：y 方向闭运算(h≈0.95·pitch) 后取 x 投影峰 + 谷底切。污渍会让走廊投影非零 → 必须峰值法。
- 每字 2–6 个连通域（p90 11.5）；形态学合并率 k13 只有 28% → 拼字只能靠 DP（prior 实测）。
- 双通道后处理：core_frac(flat<60 占比) < 0.15 → 霉斑/透字假框；ink_frac > 0.55 → 污渍糊块。
- 6.5–7.5 s/页（32 核，全分辨率，纯 OpenCV 单进程）。
- segment_alt（纯形态学：椭圆 close r≈0.055·列距 → CC → 原始墨量深谷切高块 → 孤立笔画按最近吸附）
  1/4 与原分辨率分别出 **488 / 483** 框、14 列、墨量覆盖率 0.871/0.866；跨尺度 best IoU 中位 0.964、≥0.5 占 91.6%。
  → 拼字不非得靠 DP：close + 谷切 + 笔画吸附也能一字一框（原生 2× 目视核实 1:1）。
- 列带 band_merge 必须 ≈0.10·列距：设 0.30 会把右侧相邻两列并成一带（单列 70+ 字）。
- CLI（tools/yi_segment.py）原始 TIF 全流程已跑通：--limit 1 → 13.3 s、峰值 1644 MB、520 字/520 tile；
  与 page_v1_1.json **520/520 框完全相同**（分割确定）。仅导出 --page-json 5.9 s/页。用法见 docs/CLI.md。
- 拼版样式（对齐 docs/style/style_ref_tiles.jpg）：13 列、槽 165 px、缩略图最大边 105 px、白底无格线、
  格下 13 px 文件名。page_v1_1.json 的 glyph id 是**左→右列**编号（id1 x≈103），与样式图 001 在最左列一致
  → 要 0001.png ↔ id1 必须 --order id。


## 桌面程序（2026-10-31 追加）
- 架构：FastAPI(127.0.0.1) + 原生 JS 前端 + PyInstaller onefile；入口 app/run.py（**只能用绝对 import**：frozen 下它作为 __main__ 运行，相对 import 会 ImportError）。
- 打包：packaging/app.spec + build.ps1；dist/YiGlyphSplit.exe ≈103MB；**先杀进程再重打**（exe 在跑时覆盖 dist\*.exe 会 PermissionError）。
- 数据：<项目>/pages/<pid>/{page.json(原始), boxes.json(权威), proposals.json, preview.png(纯图)}；导出用 boxes.json。
- 自测隔离：所有测试必须先在 import app.* 之前设 YI_SEGMENT_HOME=临时目录，否则会写真实 %LOCALAPPDATA%\yi-segment\config.json（已踩）。
- 验收：scripts/e2e_app.py（TestClient，30 检查，真 TIF 全分辨率 0 failures）；exe 冒烟：health/root/add/process 520 框/export 520 tiles+520 csv。
- 前端坑：CSS 类名跨模块撞名（.sel）会静默毁样式；细节图叠加框必须与 <img> 同容器，否则 flex 居中导致 24px 错位。
- 已清理：scratch/、build/、__pycache__、out/_qc_test|_exporter_test|_qc_real*|page1_tif|alt、out/page1/qc（可 python -m yi_core.qc 重生成）。
- GitHub: https://github.com/Faded-Yang/yi-glyph-splitter

## 已验证事实（AI 复核，2026-10-31）
- DeepSeek 的 deepseek-flash **默认开启思考模式**，思维链走 reasoning_content 且**计入 max_tokens**。
  旧代码 max_tokens=200 → 实测 finish=length / reasoning 376 字 / content 空 → 前端 520 条「错误:模型返回空内容」。
- 修法：请求带 extra_body={"thinking":{"type":"disabled"}} + max_tokens=512；实测同一请求 30 tokens、0.6–0.9 s/框。
  OpenAI SDK 传 thinking 必须走 extra_body（顶层参数会被忽略）。
- 真 key 实测：test_connection 带图 0.9 s；4 框 3.0 s；打包 exe 内同样通过。AI 建议多为 tighten，偶有 split。
- /api/ai/test 必须发**带图**请求，只发文本测不出视觉/思考模式问题（已改）。

## 硬约束（含原因，2026-10-31 晚）
- **AI 复核（DeepSeek 视觉）整条功能已删除**（用户：「有些鸡肋」）：入口/面板/设置/弹层/后端路由都去掉；
  不要再加回来，替代品是离线的「规范字校验」。
- **删除框后不允许移动画布视角**（用户明确要求）：setSel(id, {focus:false}) 用于 deleteSel；连续删框画面不跳。
- 大文件批量改（index.html/app.js，含 CRLF 与重复片段）**不要用多锚点 multi_edit**：一个 old_string 不匹配整批回滚，
  已连续失败 3 次。改用 python 脚本：读入 replace('\r\n','\n') → 标记/索引切割 → 写回 newline='\n'，并打印每个锚点的命中数。

## 硬约束（含原因）
- 样式：原纸色**矩形裁切**（不二值化）、紧框 + 约 10% 留白、编号 001.png 起。样式图 docs\style\。
- Windows python 默认编码 cp936：**任何 json/open 必须显式 encoding='utf-8'**，否则读回就 UnicodeDecodeError（踩过 json.dump 无 encoding）。
- tools.multi_edit 的参数是 edits[]{old_string,new_string}（不是 old/new），且**不允许 old==new**，否则整批拒绝且什么都不改。
- 用 run_code 写含反引号的文本会截断 JS 模板 → 代码/文档里别用反引号（改用「」）。
- 同一个 out 目录**禁止两个进程并发写同一页**：实测 36/520 张 tile 混入另一进程结果（尺寸与 index.csv 不符）。
  并行请用不同 out 根目录，或串行跑。
- 写 tiles 前必须先清旧 tiles（只删 ^\d+\.(png|jpg|webp)$ + index.csv/sheet.png，page.json/overlay.png 不动）：
  471→520 字覆盖时残留旧编号会让 index.csv/sheet 与文件不符（已踩）。

## 死路（不要重试）
- 固定阈值 x 投影分列：污渍/透字使走廊也非零 → 整页判成 1 列。改 find_peaks(prominence) + 谷底。
- 相邻 band 以「间隙<=6px 合并」：相邻 band 共享边界，gap 恒为 0 → 全页并成 1 列。
- 只按行 DP 不够：并排两字（批注区）要水平切口 → 已加 _post_split（w>1.55h 或 h>1.45H 时空带切）。

## 当前状态（已交付 2026-10-31）
- 成品：out/page1/ = 520 张彩色 tile + index.csv + sheet.png + page.json + overlay.png + qc/（列序=左→右，与样式图一致）。
- 精度旁证：与 segment_alt 匹配框 IoU 中位 0.991；行距≈260–320px（520 字/14 列 ≈37 字/列）；霉斑假框靠 core_frac/ink_frac 已剔。
- 残留：顶部横排批注 3–5 处连框；个别框含邻列伸出笔画；2–3 个极淡孤立笔画成框；倾斜 >1.5° 需 deskew（未内置）。
- 文档：README.md（总）、docs/CLI.md（参数）、docs/SPEC.md（契约）、docs/prior-art.md（先例）。
- tools/yi_core/{common,segment_v1,export,qc,segment_alt}.py 就绪；out/page1 已出 520 字。
- 待办：README（总说明）、右边缘两列并带(938px)修掉。CLI 已在原始 TIF 上跑通（见 docs/CLI.md；
  out/page1 与 out/page1_tif 均验证 520/520 tile 与 TIF 像素一致）。
- segment_alt 已完成：`out/alt/page_alt_full_{0.25,1.0}.json`（原图坐标）、`scratch/page_alt_0.25.json`（1/4 坐标，IoU 用）、
  `out/alt/overlay_alt_{full,native}.png`、说明 `out/alt/README-alt.md`。CLI 0.8 s(1/4) / 8.7 s(原生)。
- App 后端（task-5）就绪：`app/{store,jobs,pipeline,server,selftest_api}.py`；真 TIF e2e 84 PASS/0 FAIL（process 9.4 s→520 框，export 6.2 s→520 tiles），细节 `docs/APP-BACKEND.md`。自测必须先设 `YI_SEGMENT_HOME`=临时目录，勿碰真实 config。

## 当前状态（2026-10-31 晚 · 规范字校验版）
- 规范字库：data/glyphbank/bank.npz（1428 类 / 5716 张，5.85MB；git 里放 bank.npz.b64 文本版，yi_ref 两种都能读）。
  抽取脚本 scripts/build_glyphbank.py --pdf <手册.pdf>；来源 PDF 129 页 4x4 目录式。
- 评分：16x16 粗筛 top60 -> 48x48 余弦；实测本页 520 框 1.0s，中位 0.49，低分(0.38) 57 个 —— 低分框目视=两字粘连/碎片/霉斑。
- exe：dist/YiGlyphSplit.exe 119MB（含字库），14:29 构建；真机验证 window / health.glyphbank=true / refcheck 完成 / glyphref PNG。
- AI 复核已删（app/ai_refine.py、ai_selftest.py、_ui_mock.py 均删除；server 的 proposals/ai-refine/ai-test 路由与 settings.ai 一并删除）。
- 删框不再跳视角：setSel(id, {focus:false})。
