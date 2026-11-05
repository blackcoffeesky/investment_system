# regime_engine

通用隐状态引擎：把任意多列特征序列切成 HMM 隐状态，并逐日因果地给出状态。

引擎**不含任何资产语义**——状态的"含义"由调用方的 labeler 决定。同一套引擎服务 A 股、黄金、加密等资产，语义通过 `labeler` 注入。

---

## 快速开始

```python
from regime_engine import fit_regime, select_k,classify
# 0) 找到极端状态单独判断
ext = X.classify(feat, extreme_params)
regular_mask = ext == ""
reg_feat = feat.loc[regular_mask]

# 1) 选状态数（可选，若已知 K 可跳过）
k, table = select_k(reg_feat, candidates=(2, 3, 4), seed=42)

# 2) 全样本拟合
fit = fit_regime(reg_feat, k=k, seed=42)

fit.states            # 全样本 Viterbi 状态序列 (pd.Series)
fit.proba             # 后验概率 (pd.DataFrame, 列 p0..pk-1)
fit.separation        # Bhattacharyya 分离度 dict
fit.raw_means()       # 原始量纲的状态均值（给 labeler 用）
fit.save("gold")      # 存到 saved_models/gold/hmm.pkl

# 3) 逐日因果解码
labels = fit.walk_forward(reg_feat, labeler=my_labeler, min_train=500, refit_every=60)

## 核心概念

### 1. 引擎只做两件事

- **切状态**：在整段特征上拟合 `GaussianHMM`，给出全样本状态序列。
- **逐日因果解码**：`walk_forward` 每天只用当日及之前的数据重训 + 解码，防止未来信息泄漏。

**不做的事**：不给状态起名、不判断"牛市/熊市"、不做收益预测。这些由 `labeler` 完成。

### 2. `labeler`：语义注入点
由于每次重拟合标签和状态都会发生变化，因此用动态映射代替固定关系，保持regime包的稳定。
`labeler` 是一个回调，签名：

```python
Labeler = Callable[[pd.DataFrame], dict[int, object]]
# 输入：该窗口模型的原始量纲状态均值（行=状态，列=特征名）
# 输出：{状态号: 标签}
```

在 `walk_forward` 中，每次重训后调用一次 `labeler`，产出 `{新状态号: 标签}` 的映射。

### 3. 状态号排列不确定性 & 对齐

HMM 每次重训后，状态编号是**随机排列的**（EM 初始化不同）。同一个市场状态，第一次拟合叫"状态 0"，下次可能叫"状态 3"。

**对齐器**（`make_labeler`）解决这个问题：

```python
from regime_engine.align import make_labeler

ref_means = fit.raw_means()                 # 全样本参考系
ref_labels = L.name_states(ref_means)       # 参考命名
labeler = make_labeler(ref_means, ref_labels)

labels = fit.walk_forward(feat, labeler=labeler)
```

`make_labeler` 对每个**新模型状态**，在 `ref_means` 里找**均值最近邻**的参考状态，继承其标签。这样"状态号变、名字不变"。

> ⚠️ **参考系来自全样本**——`walk_forward` 内部因果，但参考系非因果。详见"已知局限 P34"。

### 4. 极端状态单独处理
由于金融市场肥尾极值现象更频繁，但是极端值样本仍然较少，所以要单独筛选，单独处理和判断。