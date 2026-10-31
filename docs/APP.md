# 桌面程序使用说明（YiGlyphSplit.exe）

## 四步流程

1. **导入**：点「导入 TIF」（可多选）或「导入文件夹」（批量扫描 *.tif / *.tiff）。
   第一次使用会先让你选一个**项目目录**（存放 page.json、预览图、人工修改记录；建议单独建文件夹，不要放在源图目录里）。
2. **一键处理**：选中页（或「全部」）→「一键处理」。程序只在页面上**框出每个字**，不会立刻切图。
   进度条走完即可看到整页叠加框；单页约 13 秒（6300×11000 的扫描）。
3. **（可选）AI 复核**：先点「测试连接」（会发一张带红框的测试图，确认视觉接口与思考模式设置都对），再填 DeepSeek API Key（base_url 默认 https://api.deepseek.com，模型 deepseek-flash）→
   「AI 复核」对全部框或只对「可疑框」跑一遍。AI 只产出**建议**（紫框），不会直接改你的框；
   在右栏逐条接受/拒绝，或「全部接受」。没有 Key 时可先看流程（会走 mock，理由带 [mock]）。
4. **人工复核**：拖动/缩放/新增/删除/拆分/合并/确认/存疑；右栏有 3 倍细节图（含上下邻字上下文）。
   所有改动自动保存（400ms 防抖），右上角提示「已保存 N 框」。
5. **一键切图**：选导出目录 → 输出 0001.png…、index.csv、sheet.png；可直接用于后续 OCR/建库。

## 快捷键

| 键 | 作用 |
|---|---|
| 滚轮 / 空格+拖动 | 缩放 / 平移 |
| 点击框 | 选中；拖动内部移动；拖动 8 个控制点缩放 |
| A 再框选 | 新建框 |
| Del | 删除选中框 |
| S | 以鼠标当前 y 把选中框一分为二（竖排最常用） |
| M / Shift+M | 与下一框 / 上一框合并 |
| 1 / 2 / 0 | 标为确认 / 存疑 / 复位 |
| ← / → | 上一个 / 下一个框 |
| Ctrl+Z | 撤销（50 步） |

## 命令行（高级）

    YiGlyphSplit.exe [--project D:\work\book1] [--port 8765] [--browser] [--debug]

日志：%LOCALAPPDATA%\yi-segment\logs\app.log
配置：%LOCALAPPDATA%\yi-segment\config.json（含 API Key，明文存本机；不要提交到 git）

## 项目目录结构

    <项目>/
      project.json
      pages/<id>/
        page.json      自动分割的原始结果
        boxes.json     你人工修正后的框（导出用这个）
        proposals.json AI 建议
        preview.png    缩略图（无框，前端画框用）
      export/<页名>/   一键切图的产物

## 无界面 / 批量

不想点界面时仍可用命令行工具：
    python tools\yi_segment.py --input <TIF或目录> --out out\batch --jobs 4 --resume
详见 docs/CLI.md。
