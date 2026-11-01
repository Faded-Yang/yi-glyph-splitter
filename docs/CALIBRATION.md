# 从人工修正学习分割参数

单页：

    python scripts/learn_from_correction.py --pair "out/<页>:scans/<页>.tif"

多页（推荐，泛化更好）：重复 --pair 即可。

学习结果写到 data/calibration/segment_params.json，并复制到 %LOCALAPPDATA%\yi-segment\calibration\；
pipeline 与 CLI 会自动读取。删除该文件即恢复默认参数。
