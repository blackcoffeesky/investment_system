# causual 设计文档

> **更新日期：2026-11-14**　｜　使用说明见 [`readme.md`](readme.md)　｜　变更台账见 [`CHANGES.md`](CHANGES.md)
> 项目整体架构（模块地图、依赖方向、数据流全景）见 [`../../README.md`](../../README.md)。

---

## 1. 一句话定位

**把"这些机制现在把标的往哪边推"写成一张人能读、能反驳的卡片。**
不是收益预测模型，是给主观判断用的归因工具。

---

## 2. causual 在整体中的位置

```
datahub/        数据层：SQLite，一表一序列
state_layer/    状态层：市场观测 → 状态标签（**本模块的上游**）
causual/        因果层：机制 → 方向 → 强度 → 卡片        ← 本模块
service/        服务层：统一入口 + 运维 + Web
```

**依赖方向**：causual 只依赖 `datahub` 与 `state_layer` 的 **port**，
而且**只在 CLI 里直连**；service 内一律 `ctx.services[...]`。

因果层**不 import 状态层内部**：状态以参数形式传入

```python
state = {"labels", "label_name", "labels_order", "version", "spec", "line"}
pipeline.run(asset, panel, freshness=rows, state=state)   # rows 由装配点算好
```

`panel`（DataFrame）与 `freshness`（新鲜度行）都由调用方装好——`causual_core` 里既不取数，也不判断哪张表过期。

卡片里的**市场状态只留一行**，画像与黏性在 `/state` 页（归状态层）。

---

## 3. 数据流

```
datahub.sqlite
     │
     ▼
装配点（cli.py / web/routes.py / service）：get_data(Panel.CAUSUAL_{ASHARE,GOLD})
     │  返回语义列名的日频宽表（open / close / vix / cb_holdings …）
     │  **派生列也算好了**（etf_dispersion / size_flow / national_etf /
     │   national_etf_event / dividend_yield / cb_gold_buy / etf_flow）
     ▼
causual_assets/{ashare,gold}/pipeline.py::load_raw(panel)   ← 只装配，不取数
     │  PANEL_COLS 选列 + _derived 把耦合层算好的派生列搬进来
     ▼
causual_assets/{ashare,gold}/pipeline.py
     │  prepare()      → 特征 + 供需层
     │  load_spec()    → 状态模型
     │  load_labels()  → 标签
     ▼
causual_core/
     ├─ regime_infer.py   外推最新标签
     ├─ signal_layer.py         逐边算 z、β、贡献 → α
     ├─ macro_layer.py      供需层：位置 × 边际
     └─ web/render.py       渲染卡片
     ▼
causual/output/{asset}/cards/
     ├── YYYY/MM/card_YYYY-MM-DD.md     日频：状态 + 信号层
     └── YYYY/MM/macro_YYYY-MM.md       月频：结构 + 供需
```

### 3.1 铁律：数据只在装配点进来

| 谁 | 能做什么 | 不能做什么 |
|---|---|---|
| `cli.py` / `web/routes.py` / `service` | `get_data(Panel.CAUSUAL_*)` 取面板 | —— |
| `causual_core/*` / `causual_assets/*` / `port.py` | 只收 `panel: DataFrame` 参数 | **不许** `import datahub`、**不许**调 `get_data`、**不许**要求调用方先拼 `{表名: Series}` |

要哪个面板由资产包声明（`pipeline.PANEL_KIND`），装配点用 `CausalPort.panel_kind(asset)` 查——
所以装配点**不需要知道**"ashare 对应哪个 Panel 枚举"。

**为什么禁止 dict 通道**：传 `{表名: Series}` 意味着下游还要再拼一次（哪张表配哪列、怎么对齐），
等于装配做两遍，而两遍必然漂移——历史上真出过"CLI 有股息率、service 没有"。
`series_map` 那套接口已于 2026-11-16 全量删除，**别把它加回来**。

### 3.2 列名约定（两套，别混）

| 来源 | 命名 | 例子 | 谁产生 |
|---|---|---|---|
| **耦合层**（adapter 落的面板列） | **语义小写** | `close` / `vix` / `etf_dispersion` / `size_flow` / `national_etf` / `national_etf_event` / `dividend_yield` / `cb_gold_buy` / `etf_flow` | datahub 的 `DERIVED` 注册表 + adapter |
| **本模块自产**（`prepare()` 算的） | **大写** | `RET` / `VIX_CHG` / `SUPPLY_Z` / `MARGIN_NET` / `VOLUME_Z` / `GOLD_RET` | `prepare()` |

判断标准一句话：**这列是"别人给的"还是"我算的"**。别人给的一律小写，我算的一律大写。
混了就会出现"两套列名"——v5.46 花了一整轮才并掉，别再退回去。
退役名由 `tests/causual/test_causual_dead_columns.py::LEGACY_COLS` 拦住（只增不改）。

### 3.3 `PANEL_COLS` 是**模块声明**，不是抄来的清单

```python
PANEL_COLS   = (...)   # 我这张面板需要哪些列
DERIVED_COLS = (...)   # 其中"耦合层算好、我只搬进来"的那几列
```

两者都必须是 `COLUMNS[PANEL_KIND]` 的子集，由
`tests/causual/test_causual_panel_derived.py::test_panel_cols_are_declared_by_adapter` 钉住
（同一个形状还有 `REQUIRED_TABLES = tables_of(PANEL_KIND)`：**从耦合层推导，不手抄**）。

- 运行期还有兜底：`panel[list(PANEL_COLS)]` 少列直接 `KeyError`，`_derived` 同样。
- 但**别把运行时当唯一防线**：卡片是定时任务，崩在凌晨没人看得见。
  改了 adapter 的 `COLUMNS` 而没同步这里，测试当场红——这就是那条等式测试的意义。

### 3.4 状态层的 `spec` 有两种结构（运行时两种都可能）

状态层重构成"多分组"后，`state["spec"]` 有两种形状，`store.spec_any` 会回退读旧的
`current.json`，所以 causual **必须两种都能吃**：

| | legacy（`current.json`） | v2（`spec.json`，`schema="estimator-v2"`） |
|---|---|---|
| 签名 | `spec["data"]["label_signature"]` | `spec["groups"][spec["primary"]]["label_signature"]` |
| 分组 | 无 | `groups`：A 股 `short_price`、黄金 `raw`。**每资产每分组各一个签名**——"这个资产只有一个签名"这句话不再成立 |
| 画像 | `spec["data"]["profile"]`（fit 时的快照） | **不在 spec 里**——由 `StatePort.profile(asset, panel)` 现算 |

读法：**按 `schema` 分支**，不靠 `try/except KeyError` 猜（那会把"结构写错"和"结构是旧版"
混成同一件事）。这一层收在 `causual_core/spec.py`：4 处读签名、2 处读画像都调它。

三条必须记住（2026-11-23 起）：

- **v2 没有 `spec["data"]`**。那一层只存在于 legacy；代码里读签名/画像一律走
  `causual_core/spec.py`，别处不许再写 `spec["data"]["…"]`（写了就是 `KeyError`）。
- **`label_signature` 的语义变了**：v2 起 = **primary 组的标签指纹**
  （`label_fingerprint(labels)` = 对每行 `日期:标签` 取 sha1，前 12 位；只哈希 primary 那一条）。
  ⇒ **换特征 / 换滚动窗口 → 标签序列变 → 签名变 → 边快照必须重估**；
  ⇒ **加分组不变**（别的组进不进 `groups` 都不影响 primary 的指纹）。
- 签名和边快照对不上时 `pipeline.run` 直接 `CardRejected`（"标签体系不一致"）——
  这是有意的 fail-closed：宁可不出卡，也不要拿旧 β 配新标签。

**画像怎么进卡片**：`profile` 是现算的，所以由**装配点**放进 `state["profile"]`——
`cli.state_view()` 这个 helper 三个装配点共用（CLI、`web/routes.py`、测试）。
核心代码只读 `state["profile"]`；两样都没有时打一行 stderr 并给空表，
不许静默渲染一张空画像表还装作正常。

### 3.5 ⚠️ 各条边的样本区间**不一致**（已知，暂不统一）

`estimators.fit_all()` 只把**按标签窗口切过**的 `sub` 交给 DML；另外两类估计器拿的是
**整张面板**：

```python
fit_distributed_lag(df,  …)   # 整张面板
fit_event_study(df, …)        # 整张面板
fit_dml_edge(sub, …)          # 标签窗口（有标签的日子）
```

这不是笔误：三类估计器的**样本单位不同**——DML 的样本是"带标签的交易日"，
事件研究的样本是**事件**，分布滞后是**月度观测**。硬切到"有标签的日子"没有口径上的道理。
但后果必须让人知道：**α 是把不同样本区间上估出来的 β 加在一起**。

| 资产 | 边 | 估计器 | 频率 | n_obs | 实际拟合到 |
|---|---|---|---|---|---|
| ashare | REAL_YIELD / USDCNY / USD_INDEX / VIX / MARGIN | causal_forest | D | 2293 | 2026-05-20（起点 = 首个标签日 2016-08-02）|
| ashare | NATIONAL_ETF | event_study | D | 200 | 2026-11-24（事件 2016-03 起）|
| ashare | AMOUNT_SHOCK | event_study | D | 34 ⚠️ | 2026-11-24（事件 2012-12 起）|
| ashare | EQUITY_SUPPLY | distributed_lag | ME | 60 | 2026-11-30（月度 2020-01 起）|
| gold | REAL_YIELD / USD_INDEX / VIX | causal_forest | D | 3786 | 2026-01-15（整段历史）|
| gold | USDCNY / OPEN_INTEREST / COT_NET_LONG | causal_forest | D | 2207 | 2026-01-15 |
| gold | FED_RATE / CPI_SURPRISE / ETF_FLOW | distributed_lag | ME | 175 / 212 / 120 | 2025-06-30 ~ 2026-11-30 |
| gold | GEOPOLITIC_RISK（= `world_tension`）| event_study | D | 26 | 2026-11-23 |
| gold | CB_GOLD_BUY | distributed_lag | ME | **0** | **—（样本不足，未估计）** |

**当前边数（与 `EDGES` 对齐，改边必须同时改这三处）**：

| 资产 | 边数 | 进 α | 不进 α |
|---|---|---|---|
| ashare | **8** | **7**（RY / USDCNY / USD_INDEX / VIX / MARGIN / NATIONAL_ETF / AMOUNT_SHOCK）| `EQUITY_SUPPLY`（supply 层）|
| gold | **11** | **9** | `ETF_FLOW`、`CB_GOLD_BUY`（样本不足/方向不可靠）|

已删的两条 A 股边（别复活）：

- `INSIDER_NET`（2026-11-23，用户：内部人增持只在宏观层用）——`insider_net` 列仍在
  `PANEL_COLS` 里供 `NET_SUPPLY` 使用，但不再成边；`validate_insider`（四段稳健性）
  一并退役，因为四等分后每段只有 16~18 个月，够不到 `fit_distributed_lag` 的
  `max_lag+16=22` 下限，**结构性失效**。
- `SHIBOR3M`（2026-11-28，用户：银行间流动性不在关注信号集）——数据层同时删了
  `shibor3m` 表；`SHIBOR_CHG_BP` 派生列一起去掉。它没被任何边当 `confounder`，
  所以删它不影响其他边的估计，只影响 α 的加总（它自己那份贡献约 −2bp）。

⚠️ `AMOUNT_SHOCK` 的 `n_obs` 会**随数据修订来回变**（观测到 34 → 22 → 34）：
它按 `VOLUME_Z` 的事件数计，而成交量历史被日更重写过。**读这条边的 n 之前先看数据日期**，
别把它当成标签或边结构变化的信号。

纯度（`purity`）是**逐边**按该边自己的 β 与 CI 算的：删掉别的边、或别的边换估计器，
都不会改变它。跨边比较纯度只在"同一次估计、同一份标签"下有意义的。

⇒ 引用某条边的 β 时，要说清它是**哪个区间、什么样本单位**上估的；跨边相加得到的 α
（以及不同边之间的 β 比较）都只能是"各段各自成立"的粗略合成。

快照里每条边都带 `n_obs` / `fitted_until`（`CB_GOLD_BUY` 未估计时为空），
卡片详情页脚注也标了这件事。**要不要真正统一口径，等用户拍板**——统一前不要"顺手"改。

---

## 4. 文件职责

### 4.1 `causual_core/`（与资产无关）

| 文件 | 职责 |
|---|---|
| `edge.py` | `Edge` 数据类：机制、方向、量级、层（tier）、是否进 α、估计器 |
| `nodes.py` | 节点定义与分类（外生/中间/内生） |
| `estimators.py` | DML / 分布滞后 / 事件研究 / OLS(HAC) 四类估计器 |
| `signal_layer.py` | 读边快照 → 算当期 z → 剂量×敏感性 → 汇总 α |
| `macro_layer.py` | 供需层：`pair_view`（位置 × 边际）、结构层读取 |
| ~~`freshness.py`~~ | **已删（2026-11-18）**：判定口径搬回数据层（见 §8.1）；本模块只剩 `pipeline._notes`（拼句子）与 `_month_of`（归档月） |
| `web/render.py` | 渲染总览卡与详情卡（Markdown）——`causual_core/narrative.py` 与 `build_console()` 已删（2026-11-23） |
| `snapshot.py` | 边快照的读写（`causual/data/snapshots/`） |
| `spec.py` | **吃两种状态层 spec 结构**：`label_signature_of(spec)` / `primary_group(spec)` / `profile_of(spec)`（见 §3.4） |
| `regime_fit.py` | 拟合 HMM 状态模型并落盘 |
| `regime_infer.py` | 用已拟合模型外推最新标签 |
| `pipeline.py` | **出卡编排**：面板 → 状态 → 信号 → 卡片（数据由调用方传入） |
| `cli.py` | 子命令：`regime` / `edges` / `audit` |
| `evaluate.py` | 评估口径：Rank IC、分层收益、参数回收 |
| （原 `utils/` 已迁走） | 日期对齐在 `shared/utils/time_align.py`（`to_daily` / `build` / `daily_index`）；滚动 z 在 `shared/utils/data_processing.py`（`periodic_z` / `edge_z`）。**不要引 `shared.utils.calendar`**——2026-11-22 已删除，三个函数在 production 里没有使用者 |

### 4.2 `causual_assets/`（资产插件）

| 文件 | 职责 |
|---|---|
| `ashare/pipeline.py` | `PANEL_KIND`（要哪个耦合层面板）+ `PANEL_COLS`（选哪些基础列）+ `DERIVED_COLS`（耦合层算好的派生列）；`load_raw(panel)` **只装配不取数**；`_derived(df, base)` 是派生列的唯一接缝（测试打桩它）；`prepare()` 算特征、供需层，单位换算调 `shared.utils.data_processing` 的 `diff_bp` / `pct_change_pct` / `rolling_z` |
| `ashare/config.py` | 边的定义（机制、方向、估计器、是否进 α）+ `NOT_CONNECTED`（已知没数据的列） |
| `gold/*` | 同结构；`DERIVED_COLS = ("cb_gold_buy", "etf_flow")` |

> `panel.py` 已于 2026-11-15 删除；2026-11-16 起**连派生列也不再由 causual 算**：
> 六列（ETF 四列 + 股息率 + 黄金两条流量）都在 datahub 的 `DERIVED` 注册表里，
> adapter 落成面板列。字段映射在 `pipeline.PANEL_COLS`，代码归一在 `shared.adapter`。

#### `_derived(df, base)` 这个接缝为什么还留着

它现在**不计算任何东西**，只是按 `DERIVED_COLS` 从 `base` 里取列（缺列 → `KeyError`）。
留着是因为三件事：

1. **它是测试的打桩点**：`tests/causual/test_causual_panel_derived.py::test_guard_turns_red_when_derived_vanishes`
   把 `_derived` 打桩成 `lambda df, base: {}`，然后要求"派生列消失"被判定抓住。
   没有这个函数，那几条断言就只能证明"今天数据是好的"，证明不了"判定逻辑本身有效"。
2. **它是列契约的落点**：六列由耦合层给，`PANEL_COLS` 里不重复列，装配关系只写在这一处。
3. **它是将来加 causual 自有派生量的位置**：真要算点什么（不从面板拿的），加在这里，
   不要内联进 `load_raw`。

**别把它删掉**，哪怕它永远只剩一行 `return {...}`。

### 4.3 产物目录

```
causual/data/
├── regime/{asset}/current.json    状态模型参数
├── regime/{asset}/labels.csv      历史标签
└── snapshots/{asset}/edges_*.json 边快照（β、CI、纯度、贡献）
causual/output/{asset}/cards/*.md  卡片
```

**模型产物跟代码走，原始数据不在仓库里。**

---

## 5. 三条设计主线

### 5.1 三层按"因果深度"分，不按频率

| 层 | 内容 | 进 α |
|---|---|---|
| `structure` | 制度、政策态度 | 否（只有定性 + 证伪条件） |
| `supply` | 资金/筹码供需、价值、央行 | 否（位置 × 边际） |
| `signal` | VIX、汇率、两融、国家队、放量 | **只有这层** |

**频率是深度的副产品**，不是分层依据。层次越深，数据自然频率越低。

### 5.2 供需层用「位置 × 边际」，读法按方向分化

- 位置：在自身历史的什么位置（原始值 / 滚动和 的分位）
- 边际：在变好还是变差（同比 / 环比差 / 同比 MA3 一阶差 的分位）

不预设"高就是坏"：同一个"高位+改善"，对正向指标是"强势仍在增强"，
对负向指标是"压力持续加大"。

### 5.3 只给可复现的量

- 不做"传导结构"：高自相关序列在任何滞后阶都会测出相关，那测的是粘性，不是传导
- 不做综合合成（宏观环境不合成指数）
- 没有数据的边标「未估计」，不编造代理

---

## 6. 日常调用顺序

### 6.1 每天出卡

```
python -m causual.causual_core.daily --asset ashare
  │
  ├─ pipeline.load_raw()        → 读面板列（派生列已由耦合层算好）   # 不取数
  ├─ pipeline.prepare()         → 特征 + 供需层
  ├─ pipeline.load_spec()       → 状态模型参数
  ├─ regime_infer.extend()      → 最新标签（含外推）
  ├─ signal.build_bundle()      → 逐边 z / β / 贡献 → α
  ├─ top_layer.pair_view()      → 供需层位置 × 边际
  └─ narrative.build_card()     → card_YYYY-MM-DD.md
```

### 6.2 每 2 个月重估状态

```
python -m causual.causual_core.cli regime --asset ashare
  └─ regime_fit.fit() → 状态模型落盘
```

### 6.3 每季度重估边

```
python -m causual.causual_core.cli edges --asset ashare
  └─ estimators 逐边重估 → 快照 → narrative 下次读新 β
```

### 6.4 数据更新（在 datahub 里）

```
python -m datahub.fin.update
python -m datahub.fin.check
```

---

## 7. 扩展点

| 想加什么 | 改哪里 |
|---|---|
| 新边 | `{asset}/config.py` 的 `EDGES` 加一条 `Edge(...)` |
| 新节点 | `nodes.py` 定义 + `pipeline.BASE_COLS` 映射 + `config.py` 引用 |
| 新资产 | 复制 `causual_assets/<name>/`，实现 `panel / pipeline / config / label` |
| 新卡片小节 | `narrative.build_card` 里加一节；注意层号自动重排 |

---

## 8. 已知技术债

| 项 | 影响 | 建议 |
|---|---|---|
| `web/render.py` 677 行，卡片/详情两个渲染都在里面（`causual_core/narrative.py` 那层早已不存在） | 改一处容易碰坏另一处 | 拆成 `card.py` / `detail.py` |
| `pipeline.prepare()` 既算特征又算供需层 | 职责混在一起 | 拆 `features.py` / `supply.py` |
| 状态外推用"上次模型 + 新数据" | 模型老了会失真，卡片有"龄期"提示但不会自动重训 | 加龄期阈值告警 |

> `EQUITY_SUPPLY` 那条债（"列接了真数据、快照还是 `n=0`"）**已经消掉**：2026-11 的重估之后
> 它 `n=60`、正常参与展示（只是不进 α，因为 tier=supply）。

### 8.1 识别口径的坑（都是静默的）

派生列 / 特征列消失时**不会抛异常**，只会让卡片"少一行"。两条防线：

| 防线 | 位置 |
|---|---|
| **只有一套列名**：面板列 = 耦合层语义名（`close`/`vix`），退役的大写名由 `LEGACY_COLS` 拦住 | `pipeline.PANEL_COLS` + `tests/causual/test_causual_dead_columns.py` |
| 计数盘点：`REQUIRED_TABLES` 有数据 + 派生列新鲜 + 每条边的列都在面板里 + 列数钉死 | `tests/causual/test_causual_panel_derived.py` / `test_causual_dead_columns.py` |

**新鲜度不再由本模块判断**（2026-11-18 起）：判定口径（期望期、`lag_days` 宽限、
`stale` / `overdue`）全在数据层，由装配点 `shared.adapter.freshness(panel)` 算好、
**当参数传给** `pipeline.run(freshness=...)`。本模块只做两件事：

| 事 | 在哪 | 口径 |
|---|---|---|
| 挑行 + 拼句子 | `pipeline._notes(rows, freq=…, limit=…)` | 只报 `stale`；默认 `overdue_only=True`（= 旧行为），`False` 时给每句加"（宽限已过/源尚未发布）" |
| 归档月 | `pipeline._month_of(freshness)` | 月/季频行 `actual` 的最大值所在月 |

**为什么删掉自算**：`freshness.py` 里那份手写清单漂过两次——表名改掉后 8 个名字只命中 2 个，
`continue` 把整张表吞掉，于是"没有过期表"这个结论永远成立；而且它只监控了 11 张日频表，
`geopolitic_risk` / `gold_open_interest` / `xau_usd` 这些**面板真的在用**的列从来没人看。
现在表清单 = `tables_of(panel)`，收窄是结构性的，不可能漂。

守卫：`tests/datahub/test_freshness_port.py`（口径）+ `tests/datahub/test_freshness_watchlist.py`
（表名真实、`freq` 可判、覆盖非空）+ `tests/causual/test_card_smoke.py`（真实面板出卡）。

**空列也必须显式**：边用到的列要么有数据，要么写进 `config.NOT_CONNECTED`
（`{列名: 原因}`）。没声明却整列 NaN → 测试红；声明了却已经有数据 → 也红
（否则那张表会烂成永久豁免）。为什么必须这样：估计器对全 NaN / 零方差的控制变量
是**静默跳过**的，配置上写着"控制了社融"、实际没控，卡片和快照都看不出来。

守卫：`tests/causual/test_causual_dead_columns.py`（有数据 / 声明有效 / 每个资产都得声明）。

守卫：`tests/datahub/test_freshness_watchlist.py`（清单存在性 + freq 可判 + 行为验证 + 告警）。

### 8.2 行为变化记录（数字变动时先查这里）

| 版本 | 变化 | 影响 |
|---|---|---|
| v5.45 | 黄金 `CB_GOLD_BUY` / `ETF_FLOW` 的落位日改成**发布可见日**（央行购金 +45 天、ETF 持仓 +40 天）；旧实现用数据所属日 = **前视** | 季度聚合显著变化：`CB_GOLD_BUY` 32/42 个季度变（例 `2026-03-31` 2.32 → 120.26）、`ETF_FLOW` 76/76 变（例 `2026-09-30` 211.17 → −69.05）。**黄金卡片供需段与季度分位会跟着变，不是回归**。完整对照表见 `CHANGES.md` v5.45 |
| v5.46 | 面板列名改成语义名（`CLOSE`→`close`），三对同值列合并 | 面板列数变少（ashare 38→35/59→56，gold 20→19/29→28），**数值逐位不变**；详情卡「数据新鲜度」行名变 `fed_rate` 这种小写名 |
| v5.48 | 六列派生量沉进耦合层（ETF 四列 + 股息率 + 黄金两条流量），causual **不再计算、只装配**；股息率缺列改 `KeyError`（不再"告警 + 整列 NaN"） | 列名变 `etf_dispersion` / `size_flow` / `national_etf` / `national_etf_event` / `dividend_yield` / `cb_gold_buy` / `etf_flow`；**列数与数值都不变**（逐位比对过）。`series: dict` 通道彻底删除，`load_raw` / `build_panel` / `run` / `port.generate` 全部收 `DataFrame` |
| v5.52 | 因果层的 `GEOPOLITIC_RISK` 节点**换测量**：列从 `geopolitic_risk`(GPRC) 改成 `world_tension`(WTM)。节点名**不变**（改名字会牵动已落盘的边快照索引） | `GEO_CHG` 只剩 2026-03-24 起的 159 个观测（之前全 NaN，**没做缩放、没拿旧序列补空档**）；`GEO→GOLD_CNY` 样本从 ~2400 天掉到 ~160，CI 变宽、z 变动 → 该边实测**未激活**（z=-0.93）。两张卡里 `world_tension` 不再触发 ⚠ 滞后告警（数据到 11-17） |
| v5.51 | `freshness.py` 删除，判定搬回数据层；`run` / `port.*` 收 `freshness` 行，告警判据从"自算的 stale"换成数据层的 `overdue` | **日频卡多一条 ⚠ 行**：ashare 报 `geopolitic_risk`、gold 报 `geopolitic_risk` + `gold_open_interest`（旧手写清单漏了这三张，不是回归）。月频卡与归档月不变（`2026-10`）。文案格式逐字未改 |

---

## 9. 对外接口（给 service 层）

causual 不直接对外提供服务。service 通过三样东西使用它：

| 接口 | 位置 | 契约 | 谁调用 |
|---|---|---|---|
| `PANEL_KIND` / `PANEL_COLS` / `DERIVED_COLS` | `causual_assets/{asset}/pipeline.py` | 模块声明：要哪个面板、要哪些列（⊆ `COLUMNS[PANEL_KIND]`，等式测试钉住） | 装配点 / 测试 |
| `load_raw(panel)` | `causual_assets/{asset}/pipeline.py` | **只装配不取数**：按 `PANEL_COLS` 选列，`_derived` 把耦合层算好的派生列搬进来（缺列 `KeyError`） | `build_panel` / 测试 |
| `build_panel(asof=None, panel=None, state=None)` | `causual_assets/{asset}/pipeline.py` | `prepare(load_raw(panel))` + 贴状态标签 → `(df, spec)`；`panel is None` 直接报错（装配点漏传） | `pipeline.run` |
| `pipeline.run(asset, panel, *, freshness=None, state, asof, quiet)` | `causual_core/pipeline.py` | 出卡；**只吃数据，不取数、不判断过期**。`freshness` 由装配点用 `shared.adapter.freshness(panel)` 算好 | `causual.port.CausalPort` |
| `required_tables(asset)` | `causual/port.py` | 该资产要哪些表（纯声明，= `tables_of(PANEL_KIND)`） | CLI / service 装配点 |
| `panel_kind(asset)` | `causual/port.py` | 该资产要哪个耦合层面板（纯查表，**不取数**）；装配点拿它 `get_data(...)` | CLI / service / web |
| `macro_month(freshness)` | `causual/port.py` | 月频卡该覆盖的月份（月/季频行 `actual` 最大值所在月）。**不再收 `asset`**：freshness 本来就按本资产面板算 | service（`exists` 之前） |
| `generate / generate_macro(asset, panel, *, date, month, state, freshness)` | `causual/port.py` | 出卡；`panel` / `freshness` 都由装配点取好 | service |
| `list_cards / get_card / to_html` | `causual/port.py` | 卡片检索与 Markdown→HTML | `causual/web/routes.py` |
| `register_module()` | `web/routes.py` | 返回 `Module`（router + templates + nav + port） | `service.registry.load()` |

### 9.1 已删除的接口（2026-11-16，**别复活**）

| 名字 | 曾经是什么 | 为什么删 |
|---|---|---|
| `series_map(...)` 传下来的 `{表名: Series}` 通道 | 装配点拼 dict、核心代码自己配对 | 下游要再拼一次 = 装配做两遍 → 两边漂移（出过"CLI 有股息率、service 没有"） |
| `cli._load_series` | 拉 27 张表 + 拼派生 dict → `(series, meta)` | 被 `cli._load_panel`（`get_data(PANEL_KIND)`）取代 |
| `cli.derived_series` / `CausalPort.derived_series` | 把股息率装进 `series` | 股息率已是面板列 `dividend_yield` |
| `pipeline._dividend_yield` / `DIVIDEND_YIELD_KEY` | 把"年度 → 次年 1/1"对齐 + ffill 那一步 | 对齐归**适配层**（adapter 按 `period="Y"` 做）；缺列改 `KeyError`，不再"告警 + 整列 NaN" |
| `DataPort.dividend_yield()` / `DataPort.breadth()` | 数据层的专用方法 | 派生量进 `DERIVED` 注册表后与普通表同构，专用方法等于把"这是算出来的"暴露给外部 |

`CardService.generate()` 幂等：目标日期已有卡片就直接返回路径。

⚠️ `macro_month` 与 `exists` 必须**同一口径**：幂等链是
`macro_month → exists(asset, month) → 生成`。只收窄一边（例如按月收窄但 `exists`
仍用并集月）会让同一张卡被反复重生成，所以 `asset` 只作为关键字参数加。
`web.py` 的路由挂在 service 的 FastAPI 上，模板 `extends "base.html"`。

**反向禁止**：`causual_core` 不 import `service`；卡片生成不读写 `ops_*` 表。


---

## 10. 卡片拆分（日频 / 月频）

`build_card` / `build_detail_card` 返回 `list[Section]`，每节有 `key`：

| key | 内容 | 去日频卡 | 去月频卡 |
|---|---|---|---|
| `state` | 市场状态（HMM 后验、黏性） | ✓ | |
| `structure` | 结构层（制度与政策） | | ✓ |
| `supply` | 供需层（位置 × 边际） | | ✓ |
| `signal` | 净指向分解（α） | ✓ | |

详情卡：`model / edges / betas / lags / contrib / freshness` 归日频，
`supply / supply_glossary` 归月频。

调用方：

```python
secs = pkg.make_card(df, spec, models, when, data_notes=notes)
daily = narrative.render(secs, keep=("state", "signal"))
macro = narrative.render(secs, keep=("structure", "supply"),
                         title="# … 宏观层 · 2026-09")
```

**为什么返回列表而不是拼好的字符串**：两个卡共用一次计算，但各有各的读者；
让调用方选，比在 narrative 里加 `if kind == ...` 干净，也不会出现两套渲染逻辑漂移。
