> **2026-10-31 更新**：AI 复核（DeepSeek 视觉）整条功能已移除，UI/后端路由/设置项都不再存在；替代品是离线的「规范字校验」（app/yi_ref.py + data/glyphbank）。本文档中与 AI 相关的段落仅作历史参考。

# App 前端（app/static）实现说明 —— app-ui

## 交付文件
- app/static/index.html   结构（顶栏 / 左栏页列表 / 中栏画布 / 右栏细节+AI+本页 / 设置抽屉 / 4 个弹层 / toast）
- app/static/style.css    深色 token 化样式（无外链字体，`Segoe UI` → `Microsoft YaHei UI` 回退）
- app/static/api.js       fetch 封装：非 2xx 统一取 `{detail}` 中文消息抛 Error
- app/static/canvas.js    自绘标注画布（缩放/平移/命中测试/8 控制点/框选新建），零依赖
- app/static/app.js       主逻辑（状态、页列表、右栏、AI 建议、任务轮询、设置、快捷键、防抖保存）
- app/_ui_mock.py         开发期假后端（**不参与交付运行**）：合成纸面图 + 93 框/页 + 假任务/AI 建议；
  `python -m app._ui_mock --serve --port 8781` / `--selftest`（17 检查全 PASS）

## 与后端的接口用法（按 app/SPEC-APP.md + app-api 定稿）
- GET /api/state → pages[].{id,name,src,status,status_label?,n_boxes,reviewed,error} + settings{ai{configured,api_key(打码),base_url,model,max_workers},defaults{pad_ratio,min_pad,order,fmt}}
- GET /api/pages/{pid} → {page{w,h,columns,source}, boxes[], status, status_label?, ai_available, n_proposals}；选页后用它的 boxes 数与 status 覆盖左栏摘要（修过「左栏 0 框 / 未处理」不同步）
- GET /api/pages/{pid}/preview.png?w=..&boxes=0 —— **前端自己画框**；首屏 w=min(2000,页宽)，放大到 figure 像素比 >0.5 时自动升级 w=3200
- GET /api/pages/{pid}/crop?x0&y0&x1&y1&scale —— 细节图，scale=clamp(300/裁宽,1,3)
- PUT /api/pages/{pid}/boxes {boxes:[{x0,y0,x1,y1,flags,review,note}]}（400ms 防抖；返回顺序=提交顺序，id 1..N，按位置还原选中）
- POST /api/pages/{pid}/proposals {apply:[index],reject:[index],apply_all}（用返回里的 proposals[].index）
- POST /api/pages/{pid}/ai-refine {ids: "all"|"suspect"|[boxId], mode}
- POST /api/pages/{pid}/export {out,pad_ratio,min_pad,order,fmt}；多页导出=逐页串行 POST（每页一个 job）
- GET /api/jobs/{id} 500ms 轮询：done/total 与 current{pid,frac,stage} → 顶部进度条 + 行内进度 + 取消
- GET /api/dialog/files|folder|export_dir；POST /api/pages/add|process；DELETE /api/pages/{pid}；POST /api/settings|ai/test|project/open

## 交互清单（headless Chrome + CDP 实测，scratch/ui_shot.cjs，36 项全 PASS）
点选 / 拖动 / 8 点缩放 / A 拖框新建 / Del 删除 / S 以鼠标 y 拆分（鼠标不在框内退化为中线并 toast） /
M 与下框合并 / Shift+M 与上框合并 / 1 确认 / 2 存疑 / 0 复位 / ←→（↑↓ 同义）切框并自动居中 /
滚轮以鼠标为中心缩放 0.2–5x（显示为相对适应窗口的百分比） / 空格或中键拖动平移 / F 或双击适应窗口 /
Ctrl+Z 撤销（50 步，内存） / Ctrl+Shift+Z 或 Ctrl+Y 重做 / Ctrl+S 立即保存 / Esc 取消新建或取消选中 /
输入框聚焦时不触发快捷键 / 400ms 防抖保存 + toast「已保存 N 框」+ 失败显示后端 detail /
左栏状态点+筛选+全选+勾选移除 / 任务进度条与行内进度与取消 / 设置抽屉（含测试连接） / 导出弹层 / AI 建议逐条与全部接受。

## 已知限制（文档可直接照抄）
1. 撤销/重做仅在内存，切页或刷新即失效；服务端无 undo。
2. 单选模型：没有「框选多个框批量操作」；左栏「全选」是页级勾选，不是框级。
3. 保存后后端按数组顺序把框 id 重编为 1..N，右侧「#id」是页内序位而非持久 id（选中项按位置保持）。
4. 保存后不自动重拉 preview.png（避免与自绘框重影）；需要看后端烤框结果用「重新载入」。
5. 合并/切框顺序按 page.columns 的列序→行序；后端未给 columns 时退化为按 x 分桶再按 y。
6. 导出为逐页串行任务（每页一个 job），顶部进度条只显示当前页；全项目导出未用 pid="all" 的 ids 形式。
7. AI 复核依赖后端 ai_refine：ai_available=false 时右栏显示不可用文案，按钮仍可点（会就后端中文 detail 弹 toast）。
8. 状态筛选只有 未处理/已处理/复核中/已完成 四档；error 页显示红点但不进筛选列表。
9. 无 URL 深链、无多窗口、无框旋转/吸附/对齐（规范未要求）。

## 开发/自测资产（scratch，临时）
- scratch/ui_shot.cjs   CDP 驱动的 36 项交互回归 + 8 张过程截图（node ui_shot.cjs）
- scratch/ui_final.cjs  生成 scratch/ui_selected.png 与 scratch/ui_settings.png
- scratch/ui_shots/     过程截图（01_main…08_after_job）
- 代理需 `python -m app._ui_mock --serve --port 8781` 起假后端；截图用 chrome --headless=new + `--remote-debugging-port` 走 CDP（Playwright 不可用）。

## 踩过的坑（建议进台账）
- 用 run_code 写文件时正文里不能出现反引号（会截断 JS 模板）；JS 一律字符串拼接，不用模板字面量。
- CSS 类名跨模块撞名会静默毁样式：`.sel`（下拉框）与 `.ov-box.sel` 撞，把细节图里的选中框当成了深色块；用 `.mini-sel` 区分。
- 细节图叠加框必须与 `<img>` 同容器（inline-block 包裹），否则 img 在 flex 居中时坐标基准错位（实测偏 24px）。
- headless 交互测试里 `Input.dispatchMouseEvent` 用的是**视口坐标**，画布内部坐标要加 canvas.getBoundingClientRect 偏移。
- 保存是异步的：测试在两帧内连发方向键会撞上后端重编号，需等 `!state.dirty` 再断言 id。