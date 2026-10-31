# 从人工修正学习到的分割参数

- 学习页：彝族扫描3-0002_right.tif
- 人工框数：551
- 学习前：F1 0.8385（precision 0.8635 / recall 0.8149，IoU 中位 1.000）
- 学习后：F1 0.8582（precision 0.8388 / recall 0.8784，IoU 中位 1.000）
- 参数：`{"cut_base": 0.6, "ps_w_ratio": 1.7, "core_frac_min": 0.22, "tall_split": true, "tall_split_ratio": 1.45}`
- 几何偏差（人工相对自动）：dx 0.0 px, dy 0.0 px, 宽比 1.000, 高比 1.000

`pipeline.process_page()` 会自动读取 `data/calibration/segment_params.json`。
