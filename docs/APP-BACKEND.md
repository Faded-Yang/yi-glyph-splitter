> **2026-10-31 更新**：AI 复核（DeepSeek 视觉）整条功能已移除，UI/后端路由/设置项都不再存在；替代品是离线的「规范字校验」（app/yi_ref.py + data/glyphbank）。本文档中与 AI 相关的段落仅作历史参考。

# App 后端（task-5 / app-api）实现说明

代码：`app/store.py` `app/jobs.py` `app/pipeline.py` `app/server.py` `app/selftest_api.py`
（依赖 Lead 的 `paths.py` `config.py` `run.py` 与 tools/yi_core，均未改动）

## 契约要点（与 SPEC-APP.md 的差异/补充，已通知 app-ui / app-ai）

- 静态：`app.paths.static_dir()` 挂在 `/`（StaticFiles html=True），/api 路由先注册；未知 /api/* 返回中文 JSON 404。
- 页状态字符串：pending / processed / reviewing / done / error（PUT boxes 时按 review 自动派生：全 1→done，有 1/2→reviewing）。
- preview.png 只放页面缩略图（宽 1600，?w= 可缩放），框由前端自己画；`boxes=0` 参数忽略不报错。
- PUT boxes：body 可为数组或 {boxes:[]}；按发送顺序重编号 id=1..N，返回 {boxes,warnings,status,reviewed,n_boxes}。
- proposals 统一 {index,box_id,action,before:{x0..},after:[{x0..}],confidence,reason,status}；apply_all 只处理 pending。
- 导出服务端兜底参数恒为 app/config.DEFAULT_EXPORT（0.10/6/visual/png），不读残留 config；`/api/export` 与
  `/api/pages/{pid}/export` 同一实现（pid 可为 "all"），每页写 `<out>/<页名>/{tiles,index.csv,sheet.png}`，导出前清旧
  tiles、导出后校验 tiles 数 == index.csv 行数；同页并发导出用进程内目录锁拒绝。
- 任务：{state:running|done|error|canceled, done,total,message,errors[],results[],current{pid,frac,stage}}；
  process/export total=页数；ai-refine total=目标框数（回调里 total 只增不减）。
- AI 模块缺失时 /api/pages/{pid}/ai-refine 任务报中文错误、proposals 接口 ai_available=false，服务不崩。

## 自测结果（2026-10-31）

- 单元：`python -m app.store --selftest` 21 PASS；`app.jobs` 12 PASS；`app.pipeline` 20 PASS（1/4 灰度缓存 493 框 +
  导出）；`app.server` 30 PASS。
- 真 TIF 全链路：`python -m app.selftest_api` → 84 PASS / 0 FAIL / 0 SKIP，40.3 s；process 9.4 s（load 0.27 /
  segment 7.77 / preview 0.82，520 框），export 6.2 s / 520 tiles，cancel 7.7 s（segment 不可中断，取消在分段结束后
  生效），ai-refine mock 0.5 s。
- 几何与 CLI 成品一致：导出 index.csv 首行 gx0/gy0/gx1/gy1 与 out/page1/index.csv 完全相同（103/434/193/478）。

## 环境纪律

- 所有自测在 import app.* 之前设 `os.environ["YI_SEGMENT_HOME"]` = 临时目录，不碰
  %LOCALAPPDATA%/yi-segment/config.json（实测跑完 mtime 不变）。
- 曾污染真实 config（pad_ratio=0.5/order=reading/last_project=临时目录），已修回默认值并清空 last_project。

## 已知限制

- segment_page 是单次 C 调用，取消要等它返回（≤10 s）；任务进度在页粒度，页内只有 current.frac。
- preview.png 1600 宽约 8 MB（本地回环无妨）；需要更省可改 JPEG 或降宽。
- 导出文件名位数按 export.py 规则 = max(3, 位数(总数))，520 框 → 001.png（与用户样式图 001 起一致）。
