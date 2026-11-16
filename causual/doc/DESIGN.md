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
pipeline.run(asset, panel, meta=meta, state=state)   # meta 可选，只给新鲜度/归档月用
```

`panel`（DataFrame）与 `meta` 都由调用方装好——`causual_core` 里没有取数代码。

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
```
     ├─ macro_layer.py      供需层：位置 × 边际
     └─ narrative.py      渲染卡片
     ▼
causual/output/{asset}/cards/
     ├── YYYY/MM/card_YYYY-MM-DD.md     日频：状态 + 信号层
     └── YYYY/MM/macro_YYYY-MM.md       月频：结构 + 供需
```

---

## 4. 文件职责

### 4.1 `causual_core/`（与资产无关）

| 文件 | 职责 |
|---|---|
| `edge.py` | `Edge` 数据类：机制、方向、量级、层（tier）、是否进 α、估计器 |
| `nodes.py` | 节点定义与分类（外生/中间/内生） |
| `estimators.py` | DML / 分布滞后 / 事件研究 / OLS(HAC) 四类估计器 |
| `signal_layer.py` | 读边快照 → 算当期 z → 剂量×敏感性 → 汇总 α |
| `macro_layer.py` | 供需层：`pair_view`（位置 × 边际）、`describe_series`、结构层读取 |
| `freshness.py` | 数据新鲜度：`WATCH`（日频）/ `MONTH_TABLES`（月季频）/ `WATCH_MACRO`；`scoped()` 按资产收窄；名字必须与数据层一致 |
| `narrative.py` | 渲染总览卡与详情卡（Markdown） |
| `snapshot.py` | 边快照的读写（`causual/data/snapshots/`） |
| `regime_fit.py` | 拟合 HMM 状态模型并落盘 |
| `regime_infer.py` | 用已拟合模型外推最新标签 |
| `pipeline.py` | **出卡编排**：面板 → 状态 → 信号 → 卡片（数据由调用方传入） |
| `cli.py` | 子命令：`regime` / `edges` / `audit` |
| `evaluate.py` | 评估口径：Rank IC、分层收益、参数回收 |
| `utils/calendar.py` | 交易日历与对齐 |
| `utils/zscore.py` | 滚动 z（按边自身频率） |

### 4.2 `causual_assets/`（资产插件）

| 文件 | 职责 |
|---|---|
| `ashare/pipeline.py` | `PANEL_KIND`（要哪个耦合层面板）+ `PANEL_COLS`（选哪些基础列）+ `DERIVED_COLS`（耦合层算好的派生列）；`load_raw(panel)` **只装配不取数**；`_derived(df, base)` 是派生列的唯一接缝（测试打桩它）；`prepare()` 算特征、供需层 |
| `ashare/config.py` | 边的定义（机制、方向、估计器、是否进 α）+ `NOT_CONNECTED`（已知没数据的列） |
| `gold/*` | 同结构；`DERIVED_COLS = ("cb_gold_buy", "etf_flow")` |

> `panel.py` 已于 2026-11-15 删除；2026-11-16 起**连派生列也不再由 causual 算**：
> 六列（ETF 四列 + 股息率 + 黄金两条流量）都在 datahub 的 `DERIVED` 注册表里，
> adapter 落成面板列。字段映射在 `pipeline.PANEL_COLS`，代码归一在 `shared.adapter`。

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
| `narrative.py` 579 行，卡片/详情/控制台三个渲染都在里面 | 改一处容易碰坏另一处 | 拆成 `card.py` / `detail.py` / `console.py` |
| `pipeline.prepare()` 既算特征又算供需层 | 职责混在一起 | 拆 `features.py` / `supply.py` |
| 状态外推用"上次模型 + 新数据" | 模型老了会失真，卡片有"龄期"提示但不会自动重训 | 加龄期阈值告警 |
| `CAUSUAL_DATA_SOURCE=csv` 已废弃但代码里还有残留判断 | 迷惑 | 清掉 |
| `EQUITY_SUPPLY` 的列 2026-11-15 才接上真数据（`equity_fin_monthly`），但**边快照里还是 `n=0` 的旧模型** | 卡片上这条边一直显示"未估计"，分不清是"没数据"还是"没重估" | adapter 定稿后统一重估 edges（本轮明确不做） |

### 8.1 识别口径的坑（都是静默的）

派生列 / 特征列消失时**不会抛异常**，只会让卡片"少一行"。两条防线：

| 防线 | 位置 |
|---|---|
| **只有一套列名**：面板列 = 耦合层语义名（`close`/`vix`），退役的大写名由 `LEGACY_COLS` 拦住 | `pipeline.PANEL_COLS` + `tests/test_causual_dead_columns.py` |
| 计数盘点：`REQUIRED_TABLES` 有数据 + 派生列新鲜 + 每条边的列都在面板里 + 列数钉死 | `tests/test_causual_panel_derived.py` / `test_causual_dead_columns.py` |

**监控清单也会静默失效**：`freshness.WATCH*` 里的表名一旦与数据层不一致，
`check()` 就 `continue`，于是"没有过期表"这个结论永远成立。所以：

| 约定 | 为什么 |
|---|---|
| 清单里的名字必须真实存在于 `DataPort.tables()` | 名字漂移 = 监控失效 |
| 清单里的表 `freq` 必须是 D/M/Q/Y | 其它频率 `_period_end()` 返回 None，同样静默跳过 |
| 清单**只有一份** `MONTH_TABLES`，`pipeline._month_of()` 也读它 | 原先两处各抄一份，一起烂掉 |
| `check()` 遇到认不出的名字要 **stderr 告警** | 别让"没报错"被当成"没问题" |
| `scoped()` 按 `pkg.REQUIRED_TABLES` 收窄 | `metadata()` 是全局的，A 股卡不该报黄金表 |

**空列也必须显式**：边用到的列要么有数据，要么写进 `config.NOT_CONNECTED`
（`{列名: 原因}`）。没声明却整列 NaN → 测试红；声明了却已经有数据 → 也红
（否则那张表会烂成永久豁免）。为什么必须这样：估计器对全 NaN / 零方差的控制变量
是**静默跳过**的，配置上写着"控制了社融"、实际没控，卡片和快照都看不出来。

守卫：`tests/test_causual_dead_columns.py`（有数据 / 声明有效 / 每个资产都得声明）。

守卫：`tests/test_freshness_watchlist.py`（清单存在性 + freq 可判 + 行为验证 + 告警）。

### 8.2 行为变化记录（数字变动时先查这里）

| 版本 | 变化 | 影响 |
|---|---|---|
| v5.45 | 黄金 `CB_GOLD_BUY` / `ETF_FLOW` 的落位日改成**发布可见日**（央行购金 +45 天、ETF 持仓 +40 天）；旧实现用数据所属日 = **前视** | 季度聚合显著变化：`CB_GOLD_BUY` 32/42 个季度变（例 `2026-03-31` 2.32 → 120.26）、`ETF_FLOW` 76/76 变（例 `2026-09-30` 211.17 → −69.05）。**黄金卡片供需段与季度分位会跟着变，不是回归**。完整对照表见 `CHANGES.md` v5.45 |
| v5.46 | 面板列名改成语义名（`CLOSE`→`close`），三对同值列合并 | 面板列数变少（ashare 38→35/59→56，gold 20→19/29→28），**数值逐位不变**；详情卡「数据新鲜度」行名变 `fed_rate` 这种小写名 |
| v5.48 | 六列派生量沉进耦合层（ETF 四列 + 股息率 + 黄金两条流量），causual **不再计算、只装配**；股息率缺列改 `KeyError`（不再"告警 + 整列 NaN"） | 列名变 `etf_dispersion` / `size_flow` / `national_etf` / `national_etf_event` / `dividend_yield` / `cb_gold_buy` / `etf_flow`；**列数与数值都不变**（逐位比对过）。`series: dict` 通道彻底删除，`load_raw` / `build_panel` / `run` / `port.generate` 全部收 `DataFrame` |

---

## 9. 对外接口（给 service 层）

causual 不直接对外提供服务。service 通过三样东西使用它：

| 接口 | 位置 | 契约 | 谁调用 |
|---|---|---|---|
| `load_raw(panel)` | `causual_assets/{asset}/pipeline.py` | **只装配不取数**：按 `PANEL_COLS` 选列，`_derived` 把耦合层算好的派生列搬进来（缺列 `KeyError`） | `pipeline` / 测试 |
| `pipeline.run(asset, panel, *, meta, state, asof, quiet)` | `causual_core/pipeline.py` | 出卡；**只吃数据，不取数**。`meta` 可选，只给新鲜度提示与月频归档月 | `causual.port.CausalPort` |
| `required_tables(asset)` | `causual/port.py` | 该资产要哪些表（纯声明，= `tables_of(PANEL_KIND)`） | CLI / service 装配点 |
| `panel_kind(asset)` | `causual/port.py` | 该资产要哪个耦合层面板（纯查表，**不取数**）；装配点拿它 `get_data(...)` | CLI / service / web |
| `macro_month(meta, *, asset=None)` | `causual/port.py` | 月频卡该覆盖的月份；`asset` 是**关键字参数**（传了就按该资产的表收窄） | service（`exists` 之前） |
| `generate / generate_macro(asset, panel, *, month, date, state, meta)` | `causual/port.py` | 出卡；`panel` 由装配点取好 | service |
| `list_cards / get_card / to_html` | `causual/port.py` | 卡片检索与 Markdown→HTML | `causual/web/routes.py` |
| `register_module()` | `web/routes.py` | 返回 `Module`（router + templates + nav + port） | `service.registry.load()` |

> `derived_series(asset, data)` 已于 2026-11-16 **退役**：股息率是面板列，
> 装配点直接 `get_data(...)["dividend_yield"]`，不再需要"装配派生量"这一步。

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
