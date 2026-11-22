# state_layer 设计文档

> **更新日期：2026-11-16**　｜　使用说明见 [`README.md`](README.md)
> 给模块代理看的规格。改动前先读这一页，改完回来补表格。

---

## 1. 一句话定位

**从市场观测识别"现在是什么状态"，并把状态标签交给下游。**

它是因果层（`causual`）的**上游**：状态层回答"现在像什么"，因果层回答"为什么"。
因果层把状态当数据用，**不 import 状态层内部**。

四条硬约束：

1. **状态层不依赖 causual**：面板从耦合层拿，特征/标签词表都是自己的
2. **下游只用 `label`**，不用 `state_id`——HMM 状态编号重训一次可能整体换位
3. **模型不自己评价自己**：好坏由 `metrics.py` 算
4. **只收 DataFrame，不自己取数**：数据从装配点进来（见 §3）

---

## 2. 分层

```
装配点：cli.py / web/routes.py / service
      │  panel = get_data(Panel.STATE_<asset>)      ← 全模块只有这里取数
      ▼
state_assets/<asset>/     声明：要哪些表（config）+ 怎么算特征（features）
      │  features.build(panel) → 特征矩阵
      ▼
model/                    模型链：按 MODEL_CHAIN 依次认领日期
      │  fit / infer
      ▼
fit.py                    编排：跑链 → 算指标 → 组 spec
      │
      ▼
saved_models/<asset>/     current.json / labels.csv / chain.pkl
      │
      ├──► analysis/      状态画像、观测矩阵（只读，给页面用；只收 DataFrame）
      └──► port.py        对外：标签、单点状态、外推、黏性、状态视图
```

---

## 3. 文件职责与数据来源

| 文件/目录 | 职责 | 谁调用 |
|---|---|---|
| `state_assets/<asset>/config.py` | K / 特征清单 / 极端阈值 / 标签词表 / `MODEL_CHAIN` / `METRICS` / **`REQUIRED_TABLES`** | fit / port |
| `state_assets/<asset>/features.py` | 面板 → 特征矩阵（纯函数）；入口收口列名大小写；派生列调 `data_processing` 原语 | fit / port |
| `model/base.py` | `Model` 协议：fit / infer / state_params / to_dict | 所有模型 |
| `model/hmm/` | GaussianHMM + 拟合 + 选 K + 序列化 | 链 |
| `model/extreme/` | 极端行情规则模型（认领极端日） | 链 |
| `model/__init__.py` | `MODELS` 注册表 + `run_infer`（链式推断） | fit / port |
| `metrics.py` | 状态分离度等评价指标（**模型不评价自己**） | fit |
| `align.py` | 状态命名（按均值排序映射成 trend_up/range/…） | 模型 |
| `fit.py` | `fit_pipeline`：按链跑模型 + 组 spec | cli |
| `store.py` | 产物读写（spec / labels / chain） | fit / port |
| `analysis/observations.py` | 观测矩阵（收益/波动/成交/换手/大小盘）——**收面板**，缺列给 NaN | web |
| `analysis/stats.py` | 状态画像（各状态的收益、波动、持续） | web |
| `port.py` | 对外能力 | 其他模块 |
| `cli.py` | `python -m state_layer.cli regime`（**装配点**） | 人 |
| `web/` | `/state` 页面（**装配点**） | service |

**新文件必须先补这张表。** 表里没有的文件 = 漂了。

### 数据从哪来（改这一块之前先读）

```python
# ✅ 只在装配点
from shared.adapter import get_data
from state_layer import state_assets
panel = get_data(state_assets.panel_of(asset))     # DataFrame，index=交易日

# ❌ 不要在 port / features / analysis 里做这件事
```

- 面板列是**小写语义名**（`close` / `csi300` / `amount` / `etf_dispersion`…，见 `COLUMNS[Panel]`），
  由 adapter 保证。
- `port` / `features` / `analysis` **只收 DataFrame**：不调 `get_data`，也不接受 `{表名: Series}`。
  这是 AGENTS §4'③ 的硬约束——传 dict 等于下游再拼一次，装配做两遍。
- `config.REQUIRED_TABLES` **不参与取数**（`get_data` 按 adapter 自己的声明取），
  它是 `port.required_tables()` 的装配清单（service / causual cli 拿它做并集）。
  `tests/state_layer/test_state_features_fresh.py::test_required_tables_match_adapter` 把它与
  `tables_of(Panel)` **钉成相等**——加表/删表只改一处，另一处会红。

---

## 4. 特征口径（别顺手"统一"）

`state_assets/<asset>/features.py::build(panel)`：

1. **入口收口列名大小写**：`_upper()` 把面板列名统一成大写，公式只认大写。
   现在生产来源（adapter）本来就是小写，`_upper()` 只为"因果层面板仍是大写"而留；
   等 causual 也切到 adapter 面板后可以删（届时先跑 `test_state_features_match_prepare`）。
2. **`_derive()` 三条派生列调 `shared.utils.data_processing` 原语**（不再手写）：

   | 列 | 原语 | 说明 |
   |---|---|---|
   | `SHIBOR_CHG_BP` | `diff_bp(SHIBOR3M)` | 单位 bp |
   | `MARGIN_CHG` | `pct_change_pct(MARGIN_BALANCE)` | 单位百分数 |
   | `VOLUME_Z` | `rolling_z(VOLUME)` | 60 窗口 / 20 最小期；标准差 0 → NaN |

   这三条在 causual 的 `prepare()` 里也有**同一份公式**，现在两边调同一个函数，
   "一致"是结构性的，不再靠测试守。
3. **`etf_dispersion` 读面板列**：datahub 的 `derive` 已算好并作为面板列给出，
   这里只搬运（缺列时 stderr 告警 + 整列 NaN，由新鲜度测试兜住）。
4. **`margin_chg_z` / `shibor_chg_z` 保留内联 `rolling(60).apply(...)`，不要换 `rolling_z`**：

   实测（同一份输入）换掉后 NaN 位置一致，但数值差末位——`MARGIN_CHG` 逐位相同仅
   99/3506 格、最大差 4.9e-13；`SHIBOR_CHG_BP` 54/3506、最大差 2.7e-11。
   根因：`apply` 里的 `x.std()`（nanops 两遍法）与 `rolling.std()`（Cython 在线算法）
   不是同一套浮点运算。
   而这两列**只有状态层有**（因果层没有对应副本），换它**没有去重收益**，却要动 HMM 输入
   → 会改标签、要重估边。**风险不为零、收益为零的改动，不做。**
   真要换，必须和一次重拟合 + 重估 edges 绑在一起，并把验收标准从"差 0 格"改成明确容差。

---

## 5. 模型协议（最重要的一节）

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
4. **补测试**：`tests/state_layer/test_model_contract.py` 的 roundtrip + 链过滤

候选：Student-t HMM（把协方差换成 t 分布，极端日更稳健）、HSMM（显式建模持续期）。

---

## 6. 输出契约

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
- **`model.version`** = 链 + K + 拟合指纹（`_fit_id`）；`_fit_id` 的公式**不要动**，
  它进了 spec 与 version，改了就没有可比性
- `state_probabilities` 为空时（极端日），`confidence` 也是 `None`

### 状态的视图（给消费者）

`port.state_view(asset, panel)` —— **`series` / `meta` 已删，只有两个参数**：

```python
{"labels": Series, "label_name": dict, "labels_order": tuple,
 "version": str, "spec": dict, "line": str}
```

`panel` 由装配点传入（见 §3）；状态层不自己取数、也不接受 `{表名: Series}`。
消费者只消费这个 dict，**不 import 状态层**。

---

## 7. 产物

```
saved_models/<asset>/
├── current.json   spec（model / data / metrics 三段）
├── labels.csv     逐日标签
├── chain.pkl      整条模型链（**含每个模型的 last_window**，离线后验的唯一真相）
└── hmm.pkl        旧格式单模型参数（遗留；没有 chain.pkl 时 load_chain 才回退用）
```

**最后窗口以 `chain.pkl` 为唯一真相**：`fit.save` 把 spec 与链一次落盘，链里每个统计模型
自带 `last_window`（含 `state_labels` / `transmat` / 训练时的 `features` 列名）。
链里没有窗口（旧格式链 / 没有 chain.pkl）**就直接不可用**：`port._models()` 打印明确告警，
`extend` 原样返回、`transition` 返回 `None`——不会拿一份过期窗口凑。

**不进 git**（`.gitignore` 有 `saved_models/`）。部署要单独搬，删目录时注意别连带删掉。

---

## 8. 低频/重活的分工

| 动作 | 频率 | 命令 |
|---|---|---|
| 加载标签、出卡片 | 每天 | 状态机（不重拟合） |
| 重拟合模型 | 手动 | `python -m state_layer.cli regime --asset X` |
| 重估边模型 | 重拟合后**必须** | `python -m causual.causual_core.cli edges --asset X` |

⚠️ **重拟合会改 `label_signature`**，边快照随之失效，卡片会被拒绝。
这两步必须绑在一起做。`--k` / `--seed` 只覆盖本次拟合、不写回 config；
`--k auto` = 让模型按 BIC 自己选（走 `k=None` 分支）。

---

## 9. 已知限制与欠账

- **市场宽度不在状态层**：宽度改由数据层的 `derive` 现算，`/state` 时间轴不画。
  `config.FEATURES` 里从来没有宽度类特征，所以摘除**不影响模型、不需要重拟合**
- **特征整列 NaN 是静默的**：HMM 的 `valid_index` 要求 `FEATURES` 全非空，一列整体失效就会
  一行都不认领——标签停更、卡片因「没有状态标签」被拒、页面 `transition` 缺 `stay_prob`，
  全程不抛异常。`tests/state_layer/test_state_features_fresh.py` 守着「每列最后有效日期不早于
  `csi_all` 最近交易日 + 5 个交易日」
- **欠账：黄金 `vol_z` 缺 `.replace(0, np.nan)`**（现在是 `/(rolling.std())`）→ 整段横盘
  会给 `±inf`，而 A 股的 `volume_z` 走 `rolling_z` 有这层保护。改它会动黄金特征
  → 要重拟合 + 重估 edges，所以和 datahub 的 `etf_group_shares.fillna(0)` **同一批**做
- **K 不是瓶颈，特征才是**：`experiment/k_study.py` 实测 K=3 会切出一个 2.7%、
  `stay_prob≈0` 的退化状态；K=2 更干净但切的是波动率不是趋势
- **退市股缺失**：`stock_daily` 只有当前在市股票，历史回测有幸存者偏差

---

## 10. 已退役（别再引入）

| 东西 | 退役时间 | 为什么 |
|---|---|---|
| `state_assets/<asset>/panel.py` | 2026-11-15 | 每个模块各拼一份面板 = 各自维护表名/对齐口径；改由 `shared.adapter.get_data` 统一提供 |
| `panel(series, meta)` 签名 | 2026-11-15 | `{表名: Series}` 是 `series_map` 时代的化石，下游还得再拼一次 |
| `series_map` / `_series_for()` | 2026-11-16 | 同上；页面改为"装配点取一次 DataFrame 往下传" |
| `PANEL_PROVIDER`（config 里的点号字符串） | 2026-11-15 | 死代码，且是一条指向 `causual...pipeline` 的跨模块隐性耦合 |
| `hmm_last.pkl`（文件 + 回退分支） | 2026-11-16 | 从来没有人写它（全库只有读），是上一代拟合的遗留物：无条件拿它覆盖链内窗口 → 链是新的、窗口是旧的 → `extend` 的 fit_id 校验永远失败 → 标签停止外推、卡片被拒。现在只认 `chain.pkl` 的 `last_window` |
| `port.causual_view` 这个名字 | 2026-11-16 | 端口方法名里带下游模块名 = 替别人想问题；改成 `state_view`，只描述自己 |

如果要在 `port` / `features` / `analysis` 里再 import `get_data`，先回读 §3：
**那样做会把耦合从 2 个装配点摊回全模块**，也违反 AGENTS §4'③。

---

## 11. 接口变更记录：第二参数从 features 矩阵 → panel（2026-11-22）

**改了什么**：`port.state_view` / `current_state` / `extend` / `transition` 的第二个参数
从 **features 矩阵**（调用方先 `features.build(panel)` 再传进来）改成 **panel**
（`get_data(...)` 的原样产物）。

**为什么**：多分组之后，特征属于**分组**（短价格 4 列 / 短波动 4 列 / 长价格 5 列），
不再是一个扁平矩阵。让调用方先算特征，等于把"特征怎么算"泄漏到每个调用点——
A 股一加组，`routes.py`、causual、测试都要各算一遍，特征定义就有 N 份。
现在由 estimator 内部从 panel 算自己那几组，**特征定义只有一份**（`state_assets/<asset>/features.py`）。

**影响面（必须一起改）**：
- ✅ 已改：`web/routes.py`（传 panel）、`cli.py`、`port.py`
- ⚠️ 待改（不归本模块）：`tests/service/test_e2e.py`、`tests/causual/*`
- ⚠️ 语义提醒：`extend` 缺 v2 产物时**抛 `FileNotFoundError`**，不再返回带空标签的 Series
  （带空标签最坏：下游以为有标签）。`state_view` 捕获后退化成"只给历史标签、不外推"。

**为什么不做兼容层**：按列名嗅探"这是 panel 还是 features"在同名不同义时会静默走错分支
（特征里一旦出现叫 `close` 的派生量），这种错不报错。宁可让 call site 显式改。

## 12. v2 spec 的字段布局（causual 侧要按这个读）

```jsonc
{
  "schema": "estimator-v2",
  "asset": "ashare",
  "primary": "short_price",              // 哪个组给下游用（config.PRIMARY_GROUP）
  "groups": {
    "short_price": {
      "title": "短价格",
      "k": 3,
      "features": ["ret_5", "ret_10", "er_10", "snr_10"],
      "labels": {"0": "trend_down", "1": "range", "2": "trend_up"},
      "usable": true,
      "fit_id": "…",
      "label_signature": "…"              // 每组的签名（**不在顶层**）
    }
  }
}
```

读法的三个答案：

1. **`label_signature` 的唯一来源**：`spec["groups"][spec["primary"]]["label_signature"]`。
   **不是**顶层字段——签名现在属于**某个分组**（A 股有三个），顶层放一个会让人以为资产只有一个签名。
2. **`profile` 不在 spec 里**：v2 只落标签（`groups.csv`）。画像要用
   `get_port().profile(asset, panel)`（内部 `analysis/stats.profile`，按 label 索引）。
   如果将来要"不传 panel 也能拿画像"，就把快照写进 `groups[<组>]["profile"]`——
   那是**新增一步**，现在没有，别假装有。
3. **不加顶层兼容字段**：`label_signature` / `profile` 都不上浮。理由同 1：
   分组之后"资产只有一个签名/一份画像"这句话本身是错的。


---

## 13. `label_signature` 的契约（硬约束，2026-11-22 用户拍板）

> **签名 = primary 组标签的指纹。**

- 算法：`state_assets.base.label_fingerprint(primary 组的逐日标签)` = sha1(「日期:标签」逐行) 前 12 位
- **不含**：组名列表、组数、其他组的标签、`primary` 字段本身、特征清单
- 落盘位置：`spec.groups[<组名>].label_signature`（每组各有一份；causual 只读 primary 那份）
- 唯一允许改签名的原因：**primary 组的标签体系本身变了**（重拟合换词表/换状态数）

### 将来加组时别动这些

- ❌ 不改协议（`dict[str, GroupResult]` 不变）
- ❌ 不改 primary 的定义（A 股永远是 `short_price`）
- ❌ 不改签名算法（只哈希 primary 的标签）
- ✅ 只加 key（`config.GROUPS_ENABLED` 加名字 + estimator 里 `groups[name]`）
- ✅ causual / service 的装配点不用改（它们只取 primary）

**为什么是硬约束**：用户明确"A 股加短波动/长价格只是给 state_layer 自己用，causual 不变"。
签名一变，causual 的边快照全部失效、卡片被守卫拒绝——加组就会误伤下游。
**多分组是已具备、按需启用的能力**；加组不该被下游感知。
