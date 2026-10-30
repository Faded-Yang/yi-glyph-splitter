# 彝文切字工具 · 桌面 App 规格（app/SPEC-APP.md）

目标：把现有 CLI 包成**本地桌面程序**（Python 服务 + 浏览器 UI，最终 PyInstaller 打成单文件 exe），
流程严格按用户要求四步：

1. **导入**：选 TIF（可多选）/ 选文件夹批量导入 → 列表
2. **一键处理**：跑现有分割 → 只在页面上**画框预览，不切图**
3. **（可选）AI 复核**：接 DeepSeek 视觉模型，对可疑/全部框给出修正建议 → 用户逐条接受/拒绝
4. **人工复核**：可拖拽/缩放/新增/删除/拆分/合并/确认/忽略，改完落盘
5. **一键切图**：按当前框裁图 + index.csv + sheet.png，导出到目标目录

## 目录

    app/
      run.py           # 入口（Lead）：起 uvicorn + 自动开浏览器；--port --project --no-browser
      config.py        # 配置读写（Lead）
      paths.py         # 资源路径（PyInstaller _MEIPASS）与数据目录（Lead）
      server.py        # FastAPI 路由（app-api）
      store.py         # ProjectStore：project.json / pages/<pid>/{page.json,boxes.json,preview.png}（app-api）
      jobs.py          # JobManager：后台线程任务 + 进度（app-api）
      pipeline.py      # 封装 segment_page / 画预览 / 导出（app-api）
      ai_refine.py     # DeepSeek 视觉复核（app-ai）
      static/index.html app.js style.css   （app-ui）

## 磁盘数据模型

    <project>/
      project.json     {"version":1,"pages":[{"id","name","src","status","error","width","height","n_boxes","reviewed","ai_refined"}]}
      pages/<pid>/
        page.json      分割原始结果（schema 同 docs/SPEC.md，只写一次，不再改）
        boxes.json     人工/编辑后的权威框 {"boxes":[{"x0","y0","x1","y1","flags":[],"review":0,"note":""}]}
        preview.png    缩略页 + 画框（缓存，box 变更时重建）
        proposals.json AI 建议（不自动应用）

**规则**：导出时优先 boxes.json；不存在则用 page.json 的 glyphs（review=0）。坐标一律原图像素，x1/y1 开区间。

## REST API（前缀 /api，全部 JSON，除图片端点）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | /api/state | {project, pages[], settings{ai{configured,model,base_url},defaults}} |
| GET  | /api/dialog/files | 服务端弹原生多选对话框（tkinter）→ {paths[]}；支持 .tif/.tiff |
| GET  | /api/dialog/folder | 选文件夹 → {path} |
| GET  | /api/dialog/export_dir | 选导出目录 → {path} |
| POST | /api/pages/add | {paths[]} 或 {folder,pattern} → {added[],pages[]}（按真实路径去重） |
| POST | /api/pages/process | {ids[]|"all"} → {job_id}（后台：读灰度→segment_page→boxes→preview） |
| DELETE | /api/pages/{pid} | 从项目移除（不删源文件） |
| GET  | /api/jobs/{jid} | {id,kind,state,done,total,message,errors[],results[]}；state=running|done|error|canceled |
| POST | /api/jobs/{jid}/cancel | 取消 |
| GET  | /api/pages/{pid} | {page{w,h,columns[]},boxes[{id,x0,y0,x1,y1,flags,review,note}],preview_url} |
| GET  | /api/pages/{pid}/preview.png?w=1600 | 缩略页+框 |
| GET  | /api/pages/{pid}/crop?x0=&y0=&x1=&y1=&scale=2 | 原图裁块（编辑器细节视图用） |
| PUT  | /api/pages/{pid}/boxes | {boxes[]} 整表替换 → 校验、重编号、写 boxes.json、重建 preview → {boxes[],warnings[]} |
| POST | /api/pages/{pid}/ai-refine | {ids[]|"all"|"suspect", mode} → {job_id}；只写 proposals.json，**不改框** |
| GET  | /api/pages/{pid}/proposals | {proposals[{box_id,action,before,after,confidence,reason,status}]} |
| POST | /api/pages/{pid}/proposals | {apply:[i],"reject":[i],"apply_all":bool} → {boxes[]} |
| POST | /api/pages/{pid}/export | {out,pad_ratio,min_pad,order,fmt} → {job_id}；写 <out>/<pagename>/{0001.png..,index.csv,sheet.png} |
| POST | /api/settings | {ai{api_key,base_url,model,max_workers}|null, defaults{}} → {ok}；GET 时 api_key 打码 |
| POST | /api/ai/test | 凭据自测 → {ok,message} |
| POST | /api/project/open | {path} 打开/新建项目 → {ok,state} |

错误统一：HTTP 4xx/5xx + {"detail": "中文说明"}。

## 前端行为（app-ui）

- 顶栏：项目名 · 「导入 TIF」「导入文件夹」「一键处理」「AI 复核」「一键切图」「设置」
- 左栏：页列表（文件名 / 状态色点：未处理灰、已处理蓝、复核中黄、已完成绿 / 框数 / 进度条）；支持全选、按状态筛选
- 中栏：画布（缩略页+框，可缩放 0.2–5x、平移）；框颜色：普通青、已确认绿、存疑琥珀、AI 建议紫、选中红（带 8 控制点）
- 交互：点选；拖动移动；8 点缩放；Del 删除；A 新建框（在画布上框选）；S 以当前鼠标 y 为界把选中框一分为二；M 与下一框合并、Shift+M 与上一框合并；1 确认、2 存疑、0 复位；←/→ 上一个/下一个框；滚轮缩放；空格拖动平移；Ctrl+Z 撤销（前端保留 50 步）
- 右栏：选中框的 3x 细节图（含上/下邻框上下文，参考 docs/style 之外的截图风格）+ 坐标数字输入 + 操作按钮（确认/存疑/删除/拆分/合并/添加）+ AI 建议列表（逐条接受/拒绝、全部接受）
- 保存：任何改动 400ms 防抖 → PUT /boxes；右下角 toast「已保存 N 框 / 失败」
- 任务进度：500ms 轮询 /jobs，顶部进度条 + 每页行内进度；可取消
- 中文界面；无构建步骤（原生 JS，不引 CDN，离线可用）

## AI 复核（app/ai_refine.py）

接口：

    @dataclass
    class AIConfig: api_key: str; base_url: str = "https://api.deepseek.com"; model: str = "deepseek-flash"; max_workers: int = 4; timeout: float = 60
    def refine_boxes(page_rgb: np.ndarray, boxes: list[dict], *, ids: list[int]|None = None, mode: str = "all",
                     cfg: AIConfig, progress_cb=None, cancel_cb=None) -> list[dict]

- 每个目标框：裁上下文（上下各扩 0.9h、左右各扩 0.5w，clamp），在裁块上把**当前框**画 3px 红框；
  放大到目标字高 ≥ 240 px；JPEG q≈88；base64 data URL；随 prompt 要求**只回 JSON**：
  {"action":"tighten|split|merge|keep","bboxes":[[x0,y0,x1,y1],...],"confidence":0..1,"reason":"<=20字"}
  坐标为**归一化 0-1000（相对裁块）**；服务端换算回原图坐标。
- 校验：与 before IoU>0.2、尺寸比 0.4–2.5、框在图内、split 结果 2 个且不重叠 → 否则 action=keep。
- 并发 ThreadPoolExecutor(max_workers)，429/5xx 重试 2 次（指数退避），cancel_cb 每框检查。
- 无 key / mock 模式：返回确定性合成结果（tighten by 2% inbox），并在 reason 标 "[mock]"。
- test_connection(cfg)：发一句最小文本请求，返回 {ok,message}。

## 打包（Lead）

PyInstaller --onefile --noconsole --add-data "app/static;app/static"，入口 app/run.py；
端口默认 127.0.0.1:8765（占用则自增）；启动后自动开默认浏览器；日志写 %LOCALAPPDATA%/yi-segment/logs/app.log。
命令行参数：--project PATH、--port N、--no-browser、--debug。

## 纪律

- 只用已有依赖：fastapi/uvicorn/openai(2.32)/numpy/opencv/scipy/PIL/requests。**不要**装 Flask/PyQt/Electron。
- 不要改 tools/ 下已有模块（segment_v1/export/qc 只 import 复用）。
- 全部路径要兼容 PyInstaller（静态文件用 app.paths.resource_path）。
- 每个模块自带 CLI 自测入口（python -m app.xxx --selftest）。
