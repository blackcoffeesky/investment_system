# state_layer —— 状态层

把市场观测变成**状态标签**：现在像震荡、上行、下行，还是极端行情。

**它是因果层的上游**：状态层说"现在像什么"，因果层说"为什么"。

- 设计细节：[DESIGN.md](DESIGN.md)
- 代理工作约束：[../AGENTS.md](../AGENTS.md)

---

## 快速开始

```bash
# 拟合状态模型（改特征/改 K 之后跑；会换 label_signature）
python -m state_layer.cli regime --asset ashare

# 之后必须重估边快照，否则卡片会被拒
python -m causual.causual_core.cli edges --asset ashare
```

网页：`/state`。

---

## 结构

```
state_layer/
├── state_assets/<asset>/   每个资产：config(K/特征/标签) + panel(要哪些表) + features
├── model/                  模型链
│   ├── base.py             Model 协议（fit 返回未认领日期）
│   ├── hmm/                GaussianHMM（内部选 K）
│   └── extreme/            极端行情规则模型
├── metrics.py              状态分离度（模型不评价自己）
├── align.py                状态命名
├── fit.py                  fit_pipeline：按链跑模型
├── store.py                产物读写
├── analysis/               观测矩阵 + 状态画像（给页面用）
├── port.py                 对外能力
├── cli.py                  命令行
└── web/                    /state 页面
```

---

## 对外能力（`port.py`）

| 方法 | 说明 |
|---|---|
| `labels(asset)` | 逐日标签 |
| `states(asset, start, end)` | 批量标签表 |
| `current_state(asset, features=None)` | 单点状态 + 后验 |
| `extend(asset, features, labels)` | 把标签外推到最新一天 |
| `transition(asset, features)` | 状态黏性（留在原状态的概率、平均停留） |
| `causual_view(asset, series, meta)` | **给因果层的一次性视图**（标签+名称+spec+一行提示） |
| `profile` / `timeline` / `availability` | 页面用的画像与观测 |

其他模块**只能用这些**，不许 import `model/` 或 `state_assets/`。

---

## 模型链

```python
# state_assets/ashare/config.py
MODEL_CHAIN = ("extreme", "hmm")     # 顺序即处理次序
MODEL_PARAMS = {
    "extreme": dict(sigma=3.0, pct=0.99, abs_cap=0.05),
    "hmm": {"k": 3},                 # k=None 才让模型自己选
}
```

`extreme` 先认领极端日，剩下的给 `hmm`。两个阶段用**同一条链**，
所以在线后验和历史标签口径一致。

K 写死而不是自动选：**拟合要可复现**，让数据自己选 K 只在做实验时手动传。

---

## 已知问题

- **K=3 有退化状态**（占比 2.7%、`stay_prob≈0`），详见 `experiment/k_study.py`
- **退市股缺失**，历史回测有幸存者偏差
- `chain.pkl` 还没在生产生成过，目前跑的是"旧模型 + 新链结构"
