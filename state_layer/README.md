# state_layer —— 状态层

把市场观测变成**状态标签**：现在像震荡、上行、下行，还是极端行情。

**它是因果层的上游**：状态层说"现在像什么"，因果层说"为什么"。

- 设计细节（含"数据从哪来""什么不能动""已退役清单"）：[DESIGN.md](DESIGN.md)
- 代理工作约束：[../AGENTS.md](../AGENTS.md)

---

## 快速开始

```bash
# 拟合状态模型（改特征/改 K 之后跑；会换 label_signature）
python -m state_layer.cli fit --asset ashare

# 临时试其他状态数 / 种子（不写回 config，只影响这一次拟合）
python -m state_layer.cli fit --asset ashare --k 2
python -m state_layer.cli fit --asset ashare --k auto     # 让模型按 BIC 自己选

# 之后必须重估边快照，否则卡片会被拒
python -m causual.causual_core.cli edges --asset ashare
```

网页：`/state`。

---

## 数据从哪来

**面板只在装配点取**（`cli.py` / `web/routes.py` / service）：

```python
from shared.adapter import get_data
from state_layer import state_assets
panel = get_data(state_assets.panel_of(asset))     # DataFrame，列是小写语义名
```

`port` / `features` / `analysis` **只收 DataFrame**——不调 `get_data`、不接受 `{表名: Series}`
（AGENTS §4'③）。`config.REQUIRED_TABLES` 只是 `port.required_tables()` 的装配清单，
和 `tables_of(Panel)` 有等式测试钉住。

---

## 结构

```
state_layer/
├── state_assets/<asset>/   每个资产：config(K/特征/标签/要哪些表) + features
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
├── cli.py                  命令行（装配点）
└── web/                    /state 页面（装配点）
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
| `state_view(asset, panel)` | **给消费者的一次性视图**（标签+名称+spec+一行提示） |
| `profile(asset, panel)` / `timeline(asset, panel)` / `availability(panel)` | 页面用的画像与观测 |
| `required_tables(asset)` | 该资产要哪些表（装配清单；= `config.REQUIRED_TABLES` ∪ 观测用表） |

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

- **市场宽度不在状态层**：宽度改由数据层 `derive` 现算，`/state` 时间轴不画
- **欠账：黄金 `vol_z` 缺 `.replace(0, np.nan)`** → 横盘段可能 `±inf`；改它会动黄金特征、
  要重拟合 + 重估 edges，攒到和 `etf_group_shares.fillna(0)` 同一批
- **K=3 有退化状态**（占比 2.7%、`stay_prob≈0`），详见 `experiment/k_study.py`
- **退市股缺失**，历史回测有幸存者偏差
- `chain.pkl` 已进生产（2026-11-15 重拟合）：离线后验与外推只认链里的 `last_window`，
  没有就明确告警、不外推

已退役的东西（旧面板模块、旧签名、旧产物文件等）以及"为什么退、别再引入"的理由，
见 [DESIGN.md](DESIGN.md) §10。

## 分组（2026-11-22）

A 股出 **3 组**状态：短期价格（3 态，primary）/ 短期波动（3 态）/ 中长期价格（2 态）。
九宫格由 `analysis/combine.py::grid()` 合成（唯一实现）。黄金仍只有 1 组。

- 训练/落盘：`python -m state_layer.cli fit --asset ashare`（或 `from state_layer.fit import fit_asset; fit_asset("ashare")`）
- 下游（causual）只读 **primary**（`short_price`）；加组**不会**改变 `label_signature`
- 页面上四张图的区间不完全相同（各组预热不同，九宫格取交集）
