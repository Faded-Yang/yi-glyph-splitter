# 项目结束说明（PROJECT-ENDED）

本项目（彝文古籍整页切字 + 桌面工具 + 字库管理）于 **2026-11-02** 终止。

## 交付过的成果

| 部分 | 位置 | 状态 |
|---|---|---|
| 分割算法/核心库 | `tools/yi_core/`（segment_v1、templatedp、export、qc、segment_alt、glyphmodel） | 已入库 |
| 桌面程序（一键处理/复核/切图/规范字校验） | `app/` + `packaging/app.spec` + `packaging/installer.iss` | 源码已入库，安装包未上传（体积 78 MB） |
| 字库管理程序 glyphmgr | `glyphmgr/` + `packaging/glyphmgr.spec` | 源码已入库 |
| 评测/调参/细化脚本 | `scripts/`（bench_segmentation、tune_segmentation、ml_cluster、refine_all、rebuild_out_from_original 等） | 已入库 |
| 印刷规范字库 | `data/glyphbank/bank.npz.b64`（1428 类 / 5716 字） | 已入库 |
| 文档 | `README.md`、`docs/`（CLI、SPEC、先例、校准、APP 后端/前端） | 已入库 |
| 台账 | `AGENTS.md`（含全部已验证事实、死路、约束） | 已入库 |

## 关键指标（留在台账里的结论）

- 分割：全分辨率 6 页手工框 mean F1 **0.8768**；模板似然 DP LOSO 提升 +0.003（0.8873 → 0.8902）。
- 字库：5462～6024 张切图，自动聚类 2488 类 → 视觉合并 + 小类合并 + 错分清洗后约 440 类（ML 层次聚类 base 827 类）。
- 切图：全部为**原图原分辨率裁切**（与 TIF 逐像素一致），每页保留原有留白比例。
- 桌面程序：onedir + Inno Setup 安装包；`YiGlyphSplit.exe` 9.8 MB + runtime ≈ 286 MB，安装包 ≈ 77.6 MB。

## 本地已清理

项目终止后，本地删除了：`out/`（切图，约 1.7 GB）、`scans/`（原始 TIF，约 2 GB）、`dist/`（构建产物）、`build/`、各缓存与临时文件，以及 `%LOCALAPPDATA%\yi-segment` 下的项目数据与字库缓存。

原始扫描件与切图数据**不在本仓库**（.gitignore 排除），仓库只保留源码、脚本、文档、台账与印刷字库。

## 重建方式

```bash
pip install -r requirements.txt
python -m app.run --browser            # 桌面程序（源码运行）
python -m glyphmgr.run --port 9400     # 字库管理程序
python -m PyInstaller packaging/app.spec        # 打包 exe
python -m PyInstaller packaging/glyphmgr.spec
```

## 许可

见 `LICENSE`。
