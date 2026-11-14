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
pipeline.run(asset, series, meta, state=state)
```

`series` 与 `meta` 也全部由调用方装好——`causual_core` 里没有取数代码。

卡片里的**市场状态只留一行**，画像与黏性在 `/state` 页（归状态层）。

---

## 3. 数据流

```
datahub.sqlite
     │  db.read()
     ▼
causual_assets/{ashare,gold}/panel.py     ← 唯一知道"表名 ↔ 列名"的地方
     │  panel.panel()  → 日频宽表（大写列名）
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
| `common.py` | 面板组装公用件：列名映射、按 `lag_days` 推后、日频化 |
| `ashare/panel.py` | A 股字段映射 + 派生列（ETF 分流/分歧度/国家队）；ETF 代码按**裸 6 位**归一（两侧同口径，数据层换前缀不影响匹配） |
| `ashare/pipeline.py` | `prepare()` 算特征、供需层、分红率 |
| `ashare/config.py` | 10 条边的定义（机制、方向、估计器、是否进 α） |
| `ashare/label.py` | 状态标签命名与极值状态 |
| `gold/*` | 同结构，黄金侧 |

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
  ├─ pipeline.load_raw()        → panel.panel()        # 从 datahub 拼日频宽表
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
| 新节点 | `nodes.py` 定义 + `panel.py` 映射 + `config.py` 引用 |
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
| `STUB_COLS` 里的 `EQUITY_SUPPLY` 恒 NaN → `EQUITY_SUPPLY` 边 `n=0` | 该边在卡片里永远"未估计"，但不会报错 | 接上真实月度净融资额，或把边标成「无数据」 |

### 8.1 识别口径的坑（都是静默的）

派生列 / 特征列消失时**不会抛异常**，只会让卡片"少一行"。两条防线：

| 防线 | 位置 |
|---|---|
| 代码归一 `bare_code()`：列名与 `GROUPS` 两侧同口径 | `ashare/panel.py` |
| 计数盘点：`REQUIRED_TABLES` 有数据 + 派生列新鲜 + 每条边的列都在面板里 | `tests/test_causual_panel_derived.py` |

**监控清单也会静默失效**：`freshness.WATCH*` 里的表名一旦与数据层不一致，
`check()` 就 `continue`，于是"没有过期表"这个结论永远成立。所以：

| 约定 | 为什么 |
|---|---|
| 清单里的名字必须真实存在于 `DataPort.tables()` | 名字漂移 = 监控失效 |
| 清单里的表 `freq` 必须是 D/M/Q/Y | 其它频率 `_period_end()` 返回 None，同样静默跳过 |
| 清单**只有一份** `MONTH_TABLES`，`pipeline._month_of()` 也读它 | 原先两处各抄一份，一起烂掉 |
| `check()` 遇到认不出的名字要 **stderr 告警** | 别让"没报错"被当成"没问题" |
| `scoped()` 按 `pkg.REQUIRED_TABLES` 收窄 | `metadata()` 是全局的，A 股卡不该报黄金表 |

守卫：`tests/test_freshness_watchlist.py`（清单存在性 + freq 可判 + 行为验证 + 告警）。

---

## 9. 对外接口（给 service 层）

causual 不直接对外提供服务。service 通过三样东西使用它：

| 接口 | 位置 | 契约 | 谁调用 |
|---|---|---|---|
| `panel(ds=None)` | `causual_assets/{asset}/panel.py` | 传 `DataService`，返回日频宽表 | `pipeline` |
| `pipeline.run(asset, series, meta, asof, quiet)` | `causual_core/pipeline.py` | 出卡；**只吃数据，不取数** | `causual.port.CausalPort` |
| `required_tables(asset)` | `causual/port.py` | 该资产要哪些表（纯声明，调用方负责读） | CLI / service 装配点 |
| `derived_series(asset, data)` | `causual/port.py` | 数据层**现算的派生量**（股息率）→ `{"表名:列名": Series}`，直接 `series.update(...)` | CLI / service 装配点（**唯一入口**，两边别再各拼一份） |
| `macro_month(meta, *, asset=None)` | `causual/port.py` | 月频卡该覆盖的月份；`asset` 是**关键字参数**（传了就按该资产的表收窄） | service（`exists` 之前） |
| `list_cards / get_card / to_html` | `causual/port.py` | 卡片检索与 Markdown→HTML | `causual/web/routes.py` |
| `register_module()` | `web/routes.py` | 返回 `Module`（router + templates + nav + port） | `service.registry.load()` |

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
