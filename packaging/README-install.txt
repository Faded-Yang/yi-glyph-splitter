彝文切字工具 · 安装说明
======================

安装位置
--------
默认装到 %LOCALAPPDATA%\Programs\YiGlyphSplit（不需要管理员权限）。
如果你想装到 D 盘，安装向导第一步把目录改成 D:\Apps\YiGlyphSplit 之类即可。

安装目录结构
------------
  YiGlyphSplit.exe      启动器（约 10 MB），双击它就能用
  runtime\              Python 运行时 + OpenCV / numpy / scipy 等依赖（约 286 MB）
  unins000.exe          卸载程序

数据位置（不会被卸载删除）
--------------------------
  %LOCALAPPDATA%\yi-segment\        程序设置
  %LOCALAPPDATA%\yi-segment\projects\  你的项目（页面、框、校验结果）

第一次使用
----------
1. 双击桌面的「彝文切字工具」（或开始菜单里的同名项）
2. 「导入 TIF」→ 选一个项目目录（建议单独建一个，存放中间数据）
3. 「一键处理」→ 自动框选 + 自动规范字校验
4. 检查颜色（绿=很标准，红=可疑），必要时改框 / 删框 / 手动画色
5. 「一键切图」→ 输出 001.png…、index.csv、sheet.png

卸载
----
开始菜单 →「卸载 彝文切字工具」，或者设置 →「应用」里找到它。
用户项目数据在 %LOCALAPPDATA%\yi-segment，需要手动删除。
