# state_layer 设计文档

> **更新日期：2026-11-14**　｜　使用说明见 [`README.md`](README.md)
> 给模块代理看的规格。改动前先读这一页，改完回来补表格。

---

## 1. 一句话定位

**从市场观测识别"现在是什么状态"，并把状态标签交给下游。**

它是因果层（`causual`）的**上游**：状态层回答"现在像什么"，因果层回答"为什么"。
因果层把状态当数据用（就像用 `csi_all` 一样），**不 import 状态层内部**。

三条硬约束：

1. **状态层不依赖 causual**（面板、特征、标签词表都是自己的）
2. **下游只用 `label`**，不用 `state_id`——HMM 状态编号重训一次可能整体换位
3. **模型不自己评价自己**：好坏由 `metrics.py` 算

---

## 2. 分层

```
市场数据（datahub）
      │  series_map / read
      ▼
state_assets/<asset>/     每个资产声明要哪些表、怎么拼面板、怎么算特征
      │  panel() → features()
      ▼
model/                    模型链：按 MODEL_CHAIN 依次认领日期
      │  fit / infer
      ▼
fit.py                    编排：跑链 → 算指标 → 组 spec
      │
      ▼
saved_models/<asset>/     current.json / labels.csv / chain.pkl / hmm_last.pkl
      │
      ├──► analysis/      状态画像、观测矩阵（只读，给页面用）
      └──► port.py        对外：标签、单点状态、外推、黏性
```

---

## 3. 文件职责

| 文件/目录 | 职责 | 谁调用 |
|---|---|---|
| `state_assets/<asset>/config.py` | K / 特征清单 / 极端阈值 / 标签词表 / `MODEL_CHAIN` / `METRICS` | fit / port |
| `state_assets/<asset>/panel.py` | 声明 `REQUIRED_TABLES` + 拼日频宽表 | cli / web / fit |
| `state_assets/<asset>/features.py` | 面板 → 特征矩阵（纯函数） | fit |
| `model/base.py` | `Model` 协议：fit / infer / state_params / to_dict | 所有模型 |
| `model/hmm/` | GaussianHMM + 拟合 + 选 K + 序列化 | 链 |
| `model/extreme/` | 极端行情规则模型（认领极端日） | 链 |
| `model/__init__.py` | `MODELS` 注册表 + `run_infer`（链式推断） | fit / port |
| `metrics.py` | 状态分离度等评价指标（**模型不评价自己**） | fit |
| `align.py` | 状态命名（按均值排序映射成 trend_up/range/…） | 模型 |
| `fit.py` | `fit_pipeline`：按链跑模型 + 组 spec | cli |
| `store.py` | 产物读写（spec / labels / chain） | fit / port |
| `analysis/observations.py` | 观测矩阵（收益/波动/成交/换手/宽度/大小盘） | web |
| `analysis/stats.py` | 状态画像（各状态的收益、波动、持续） | web |
| `port.py` | 对外能力 | 其他模块 |
| `cli.py` | `python -m state_layer.cli regime` | 人 |
| `web/` | `/state` 页面 | service |

**新文件必须先补这张表。** 表里没有的文件 = 漂了。

---

## 4. 模型协议（最重要的一节）

```python
class Model(ABC):
    def __init__(self, params: dict | None = None): ...

    def valid_index(self, features) -> pd.Index:
        """这个模型能处理哪些行（默认全列非空；子类按自己用到的列收窄）"""

    def fit(self, features, *, claim=None, **kw) -> pd.Index:
        """训练。返回**没认领**的日期（交给下游）。认领结果放 self.labels_"""

    def infer(self, features, *, claim=None) -> dict:
        """{states: 只含认领日期, remaining_index, proba: DataFrame|None}"""

    def state_params(self) -> dict | None:
        """纯数组（means/covars/transmat）供 metrics 用；规则模型返回 None"""
```

**链的语义**（`MODEL_CHAIN = ("extreme", "hmm")`）：

```
features ──► extreme: 认领极端日，返回剩下的
                     │
                     ▼
              hmm: 认领其余全部，返回空
```

- `claim` 限定"这一环只准在这些日子上下判断"，传全量特征是为了滚动窗口算得对
- **`fit` 和 `infer` 必须走同一条链**——否则在线后验和历史标签不是同一个口径
  （曾经踩过：fit 时 hmm 只见过非极端日，infer 时却把极端日也喂进去）

### 加一个新模型

1. 建 `model/<name>/`，实现 `Model` 协议
2. `model/__init__.py` 的 `MODELS` 登记一行
3. 资产 `config.MODEL_CHAIN` 里加上它
4. **补测试**：`tests/test_model_contract.py` 的 roundtrip + 链过滤

候选：Student-t HMM（把协方差换成 t 分布，极端日更稳健）、HSMM（显式建模持续期）。

---

## 5. 输出契约

```json
{
  "date", "asset", "state_id", "label", "label_name",
  "state_probabilities", "confidence",
  "model": {"kind", "k", "version", "features", "fit_date", "signature"},
  "metrics": {"method", "min", "passed"},
  "as_of_date"
}
```

- **`label`** 是下游唯一该依赖的字段
- **`model.version`** = 类型 + K + 签名 + 拟合日，任何一项变了都能定位
- `state_probabilities` 为空时（极端日），`confidence` 也是 `None`

### 给因果层的视图

`port.causual_view(asset, series, meta)` 返回：

```python
{"labels": Series, "label_name": dict, "labels_order": tuple,
 "version": str, "spec": dict, "line": str}
```

因果层只消费这个 dict，**不 import 状态层**。

---

## 6. 产物

```
saved_models/<asset>/
├── current.json   spec（model / data / metrics 三段）
├── labels.csv     逐日标签
├── chain.pkl      整条模型链
└── hmm_last.pkl   最后窗口（在线算后验用）
```

**不进 git**（`.gitignore` 有 `saved_models/`）。部署要单独搬，删目录时注意别连带删掉。

---

## 7. 低频/重活的分工

| 动作 | 频率 | 命令 |
|---|---|---|
| 加载标签、出卡片 | 每天 | 状态机（不重拟合） |
| 重拟合模型 | 手动 | `python -m state_layer.cli regime --asset X` |
| 重估边模型 | 重拟合后**必须** | `python -m causual.causual_core.cli edges --asset X` |

⚠️ **重拟合会改 `label_signature`**，边快照随之失效，卡片会被拒绝。
这两步必须绑在一起做。

---

## 8. 已知限制

- **K 不是瓶颈，特征才是**：`experiment/k_study.py` 实测 K=3 会切出一个 2.7%、
  `stay_prob≈0` 的退化状态；K=2 更干净但切的是波动率不是趋势
- **退市股缺失**：`stock_daily` 只有当前在市股票，历史回测有幸存者偏差
- **链未在生产验证**：`chain.pkl` 从未生成，目前跑的是"旧模型参数 + 新链结构"
