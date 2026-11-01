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

## 打包（2026-10-31 晚2：onedir + Inno Setup）
- 已从 onefile(119MB) 改为 **onedir + Inno Setup 安装包**：dist/YiGlyphSplit/{YiGlyphSplit.exe 9.7MB, runtime/ 286MB}；
  dist/YiGlyphSplit-1.0.0-Setup.exe ≈ 77.5MB（lzma2/ultra64）。
- spec：EXE(exclude_binaries=True, contents_directory="runtime") + COLLECT(name="YiGlyphSplit")；
  build.ps1 两步（pyinstaller -> ISCC），ISCC 路径 %LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe。
- Inno 6 未带中文语言：ChineseSimplified.isl 从 jrsoftware/issrc Files/Languages/ 取来放进编译器 Languages/（不在仓库）。
- **ISCC 编译约 2-4 分钟**（lzma2 ultra64 压 286MB）；前台跑 run_code 会 120s 超时 → 必须后台 job。
- 安装测试：setup /VERYSILENT /CURRENTUSER /DIR=... → 快捷方式 + 1878 文件 + 启动 OK + unins 卸载 OK。

## UI（2026-10-31 晚2）
- 左右侧栏可折叠/可拖宽：.main grid 用 --left-w/--right-w；splitter 绝对定位；localStorage 'yi.ui.layout'；
  Ctrl+B / Ctrl+Shift+B；双击 splitter 复位（266/352）。实测 grid 266/1040/352 -> 0/1306/352 -> 266/950/442。
- 新建框十字延长参考虚线：canvas.js _drawGuides()（armed 时画贯穿准线 + 锚点延长线）。
- 撤销掉色根因：normBox 丢掉 refLevel/refScore/refTop → 已保留 + restore 后 rebuildRefby()。

## 折叠侧栏的两个真 bug（2026-10-31 晚3，已修）
- bug1：隐藏侧栏用 display:none 会让后续 grid item 前移（stage 落到 0 宽列）→ 画布被挤成一条、右侧面板错位。
  修法：.main > .sidebar/.stage/.inspector 显式 grid-column/row 钉死位置（别依赖 DOM 顺序）。
- bug2：canvas._resize 先改了 this.cw/ch 再算中心点 → 折叠/展开后图像偏到一边（左侧留黑带）。
  修法：先用旧 cw/ch/scale/tx/ty 算图像中心，再更新尺寸并反算 tx/ty；首次布局走 reset 分支。
- 验证：1680x1050 与 250% DPI(2240 CSS) 下 hide-left 后 grid 0/flex/352、stage=剩余、图像居中；both-hidden 也正常。
- 注意：Chrome 复用同一 user-data-dir 会缓存 style.css，改完 CSS 测试要用新 profile，否则会误判没修好。

## 框编号（2026-10-31 晚4）
- canvas.js 按 (列中心x, y0, x0) 动态编号，全部框左上角画角标；setData 时重算(numMap/numCount/numDigits)。
- 与导出 visual 顺序**逐项一致**：实测 522 框画布序列 == index.csv 的 id 序列（0 mismatch）。
- 整页适配视图下只画 w>=30 且 h>=22 的框（选中框总画），否则 520 个角标糊成一片。
- app.js：右栏标题与状态栏改显示「第 N 框」（view.numOf），window.YI_VIEW 暴露给自动化测试。

## 分列（2026-10-31 晚5）
- bug：右边缘两列被并成一个 938px 宽列带 → 编号按列中心排序时两列交替（461,460,463,462…）。
- 修法1 自动：pipeline.refine_columns 把「宽 > 1.55×中位列宽」的带在带内墨量最深（否则中点）处切开；
  只改 page.json['columns']，不动 boxes/glyphs（人工框不丢），标记 columns_refined；页面打开时 ensure_columns 自动跑一次。
  实测 14 列(含 938) -> 15 列(462/482…)，每列内编号严格递增（0 处乱序）。
- 修法2 手动：顶栏「分列线」显隐分隔线（canvas._drawColumns + 顶部把手），拖边界改列（_down/_move 的 'col' 模式），
  松手 PUT /api/pages/{pid}/columns 保存并 columns_manual=true；「重置分列」= PUT {reset:true} 重新自动分列。
- **注意**：给 server.py 插入顶层 helper 时千万别插在 create_app 内部的注释锚点前 —— 会把函数体切断、create_app 返回 None。

## 版本/构建可见（2026-10-31 晚6）
- 起因：用户说分列线看不到，但同二进制在我这有 → 无法判断他跑的是哪一版，白折腾。
- 现在：窗口标题带 v1.0.1；设置 → 关于 显示「版本 x · 构建 时间」；/api/health 返回 build；
  本页信息多一行「分列 N 列（已显示/已隐藏分列线）」；设置里新增「显示分列线」复选框（与顶栏按钮同步）。
- build_stamp：frozen 取 sys.executable mtime，源码跑取 app/run.py mtime（app/buildinfo.py）。
- 分列线加粗（1.6px、alpha .9、虚线 7/5）+ 顶部编号把手；仍默认开启。

## 素材目录 + 从人工修正学习（2026-10-31 晚7）
- 10 页原始 tif 已归到 scans/（约 2 GB，.gitignore 里排除 scans/*.tif），scans/README.md 说明用途。
- scripts/learn_from_correction.py：读人工导出的 index.csv(gx0..gy1) -> 跑分割 -> IoU 匹配 -> 1/4 图爬山
  (cut_base/shape_w/ps_*/frag_merge_frac/ink_frac_max/core_frac_min) -> 全分辨率调 tall_split -> 写
  data/calibration/segment_params.json + 复制到 %LOCALAPPDATA%\yi-segment\calibration\。
- 新增 segment_v1: _split_tall_boxes + Params.tall_split/tall_split_ratio/tall_split_depth/tall_split_min_part
  （按行墨量谷底切「两字粘连」的高框，不要求有空隙）。
- 学习结果（0002_right，551 人工框）：F1 0.8385 -> 0.8582，recall 0.815 -> 0.878，precision 0.864 -> 0.839；
  参数 cut_base 0.6 / ps_w_ratio 1.7 / core_frac_min 0.22 / tall_split true / ratio 1.45。
  holdout 0003_left：746 -> 767 框（无膨胀）。pipeline 与 CLI 都会读校准文件；exe 内实测 577 框（默认 520）。
- 坑：pipeline 里用了 log 但没定义 logging.getLogger -> process 直接 NameError，已补。

## 一键学习（2026-10-31 晚8）
- 用户要求：学习能力进仓库，**学习结果不上传**；每次学习不要重打整包 → 做成程序内按钮。
- app/learn.py: learn_from_page(store,pid) 以 boxes.json 为标准答案，1/4 图粗搜 6 参数 + 整页 tall_split 试 2 组，
  写 %LOCALAPPDATA%\yi-segment\calibration\segment_params.json，pipeline.calibration_params() 优先读它 → 立即生效、无需重装。
  实测（exe 内）：F1 0.6434 -> 0.8405，27s；/api/learn 任务 + /api/calibration 查询。
- UI：顶栏「一键学习」（确认框）；设置 → 关于显示当前学习参数。
- 学习结果不再入库：data/calibration/*.json + REPORT.md 进 .gitignore，目录只留 README；打 exe 前删掉本机学习文件（否则会打进 runtime）。
- 扫描黑边：segment_v1._clear_dark_edge_bands（贴边、成条、墨密度>0.35 的列带直接清掉，参数 edge_clean/edge_band_frac/edge_band_ink）。
  0003_left 黑边假框 170+ -> 0；总框数 746 -> 563（人工 596）。
- 多页学习脚本：scripts/learn_from_correction.py --pair EXPORT:TIF（可重复）。3 页结果：mean F1 0.8001 -> 0.8281。
## 启动卡在「正在连接后端…」（2026-10-31 晚9）
- 真因：app.js 里 startLearn 的 confirm 字符串被上一轮 patch 写成了**跨行字符串**（JS 里的裸换行）→ 整个 app.js SyntaxError，boot 不执行，前端永远停在初始提示。
- 排查手段：加载 exe 的 http://127.0.0.1:<port>/ 用 CDP 听 Runtime.exceptionThrown → 直接拿到 SyntaxError；本地 node --check app.js 也能秒查。
- 修法：字符串里要换行就用 String.fromCharCode(10) 或 \n，别在 python 侧拼接时把换行落进 JS 字面量。
- **run_code 坑（已第 3 次踩）**：背包模板（反引号）里出现 Windows 路径 C:\Users\95959... 会被当成 \9 转义而整段解析失败（"\\8 and \\9 are not allowed in template strings"）。
  → 命令字符串里路径一律用正斜杠 C:/Users/...；或把内容先写成文件再执行。
## 分列线：拖动错位 & 加删线（2026-10-31 晚10）
- bug1（拖不动/闪到另一边）：page.json 的 columns 由 segment_v1 输出是**右→左**顺序，界面按左→右画/算，边界索引错位；
  修法：pipeline._sorted_columns 统一左→右并重排 idx（ensure_columns 出口）+ 服务端 _page_columns 也排序。
- bug2（抓不到想要的线）：_colBoundaryAt 原本取「6px 内第一条」，窄列两条线在屏幕上只差几像素 → 总抓错；
  修法：取最近的一条（bestD 初始 8），并列才取前的。
- 列宽：客户端 MIN_COL_W=90、新线两侧各 >=90；服务端校验 <60 直接拒。自动分列不再切出细条（min_part=max(140,0.45*median)）；
  ensure_columns 还做 _merge_slivers：<90px 的噪声列与较宽邻列合并（0003_left 19 列含 40px 细条 -> 16 列 min 213）。
- 加删线：双击分列线=删除；Shift+点击列内=加线（capture 监听 + stopImmediatePropagation）；右栏「+ 加分列 / − 删分列」按鼠标位置操作。
- 验证（打包 exe 内 CDP）：16 列时拖第 5 条只动该条（changed=[5]）；加线 16->17、删线 17->16、双击删 16->15。
## 分列线交互改版（2026-10-31 晚11）
- 用户批评：把鼠标移到线上再点右栏按钮做不到（只有一个鼠标）→ 改成**单击线选中（变黄）+ 按钮删除**。
- 用户要求：图片区域**双击不要有任何动作**（原 dblclick→resetView 已整个删掉；canvas 上已无 dblclick 监听）。
- 现交互：拖线=调边界；单击线=选中(selCol，黄色3px)；「− 删分列」删选中线；「+ 加分列」在有选中框时切该框所在列，否则切最宽列；Shift+点击列内=按点加线；「重置分列」重算。
- 实现：canvas _down 记录 x0/moved，_up 未移动则 selCol=idx 并回调 onColumnSelect；app 侧 S.colSel + updateColSelUi() 更新 #colSelInfo。
- 验证（打包 exe 内）：单击第 3 条选中(selCol=3)、删除 16->15、加分列 15->16、双击后 zoom/列数不变。
## 一键学习改为「累积学习」（2026-10-31 晚12）
- 用户问：提示「会覆盖现有学习结果」是不是只用这一次？→ 改为累积：每次学一页就把该页框登记成样本，参数用所有样本重算。
- 存储：%LOCALAPPDATA%/yi-segment/calibration/references/<页>.json（{tif, boxes}，最多 8 页）；segment_params.json 只是参数+统计，覆盖它不影响样本。
- 算法：以当前参数为起点；1/4 图 6 参数粗搜（目标=多页平均 F1）；整页阶段最多验 3 页、试 2 组 tall_split。
- 实测（TestClient，两页）：learn#1 pages=1 meanF1 0.829；learn#2 pages=2 meanF1 0.890，references 两个文件。
- API：/api/calibration 增加 pages_count/pages_used/mean_f1；确认框、toast、设置行显示累计页数与平均 F1。
- 坑：WMI Win32_Process.Create 启动 exe 不继承调用者改过的 $env:YI_SEGMENT_HOME（DCOM 用机器环境）→ 会写进真实 %LOCALAPPDATA%。隔离测试用 TestClient 或 Start-Process。
## 自建字库（2026-11-01 凌晨）
- app/userbank.py：%LOCALAPPDATA%/yi-segment/glyphbank/user.npz + user_meta.json；条目=(page,x0..y1,label,ink48)。
- yi_ref.match()：标准库 top + 自建库 top 合并；**分数/分级仍取标准库**（阈值按标准库标定）；自建候选带 src=user、count；自匹配用 exclude=(x0,y0,x1,y1) 排除，否则自己永远 1.0。
- check_boxes：自建库相似度 >=0.78 时把 low 提升为 warn（flag=userbank），避免整页手写体全红。
- 计数：每框 top[0] 的 label 在本页的出现次数 -> label_count / top[].page_count（UI「出现过 N 次」）。
- 收录：POST /api/pages/{pid}/harvest + 右栏「收录自建字库」；改过的页（框数≠自动）收全部框，否则只收 review==1；run_refcheck 开始时也自动 harvest。
- 打包：spec hiddenimports 加 app.userbank/app.yi_ref/app.learn/app.buildinfo（懒加载 import 静态分析可能漏）。
- 实测（exe 内，0003_left 594 框）：harvest 594；refcheck userbank=594；364 框最佳匹配为自建；user glyphref 200。
## 自建字库成为主得分（2026-11-01 凌晨2）
- 用户反馈：规范字库（印刷体）与手稿字体不搭，标准库分数不可信 → 得分 = max(标准库分, 自建库相似度 × 重复出现权重)。
- 自建库聚类：userbank.assign_clusters 在 48x48 特征上贪心聚类（CLUSTER_SIM=0.72，CLUSTER_VERSION=2，meta 存 cluster；旧库 load 时按版本自动重算）。cluster_sizes 即「同形出现过几次」。
- 权重：f = min(1, log2(1+size)/log2(7))，user_score = sim × (0.75+0.25f)；最终分 = max(std, user_score)；UI 显示「出现过 N 次（+0.xx）」。
- 实测（0003_left，594 字）：strong107/good338/warn142/low7（原 124/328/131/11），bonus 0~0.215；单页样本多数字形只出现 1 次，权重低——多收几页后共同字形成簇，分数会明显上来。
- 坑：assign_clusters 变量改名后残留 f16 → NameError（已修）；math 未在 yi_ref 顶层 import（已修）。
## 一键处理精度攻坚（2026-11-01 凌晨3，进行中）
- 评估骨架：`scripts/bench_segmentation.py --from-out [--scale 0.25] [--model]` —— 直接读 `out/<页>/index.csv`（只读）+ `scans/<页>.tif`，与手工框 IoU≥0.5 匹配，输出 P/R/F1 与错误分类（merged/spurious/oversplit/dropped/offset）。
- 参数搜索：`scripts/tune_segmentation.py`（坐标下降，目标=6 页平均 F1，0.25 图约 1s/页）。
- 手工字形模型：`scripts/build_manual_refs.py` → `%LOCALAPPDATA%/yi-segment/glyphbank/refs_manual.npz`（3379 字形，48/16 特征）+ `tools/yi_core/glyphmodel.py`（NN 相似度，按页排除自身）。
- 已上线改进（都写进 segment_v1.Params，可校准）：`_split_once` 行/列谷底补切（含 tight_box）、`_tighten_box` 收边界（去掉旁边墨点/邻居笔画尖角，offset 由 226→98）、`stain_filter`（凸包填充率>0.85 且 ink_frac>0.35 判污渍）、补切后丢「半截笔画」碎片。
- 6 页实测（0.25 图，控制变量）：基线 0.8465 → 0.8789（补切+污渍+收边+碎片）→ 0.8821（wide_split_ratio 1.70 / tall_split_depth 0.80）。全分辨率终版（已写入用户校准）mean F1 **0.8768**，extra 526 / missed 336。
- 死路①：**强制切高框**（h>1.55·med_h 无视谷底深度）→ 0.8786，比不加差；高框里混着真·高字。
- 死路②：**NN 相似度过滤**——TP 中位 0.61 / spurious 0.44，按 sim<0.45 丢会误杀 57 个真框只换 38 个假框；`model_refine.py` 在 0.25 图无净收益，暂不接入 pipeline（保留模块备用）。
- 硬约束：`out/` 只读（用户明确「可以读，不能写」）；手工框数量必须与 out 的 index.csv 一致（恢复页时用）。
- 坑：bench 里 0.25 图坐标要 /scale 才能和手工全分辨率框比（踩过，导致 IoU 全 0）；脚本必须把 `tools/` 加进 sys.path 才能 import yi_core。
## 已移除：动态学习（2026-11-01）
- 用户判断「一键学习」鸡肋（只调几个旋钮，无法实质提升），整条功能已删：按钮、/api/learn、/api/calibration、app/learn.py、spec hiddenimport。
- 分割参数仍可通过 %LOCALAPPDATA%/yi-segment/calibration/segment_params.json 覆盖（静态调优结果），但程序内不再有学习入口。

## 主程序：导入 out 框（2026-11-01）
- 顶栏「导入 out 框」：用 out/<页>/index.csv 的 gx0..gy1 覆盖本页框，自动向上找 out（也可在弹框填 out 目录），结果立即显示在画布。实测 0002_right 导入 551 框。

## 独立字库管理程序 glyphmgr（2026-11-01）
- 启动：python -m glyphmgr.run --port 9400（默认读仓库 out/），浏览器 http://127.0.0.1:9400/ 。
- 功能：8 页 4379 切图按字形相似度聚成类（0.64，2160 类，>=8 个的 69 类）；每类列出所有页+框号；选中框可横切/竖切（比例滑杆）、与上一/下一框合并、切开整类、撤销。
- 动作直接改写 out/<页> 的 PNG 与 index.csv：重新裁切（pad=max(6,0.1*max(w,h))，与导出器逐像素一致）、重新编号 001..N；改动前备份到 out/.glyphmgr/backup/<页>/<时间>/。
- 实测：0002_right 551 -> 切 552 -> undo 551 -> 合并 550 -> undo 551；未触碰的 0004_left 用同一公式重算 40 张 tile 与磁盘文件 0 差异。
- 坑：本机 OpenCV 的 imread/imwrite 对 TIF 和 PNG 都失败（can't open/read file）→ 一律用 PIL 读写（load_gray / Image.fromarray().save）。

## 模板似然 DP（2026-11-01）
- tools/yi_core/templatedp.py：8 页手工字形聚成 64 模板（scripts/build_templates.py -> %LOCALAPPDATA%/yi-segment/glyphbank/templates.npz），列带内 NCC 打分，DP=Σ字形似然+高度先验-切墨惩罚；Params.tpl_dp 默认 True，无模板自动回退。
- 实测（8 页、0.25 图、leave-one-page-out 模板）：0.8873 -> 0.8902（missed 431->380，extra 581->613）；提升有限，主要收益是漏切减少。
- 下一步（未做）：模板按真实笔画长宽比缩放、候选段归一化后余弦替代 NCC 滑窗、按页自适应权重。
## 拖框「断触」修复（2026-11-01 凌晨5）
- 症状：移动/拉框边时框突然不动，要反复扒拉几下才能继续。
- 根因（多条叠加）：①画布只监听自己的 pointermove/up，指针被系统或窗口外抢走后收不到后续事件；②WebView 对鼠标发 pointercancel 时旧代码把它当松手直接结束拖动；③拖动拿的是 box 对象引用，期间任何 view.setData 换成新数组就会改到孤儿对象；④抓在框控制点上时若附近有分列线，会先命中分列线（变成拖列，框看起来卡住）。
- 修法：window 级 pointermove/pointerup 兜底；lostpointercapture 后重新 setPointerCapture；pointercancel(pointerType=mouse) 不再结束拖动；_dragBox() 按 box id 每次重新取框；控制点命中优先于分列线；pointerdown preventDefault + CSS #cv{touch-action:none;user-select:none}。
- 验证（打包 exe 内 CDP）：拖动 5 步 x 120→550，中途注入 pointercancel 后继续 658→1088，移出画布仍跟随，松手 mode=idle，无 JS 异常。
## 修复：out/彝族扫描3-0002_right 被改成黑白且框变大（2026-11-01 下午）
- 原因（我在做 glyphmgr 测试时引入）：①重导出用 `Image.fromarray(灰度)` 保存 → 变成 8bit 灰度（原图是 24bpp RGB）；
  ②`Page.glyph_boxes()` 读的是 csv 的 x0..y1（**已经含 10% 留白的外框**），重导出又加了一次 pad → 框/图整体放大约 2 倍。
- 修法：`glyph_boxes()` 改用 gx0..gy1（紧框），新增 `tile_rects()` 供需要时使用；`reexport` 改读**彩色原图**（PIL RGB）裁剪保存，pad 只加一次；
  重新生成 sheet.png；清理旧 tile 只按 `^\d+\.(png|jpg|webp)$`，不再误删 sheet.png。
- 恢复：从 `out/.glyphmgr/backup/彝族扫描3-0002_right/20261101-042724/` 拷回原始彩色 tiles + index.csv + sheet.png（551 张，24bpp RGB，219x214）。
- 验证：未动过的 0004_left 用新公式重导出到临时目录，抽样 7 张与原图 **0 像素差**、RGB、sheet 正常；0002_right 已恢复为原样。

## 一键切图参数（2026-11-01）
- 弹框新增：**文件名前缀 / 起始编号 / 编号位数 / 是否生成 sheet.png / JPG 质量**（原有 留白比例、最小留白、编号顺序、图片格式、导出范围）。
- 默认值进设置（config.export + 设置抽屉），导出时用对话框里的值；`prefix=yi_, start=10, digits=4` 实测输出 `yi_0010.png ...`，551 张、csv 551 行。
- 导出自校验改成按“实际写出的文件名”核对（支持前缀），不再依赖 `^\d+\.png$`。

## glyphmgr 独立 exe（2026-11-01）
- 入口 `glyphmgr/app_entry.py`（自动找 out、自动选端口、自动开浏览器）；打包 spec `packaging/glyphmgr.spec` → `dist/YiGlyphMgr/YiGlyphMgr.exe`（6.6MB + runtime/）。
- 踩坑：windowed exe 里 uvicorn 默认日志格式化会崩（`NoneType.isatty`）→ devnull 兜底 + `log_config=None`；
  另外 classes.json 是 `{built,sim,pages,classes}` 结构，加载时必须取 `.classes`，否则 summary 把 dict 当列表（classes=4）并在 /api/classes 500。
- 实测：exe 启动后 summary 8 页/4379 图/2160 类，/api/classes 200，/api/class/36 返回 44 个成员。
## 一键处理参数进主程序（2026-11-01 下午）
- 设置抽屉新增「一键处理参数（分割器）」面板：cut_base / cut_w / shape_w、min_h_frac / max_h_frac / frag_merge_frac、
  两字粘连补切（开关 / 补切阈值 / 谷深阈值）、左右补切（开关 / 宽阈值）、收紧框边界、污渍过滤（开关 / 实心度 / 墨占比上限）、
  真墨下限、模板似然 DP 开关；带「保存处理参数」「恢复默认」。
- API：GET /api/segment-params（返回默认值+当前生效值+文件路径）、PUT（写 %LOCALAPPDATA%/yi-segment/calibration/segment_params.json，类型按 Params 字段强转）、POST /api/segment-params/reset（删文件恢复默认）。
- 保存后 pipeline.calibration_params() 立即读到 → 下次「一键处理 / 处理本页」生效，无需重启。
- 验证（打包 exe + CDP）：打开设置后 cut_base=0.9、tall_split_ratio=1.45、提示「当前参数已加载」；API 保存/复位实测通过。
## glyphmgr 空界面修复（2026-11-01 下午）
- 现象：双击 YiGlyphMgr.exe 后界面显示 0 页/0 类，因为旧逻辑只在 exe 同级与上一级找 out/；
  实际 out 在项目根（exe 在 dist/YiGlyphMgr/，中间隔了两层）→ 找不到。
- 修法：①自动检测改为「从 exe 目录和当前目录各向上找 5 层」的 out，且必须含 *\index.csv 才算；
  ②把选中的目录记到 %LOCALAPPDATA%/yi-segment/glyphmgr.json，下次直接用它；③界面顶部显示当前目录；
  ④找不到时显示输入条：粘贴 out 路径 →「加载这个目录」；新增 API GET/POST /api/root（POST 会重新加载并持久化）。
- 验证（exe 内）：从 dist/YiGlyphMgr 启动自动找到仓库 out（8 页/4379 图/2160 类）；POST /api/root 切换后 summary 同步，glyphmgr.json 记录路径。
## glyphmgr 手动分类 + 模型初分类（2026-11-01 下午）
- 手动分类：点框选中（Ctrl/Shift 多选）→「新建类并放入」/「并入左侧所选类」/「取消手动分类」；左侧类别显示【标签】；
  右侧可改类别标签、可把当前类整体并入左侧所选类（/api/label、/api/merge-classes）。
- 手动结果存 out/.glyphmgr/manual.json：{moves:{page|file -> classKey}, labels:{classKey -> label}, next_new}；
  build_classes 时以自动聚类为底、再叠加手动移动与标签，重算分类不会丢手动结果。
- 模型初分类：离线用规范字库（data/glyphbank/bank.npz，1428 类）给每个类取最多 3 张样本求平均墨图匹配，
  得分 >=0.42 的写建议标签（形如 100453（0.65））；已跑 2488 类 → 2213 类有标签，标签可手改。
- 打包：spec datas 增加 data/glyphbank（bank.npz 5.85MB），exe 总大小 192MB（_internal）。
- 验证（exe 内）：summary 10 页/2488 类/5462 图；类别列表首行显示【100453（0.65）】；选中框后 selInfo 显示「已选 1 个」；
  API assign/unassign/merge-classes/autolabel 均通过。
- 注意：glyphmgr 打开时首次 summary 可能需 5-15 秒（构建/加载 2488 类），前端等待即可。
## 视觉模型初分类：成本策略（2026-11-01 下午）
- 数据规模：5462 切图 / 2488 个自动类；类大小长尾（top64=18%、top256=40%、top512=55%、top1024≈72%）。
- 选择：**按覆盖优先**，只让视觉模型看 top512 类（占 55% 字形），其余长尾类（n≤2）用离线聚类 + 最近标签传播，不花 API。
- 图规格：**8×8=64 格/张、128px/格、1024×1024**（对 1024px 上限的模型也安全；格内标签写 G<序号> n=<次数>）。
- 预算：top512 → **8 张图 = 8 次识图调用**；每个子代理最多 2 张；一次跑完整个库。
- 若模型支持 2048px：可改 10×12=120 格/张（~170px/格）、1600×1600 → top512 只要 5 次调用。
- 缓存：结果写 out/.glyphmgr/manual.json（moves/labels），重跑时跳过已标注类，**重复分析 0 成本**；
  子代理返回的组表用 scripts/apply_vision_groups.py 落地（合并类 + 标记「疑似两字/碎片」）。
- 实测：2 张图（240 类）→ 68 组合并、2488→2420 类、0 失败。
