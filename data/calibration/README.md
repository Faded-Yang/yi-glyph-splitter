# 学习到的分割参数

这个目录里的 segment_params.json 是**程序在本机「一键学习」生成的**，属于每台机器自己的数据：

- 不随仓库分发、也不会被提交（见根目录 .gitignore）；
- 程序优先读取 %LOCALAPPDATA%\yi-segment\calibration\segment_params.json；
  这里只是从源码运行时的一个回退位置；
- 想恢复默认：删掉那个文件；
- 想重新学习：程序里点「一键学习」，或用 scripts/learn_from_correction.py。
