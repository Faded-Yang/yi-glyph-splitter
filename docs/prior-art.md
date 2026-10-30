# 先例调研：古籍二值化与竖排手写字切分（prior art）

> 全部结论来自实跑，不是阅读清单。复现脚本：`tools/vendor/prior_probe.py`、`tools/vendor/doxa_compare.py`；
> 数据 `scratch/prior/probe_stats.json`、`scratch/prior/doxa_compare.json`；对比图 `scratch/prior/morph_compare.png`、`doxa_{faint,stain,paper}.png`。
> 网络实测：github.com 可达但每次 ~30s；api.github.com 的 repos 端点可用，**search 端点已被出口 IP 限流**；
> **raw.githubusercontent.com 30s 超时（拿不到源码）**；huggingface.co 直连不通，**hf-mirror.com/api 可达**。

## 一句话结论

**主二值化不换**（我们 flatten + Sauvola(k≈0.34, win=61) 在霉斑上比 Doxa 全套默认参数都干净）；
**字切分没有任何现成实现可抄**，断笔合并靠形态学解决不了（实测上限 ~28%），必须走 DP/图聚类；
**唯一值得抄的是 Doxa 里 SU 的局部自适应阈值公式**（CC0 公共域），用来做"霉斑/透字 veto 掩膜"。
深度学习路线（Doc-UFCN / DeblaNet / onnx 模型）在本机条件下全部不可行，理由见下。

## 1. 二值化：Doxa / doxapy（可用，CPU 离线，CC0）

* repo: `brandonmpetty/Doxa`（C++，19 种局部自适应算法 + DIBCO 指标）；python 轮子 PyPI `doxapy`，**License CC0-1.0**。
* **版本坑（关键）**：0.9.4 起只发 `cp312-abi3` 轮子；本机 py3.10 只能装 **0.9.2**（有 cp310-win_amd64，116 KB，依赖仅 numpy）。
  已实测安装：`python -m pip install --no-deps doxapy==0.9.2` ✅，无 torch。
* **API 坑（关键）**：`to_binary` 是**原地写**、返回 None，且 **0=墨 / 255=纸**（反的）：

```python
import doxapy, numpy as np
b = doxapy.Binarization(doxapy.Binarization.SU)   # SU / ISAUVOLA / NICK / SAUVOLA / WAN / GATOS
b.initialize(gray_u8)                              # uint8 2D，建议先过 common.flatten_illumination
out = np.zeros(gray_u8.shape, np.uint8)
b.to_binary(out, {"window_radius": 40})            # 原地写，返回 None
ink = (out == 0)                                   # 直接 out>0 会得到"87% 是墨"的荒唐结果
```

### 实测对比（同一张图的 3 类 512×512 tile，统一 flatten 预处理；"纸面 tile"= 全页最空的一块，测误判）

| 指标 | ours k.34 | ours k.25 | doxa SAUVOLA | SU | ISAUVOLA | GATOS | WAN |
|---|---|---|---|---|---|---|---|
| 纸面误判 ink（center/right） | .063 / .025 | .067 / .054 | .074 / .086 | **.037 / .004** | .067 / .010 | .059 / .052 | .092 / .140 |
| 霉斑区 碎片数 n_cc | 30 / 77 | 80 / 108 | 122 / 136 | **18 / 77** | 32 / 53 | 24 / 56 | 190 / 164 |
| 淡笔画 碎片数 n_cc | 30 / 68 | 80 / 155 | 122 / 244 | 18 / **475** | 32 / 244 | 24 / 190 | 190 / 389 |

* **目视**（`scratch/prior/doxa_faint.png`，淡字+斜向霉斑）：doxa 全系默认参数在霉斑处满屏散点，我们的 Sauvola 明显更干净。
  原因两条：doxapy 默认 `window_radius`≈15（31px）远小于我们的 61；且它不做匀光前置（我们做了）。
* **SU 的两面性**：纸面误判最低（0.4%–3.7%，我们 2.5%–6.3%），但换成淡字 tile 会碎成 475 个碎片 → **不能逐像素替换**，只适合当区域级"这里到底有没有墨"的判据。
* **吞吐（按 512² 中位耗时 ×27.6 外推 70MP 整页）**：SAUVOLA 0.05s、ISAUVOLA 0.24s、SU 0.66s、WAN 1.85s、**GATOS 9.7s（超 5s 预算，禁用）**。
  即：单页拿 doxapy 抄底不会拖垮批量，但没必要——我们的实现已经够快且更好。

**可执行结论**：`common.binarize()` 保持不动。若要加"霉斑页"的稳健性，在 `binarize()` 后面加一层
`doxapy SU → 低误判纸面掩膜 → 把远离任何 SU 墨团的 Sauvola 散点清零`（预计能砍掉霉斑 tile 里一半碎片），
但这属于 segment/qc 侧的清洗，不建议塞进共享 core。

### 深度学习二值化（全部不可行）
* **Doc-UFCN**（PyPI `doc-ufcn` 0.1.9，Teklia）：py 3.10 可以，但硬依赖 `torch==2.1.0` + `huggingface-hub`（权重需联网拉取）。→ 不用（纪律禁 torch，且 HF 直连不通）。
* **DeblaNet / U-Net 类 DIBCO 方案**：权重是 DIBCO/H-DIBCO 训练集训练的，对 70MP 手抄本必须重训 + 需 GPU。→ 不用。
* **onnx 预训练**：hg 镜像搜索 `binarization` 只有 `SBB/sbb_binarization`、`SBB/eynollah-binarization`（tf-keras，apache-2.0）、
  `DiTo97/binarization-segformer-b3`（transformers+torch，openrail）——**没有任何官方 onnx 权重**，都要自己转。
  → onnxruntime 在本项目目前只能当摆设，不要为它设计架构。

## 2. 竖排文本行/列切分与字切分

**没有现成可用的"彝文手写竖排单字切分"实现。** kraken / OCR-D / dhSegment / docExtractor 都是拉丁横排+深度学习路线，竖排不适用。
可复用的只有通用算子，全部 OpenCV/numpy 一个函数搞定，已被我们实测覆盖：

* **列切分（投影法）实测**：1/4 页 + flatten + Sauvola(win25,k.30) + 列投影平滑 9 + 12% 峰值阈值 → **一次调参就是 14 列**，
  列距 ≈450px（SPEC 的 400px 同级）；但列带宽只有 172–220px（比 300px 字宽更瘦），说明**全局阈值会把细笔画削掉**，需按列墨量自适应。
  产物 `scratch/prior/probe_stats.json:page`。
* **断笔（关键负面结论）**：300×350 字格内的连通域数 **mean 5.6 / p50 5 / p90 11.5**（证实 SPEC 的"一字 2–6 域"，还更碎）。
  形态学闭运算能合并的比例：k=3 → 9.6%、5 → 17.1%、7 → 21.2%、9 → 23.3%、11 → 24.7%、13 → **28.1%**，
  而 k≤13 全程没有出现 >430 宽 / >480 高的融合块（不误连邻字）。
  → **闭运算修不好断笔**（>70% 碎片仍分离），且再开大就开始吃邻字。**必须靠 DP + 字高先验 + 连通域不切断约束**（Lead 的方向是对的），
  形态学的正确用法只是 3×3–5×5 去噪/补孔，不是拼字。图：`scratch/prior/morph_compare.png`。
* **RLSA（run-length smearing）**：竖排就是"按列做竖直 smear"，现成代码 `Vasistareddy/pythonRLSA`、`DenzilFrancisCrasta/python-rlsa`（纯 numpy/cv2）。
  思路可借（smear 后做连通域当"列条带"），但同样不解决字内断笔，优先级低于 DP。
* **watershed 分割粘连字**：`cv2.watershed` + 距离变换可实现（无需 skimage），只在"相邻字真的粘连"时作为 DP 的补充手段。

## 3. 彝文识别 / 数据集（只取方法要点）

* 检索到 Nature 系（npj Heritage Science `s40494-*` 系列）与中文期刊的彝文古籍/字符识别工作，共识方法论：**先版面（列/行）→ 再单字 → 再 CRNN/CNN 分类**，
  数据集多为人工标注的几千～几万单字，**没有可直接下载的切分模型或权重**。
* 对我们的意义：确认"先切字、后识别"的两段式路线正确；切字阶段没有外部捷径，别指望找现成模型。

## 如果只抄一样东西

**抄 Doxa 里 SU 的局部自适应阈值公式（CC0 公共域，可用 numpy 30 行重写进 `tools/vendor/bin_algos.py`），
但只把它当"霉斑 / 背面透字 veto 掩膜"生成器，不当主二值化。**
理由：它是实测里纸面误判最低的（0.4%–3.7% vs 我们 2.5%–6.3%），正好补我们的短板（霉斑/透字误判为墨）；
而笔画完整度在淡字区反而更差（475 碎片 vs 我们 68），所以只能"删假墨"不能"定真墨"。
**其余一切（主二值化、列切分、字切分）继续自写——实测没有任何开源件在这份素材上打得过现有 pipeline。**
