# causual —— 因果归因层

> **更新日期：2026-11-14**
> 多代理协作约束见 [`../../AGENTS.md`](../../AGENTS.md)。

把宏观、制度与资金关系写成一张**显式的因果图**，每天算一次"这些机制现在把标的往哪边推"，
输出成一张人能读、能反驳的卡片，用来**辅助主观判断**（不是收益预测模型）。

支持两个资产：**国内黄金**（`gold`）与 **A 股中证全指**（`ashare`）。

> 项目整体架构、模块地图、依赖方向见工作空间根目录的 [`../../README.md`](../../README.md)。
> 本文件只讲 causual 自己：怎么跑、卡片长什么样、边界在哪。

> **文档导航**
> - 本文件：causual 的用法与卡片说明
> - [`DESIGN.md`](DESIGN.md)：causual 内部架构、文件职责、调用顺序
> - [`CHANGES.md`](CHANGES.md)：逐版变更
> - [`PROBLEM.md`](PROBLEM.md)：已知数据陷阱
> - [`SIGNAL.md`](SIGNAL.md)：信号层与 α 的推导细节

---

## 1. causual 在干什么

| 是 | 不是 |
|---|---|
| 一个**分层归因引擎**：长期看结构、中期看供需、当天看信号 | 一个收益率预测模型 |
| 每天一张卡片：状态 + 归因 + 供需位置 + 失效条件 | 自动交易系统 |
| 双品种（黄金 / A 股）、日频 | 多资产组合 |
| **α 只做信号层的当期归因**（t 日读数 → t 日收益） | 仓位建议 |

**α 不解释全部涨跌**。它只覆盖日/周频的信号层；长期趋势（制度、供需）不进 α。
所以"边全正但市场在跌"是正常现象——缺的那部分在长期层。

---

## 2. 代码结构

```
causual/
├── causual_core/                # 资产无关的因果层（**不含取数代码**）
│   ├── edge.py / nodes.py       # DAG 结构与节点池
│   ├── estimators.py            # DML / 分布滞后 / 事件研究
│   ├── signal_layer.py          # 贡献 = 剂量 × β × z × gate × active × 纯度
│   ├── macro_layer.py           # 结构层 + 供需层（位置 × 边际）
│   ├── freshness.py             # 数据新鲜度（按 freq + lag_days 判过期）
│   ├── regime_fit.py            # 状态模型拟合
│   ├── regime_infer.py          # 今日后验 / 标签外推
│   ├── pipeline.py              # 出卡编排：面板 → 信号 → 叙事 → 落盘
│   ├── cli.py                   # 命令行：regime / edges / audit / daily
│   ├── narrative.py             # 卡片渲染（总览 / 详情 / 控制台）
│   ├── card_split.py            # 把完整卡拆成日频卡 / 月频卡
│   ├── snapshot.py / evaluate.py
│   └── utils/{calendar,zscore}.py
├── causual_assets/
│   ├── common.py                # 面板组装公用件（列名映射 + 公布滞后）
│   ├── ashare/{panel,pipeline,config,label}.py
│   └── gold/{panel,pipeline,config,label}.py
├── causual_cards.py             # **对外接口**：list_cards / get_card / to_html
├── web.py                       # **对外接口**：FastAPI 路由（供 service 挂载）
├── templates/                   # 卡片页面模板（extends service 的 base.html）
├── data/{regime,snapshots}/     # 模型产物
└── output/{asset}/cards/YYYY/MM/
```

**状态标签**由 `diagnose` 层提供（现在仍由 `regime_engine` 产出，但 causual 只把它当普通数据列用，不 import 诊断层）。

`causual_core` 里没有 `requests` / `akshare` / `db.read`——取数在 `datahub`，
面板组装在 `causual_assets`，两者通过注入的 `DataService` 连接。

---

## 2.1 与 service 层的接口

causual 不直接对外提供服务，而是暴露三样东西让 service 挂载：

| 接口 | 位置 | 契约 |
|---|---|---|
| `panel.panel(ds=None)` | `causual_assets/{asset}/panel.py` | 传入 `DataService`，返回日频宽表 |
| `pipeline.run(asset, series, meta, asof=None, quiet=...)` | `causual_core/pipeline.py` | 出卡：**只吃数据，不取数** |
| `list_cards(asset, start, end)` / `get_card(asset, date, kind)` / `to_html(md)` | `causual_cards.py` | 卡片检索与渲染 |
| `register_module()` | `web.py` | 返回 `service.registry.Module`（路由 + 模板目录 + 导航） |

service 侧的 `CausalPort` 只调 `pipeline.run`，不碰 causual 内部。

**约束**：`causual_core` 不 import service；`DataService` 由调用方注入。

---

## 3. 快速开始

```bash
# 依赖（装进 global_venv）
uv pip install --python D:\desktop\workspace\global_venv\Scripts\python.exe ^
    numpy pandas scikit-learn statsmodels hmmlearn econml scipy akshare xlrd openpyxl

cd D:\desktop\workspace\quant
set PYTHONPATH=D:\desktop\workspace\quant
```

**日常（秒级，读已有快照 + 外推最新标签）**：

```bash
python -m causual.causual_core.daily --asset ashare
python -m causual.causual_core.daily --asset gold
```

**维护（数据更新 / 结构变化时）**：

```bash
python -m causual.causual_core.cli regime --asset ashare   # 重拟状态模型（含极端规则）
python -m causual.causual_core.cli edges  --asset ashare   # 用新标签重估所有边 → 快照
python -m causual.causual_core.cli audit  --asset ashare   # 方向一致性审计
```

**数据抓取**（各资产独立）：

```bash
python -m causual.causual_assets.ashare.fetch
python -m causual.causual_assets.gold.fetch
```

`daily` 的拒绝条件（fail-closed）：没有状态模型 / 没有边快照 /
快照签名与当前标签体系不一致 / 快照超过 250 天未重估。

产出：

```
output/{asset}/cards/card_YYYY-MM-DD.md      # 每日状态卡（主产出）
output/{asset}/cards/detail_YYYY-MM-DD.md    # 模型详情（β、CI、纯度、滞后路径）
data/regime/{asset}/current.json             # 状态模型快照（复现用）
data/snapshots/{asset}/edges_*.json          # 边估计快照
saved_models/{asset}/hmm.pkl                 # 全样本模型
saved_models/{asset}/hmm_last.pkl            # 最后窗口模型（在线后验/标签外推）
```

**手工维护的数据**（无法自动抓取，需人工更新）：

| 文件 | 用途 | 频率 |
|---|---|---|
| `data/manual/cb_gold_monthly.csv` | 央行购金（WGC 87 国固定篮子） | 月度 |
| `data/manual/equity_supply_monthly.csv` | A 股净融资额（增发/减持/IPO） | 月度 |
| `data/manual/ETF_Flows_*.xlsx` | ETF 份额历史（东财导出） | 按需 |
| `data/manual/市场概况_*.xlsx` | 股东/高管增持（东财导出） | 月度 |
| `data/manual/gold_Q1_annually/gold_Q1_YYYY.xlsx` | 黄金季度供需（WGC） | 季度 |

---

## 4. 数据

**所有数据都来自 `datahub`**——一个同级独立包，SQLite 存储。causual 不再自己抓数据、
不读 CSV 面板，也不依赖 `data/` 下的任何文件。

### 4.1 取数路径

```
datahub/datahub.sqlite
   ↓  datahub.db.read(...)
causual_assets/{ashare,gold}/panel.py      列名映射 + 公布滞后 + 派生列
   ↓  panel.panel()
causual_assets/{ashare,gold}/pipeline.py   prepare() 算特征与供需层
   ↓
regime（状态） / signal（边） / narrative（卡片）
```

`panel.py` 是唯一知道"数据层表名 ↔ 业务层列名"的地方；`causual_core` **不含任何取数代码**。

### 4.2 面板契约

`panel.panel()` 返回日频宽表：

- index = 该资产主价格表的交易日（A 股 `csi_all`，黄金 `gold_cny`）
- 列名沿用大写旧名（`CLOSE`、`VIX`、`M1_LEVEL`…），供 `prepare()` 直接使用
- 月频/季频变量按 `metadata.lag_days` **推后**再 ffill

例：2026-09-30 的 CPI，`lag_days=9` → 10-09 才进入面板。**这不是消费层的假设，
是数据层的属性**，所以放在 `metadata` 而不是写死在代码里。

### 4.3 派生列（数据层不存）

| 列 | 来源 |
|---|---|
| `SIZE_FLOW` / `ETF_DISPERSION` / `NATIONAL_ETF` / `NATIONAL_ETF_EVENT` | 由 `etf_shares` 现算 |
| `CB_GOLD_BUY` | `cb_holdings.diff()` |
| `ETF_FLOW` | `gold_etf_holdings.diff()` |
| `PAYOUT_RATIO` | 读 `dividend_annual`，取**上一年度**值（避免前视） |

判定标准：**去掉它之后能不能用库里其他数据算出来**。算得出来的就不入库。

> **坑：ETF 代码按裸 6 位归一。** 数据层的 `etf_shares` 键是 `etf_shares:sh510300`
> （2026-11-14 起带市场前缀），而 `GROUPS` 写的是裸码。`panel.bare_code()` 把
> `sh510300` / `c510300` / `510300.SH` 一律归一成 `510300`，**列名与 `GROUPS` 两侧
> 同口径比对**——否则四列派生量会整体消失，而且不报错（详见 CHANGES v5.40）。
> `tests/test_causual_panel_derived.py` 盯着这件事。

> **坑：差分必须在原始频率上做。** 不能对日频 ffill 后的列再 `resample().diff()`——
> 月末/季末落点依赖交易日，会错位一到两期（实测 ETF 流入相关性从 0.999 掉到 0.32）。

### 4.4 更新数据

```bash
python -m datahub.fin.update                       # 自动源（FRED / akshare / CFTC / XAUS / WTM）
python -m datahub.fin.intake wgc <xlsx> --load     # 手工源：WGC 季报
python -m datahub.fin.intake wgc_etf <xlsx> --load # 手工源：WGC ETF 月报
python -m datahub.fin.check                        # 完整性（按交易日历判缺）
```

### 4.5 已知数据问题

| 变量 | 问题 |
|---|---|
| `industrial_yoy`、`exports_yoy` | akshare 只有财经日历接口，停在 2025-08 |
| `geopolitic_risk` | 源已停更，旧序列冻结；新数据改用 `world_tension`（2026-03 起） |
| `cb_holdings` | 2026Q2 有 23/87 国 `AWAITED`，按 Q1 补齐 —— 结果是**低估** |
| `gold_cpi_surprise` | 标准 AR(1) 配方与旧序列相关性 0.75（同源不同参数） |

---

## 5. DAG 结构

> v5.0 起是三模块架构。**新代码只在下面这三个包里**；
> 第 4.x 节描述的是黄金资产插件内部的边表。

```
regime_engine/            独立顶级包：通用隐状态引擎（不含资产知识）
├── api.py                fit_regime(features_df, k, seed) —— 唯一入口
├── hmm.py  select.py  bhattacharyya.py
└── serialize.py          saved_models/{asset}/hmm.pkl（纯参数字典）

causual/causual_core/     与资产无关的因果内核
├── nodes.py              全局节点池
├── edge.py               Edge 数据类
├── calendar.py           align_to_trading_calendar
├── estimators.py         DML / 分布滞后 / 事件研究
├── signal.py  narrative.py  snapshot.py  evaluate.py
├── cli.py                python -m causual.causual_core.cli daily --asset gold
└── cli.py                regime / edges / audit

causual/causual_assets/   资产插件
├── gold/                 config(11 边) fetch label pipeline
└── ashare/               config(12 边) fetch label pipeline
```

日常三条命令：

```bash
python -m causual.causual_core.daily --asset gold
python -m causual.causual_core.daily --asset ashare

# 维护
python -m causual.causual_core.cli regime --asset gold     # 重拟状态模型
python -m causual.causual_core.cli edges  --asset gold     # 重估边 → 快照
python tests/run_tests.py                                  # 验收测试
```

### 4.0 旧版结构（v4，已废弃）

```
causual/
├── paths.py        因果 DAG（16 条边、标签、权重、失效条件、发布延迟）
├── fetch.py        真实数据抓取与面板拼接（缓存、重试、覆盖率报告）
├── data.py         校验 / 发布延迟 / 合成数据（带真值）
├── regime/         v4 的状态模块（已被 regime_engine 取代）
├── estimators.py   LinearDML（默认）/ CausalForest（可选）/ 分布滞后 / 事件研究
├── signal.py       逐日信号 = 剂量 × 敏感性；两口径稳健性
├── narrative.py    每日因果卡片
├── evaluate.py     Rank IC / 分层 / 参数回收 / 符号审计
├── snapshot.py     边模型快照（只存系数，不存模型对象）
├── cli.py          命令行入口（regime / edges / audit / daily）
├── pipeline.py     出卡编排（不拟合任何模型）
└── causual.py      研究/回测编排 + CLI
```

### 4.1 三层节点

- **外生**：`FED_RATE` `CPI_SURPRISE` `USD_INDEX` `GEOPOLITIC_RISK` `CB_GOLD_BUY`
- **中间**：`REAL_YIELD_10Y` `VIX` `USDCNY` `ETF_FLOW` `COMEX_INVENTORY`
  `MINE_SUPPLY` `JEWELRY_DEMAND` `OPEN_INTEREST` `ETF_PREMIUM`
- **终端**：`GOLD_CNY`

### 4.2 11 条边

`层` 分为 **因果**（机制有明确先后）与 **关联**（与金价同期共同决定，只能算条件关联 + 滞后传导）。
标 ★ 的边**不进入 α**（样本不足或符号不可靠），只做结构展示与符号审计。

| 源 → 目标 | 机制 | 方向 | 权重 | 频率 | 层 | 估计器 |
|---|---|---|---|---|---|---|
| FED_RATE → REAL_YIELD_10Y | 政策利率推高实际收益率 | +1 | .85 | 日 | 因果 | 事件研究 |
| CPI_SURPRISE → FED_RATE | 通胀驱动政策反应 | +1 | .50 | 月 | 因果 | 事件研究 |
| REAL_YIELD_10Y → GOLD_CNY | 持有黄金的机会成本 | −1 | .85 | 日 | 因果 | DML |
| USD_INDEX → USDCNY | 美元向人民币传导 | +1 | .70 | 日 | 因果 | DML |
| USDCNY → GOLD_CNY | **人民币计价渠道（汇率主通道）** | +1 | .80 | 日 | 因果 | DML |
| GEOPOLITIC_RISK → GOLD_CNY | 避险资本流 | +1 | .35 | 事件 | 因果 | 事件研究 |
| VIX → GOLD_CNY | 避险需求 | +1 | .40 | 日 | 因果 | DML |
| CB_GOLD_BUY → GOLD_CNY | 央行结构性需求 | +1 | .55 | **月** | 因果 | 分布滞后 |
| ETF_FLOW → GOLD_CNY ★ | ETF 净流入 | +1 | .50 | **月** | 因果 | 分布滞后 |
| OPEN_INTEREST → GOLD_CNY | 沪金持仓拥挤度 | −1 | .25 | 日 | **关联** | DML |
| COT_NET_LONG → GOLD_CNY | CFTC 非商业净多头 | −1 | .35 | **周** | **关联** | DML |

**已删除（v4.1）**：`XAU_USD → GOLD_CNY` 与 `USD_INDEX → XAU_USD`。
国际金价与国内金价是近似恒等式（`GOLD_CNY = XAU × USDCNY / 31.1035 ×(1+溢价)`），
当因果边既无经济含义、又会以 β≈+0.6 压住其他机制。`XAU_USD` 现在只作为
**混杂控制变量**留在 `confounders` 池里（阻断「美元 → 汇率 → 金价」的后门路径），
并在卡片的「今日归因」里作为**参照线**出现，不进 α、不做因果解释。

**已删除（v4.2）**：`JEWELRY_DEMAND`、`TECH_DEMAND`、`MINE_SUPPLY` 三条季频边。
它们只有 WGC 季频数据（~35 个季度），去趋势后全部不显著，且合计只占黄金需求的小部分；
恢复时需把三列加回 `REQUIRED_COLUMNS`。

三个刻意的设计：

1. **`direction` 只审计，不参与乘法**。估计符号与理论不符时在卡片标 ⚠，不强行翻转。
2. **`magnitude` 是主观先验**，没有统计依据，只决定相对权重（改它会线性改变贡献）。
3. **`regime_valid` 存标签而非 HMM 编号**。编号会随数据漂移，标签不会。

已删除：`ETF_PREMIUM → GOLD`（境内 ETF 溢价长期贴 0，信息量不足，用 `COT_NET_LONG` 替代）。
未入图：`RECYCLED_SUPPLY`（回收金）——它由价格驱动（价高→回收多），作为"原因"是反向因果。

---

## 6. 三层结构（v5.15 起）

分层的依据是**因果深度**，不是数据频率。

价值与资金的供需层、制度与政策的结构层，更接近股市涨跌的**深层结构**；
信号层是表层扰动。层次越深，数据的自然频率通常越低——**频率是深度的副产品，
不是分层依据**。所以不要因为"VIX 是日频、社融是月频"就以为分层是为了对齐频率。

| 层 | 内容 | 典型频率 | 进 α | 估计方式 |
|---|---|---|---|---|
| `structure` | 制度、政策态度 | 年/事件 | 否 | 只有定性：判断 + 证据 + **证伪条件** |
| `supply` | 资金与筹码的供需、价值、大股东态度、央行购金 | 月/季 | 否 | 描述统计（同比 / 分位 / 同期相关 / 自相关） |
| `signal` | VIX、汇率、Shibor、两融、国家队、放量 | 日/周 | **仅此层** | 因果回归（DML / 分布滞后 / 事件研究） |

**α 只覆盖信号层。** 所以出现「边全正但市场在跌」时不是模型出错——缺的那部分在
长期层，它本来就不该进当日归因。

结构层是人工维护的：`data/manual/structural_state.csv`，列
`date, area, judgement, evidence, falsifier, stance`。每条都必须写**证伪条件**，
否则定性判断会退化成"事后怎么都能解释"的叙事。

供需层明确标注"不是因果估计"：它只回答"这个指标处在什么位置、和市场正相关还是负相关、
它自己有多粘"。

### 6.0 为什么没有"传导结构"（v5.21 移除）

早期版本给过 60/120/180 日条件均值差（指标高时未来 h 日收益 − 低时），后来**移除了**：

高自相关的序列（CPI 的 AR1 = 0.97、PPI = 0.99）在任何滞后阶都会测出相关，
但那是**序列自身的延续性**，不是传导。用未来收益做条件均值，混进去的是
"因素粘性 + 股市均值回归"，不是"这个因素花了多久传到股市"。

所以现在只保留三样能说清的：

| 列 | 含义 |
|---|---|
| 同比(趋势) | 指标自身的变化方向（各类口径见 6.2） |
| 与股市同期相关 | 同一个月「指标值 vs 当月收益」的相关，**同期、非领先、非因果** |
| 自相关(AR1) | 因素自身的粘性。>0.8 高粘性 / 0.5~0.8 中 / <0.5 低 |

传导速度目前无法定量给出，详见卡片第 3 节的说明。

### 6.1 供需层的两个维度（v5.28 起）

| 维度 | 问的是 | 口径 |
|---|---|---|
| **位置** | 在自身历史的什么位置 | 原始值 / 滚动和 的分位 |
| **边际** | 在变好还是变差 | 同比 / 环比差 / 同比 MA3 一阶差 的分位 |

两者都只给分位，不给绝对值——不同量纲没法横向比。窗口统一（最多 60 期）。

**读法按方向分化**：

| 位置 | 边际 | 正向指标（+） | 负向指标（−） |
|---|---|---|---|
| 高 | 改善 | 强势且仍在增强（拥挤度上升） | 压力持续加大 |
| 高 | 恶化 | 见顶信号 | 压力见顶（转好） |
| 低 | 改善 | 左侧机会 | 压力开始累积 |
| 低 | 恶化 | 右侧回避 | 压力出清中 |

方向为「不定」的（如 CPI）不做自动判读。

### 6.2 四个小节

| 小节 | 内容 |
|---|---|
| 3.1 供需总量 | M1 / 总市值 |
| 3.2 供需增量 | ΔM1、净供给（融资−增持） |
| 3.3 宏观环境 | 社零、工业增加值、PMI、M1−M2、CPI、PPI、出口 |
| 3.4 增长转化 | 分红/净利润、大股东净增持 |

### 6.3 为什么不再给"传导结构"

早期版本给过 60/120/180 日条件均值差，v5.21 移除：

高自相关的序列（CPI 的 AR1 = 0.97、PPI = 0.99）在任何滞后阶都会测出相关，
但那是**序列自身的延续性**，不是传导。用未来收益做条件均值，混进去的是
"因素粘性 + 股市均值回归"，不是"这个因素花了多久传到股市"。

现在只保留三样能说清的：同比趋势、同期相关（已不展示）、自相关。

## 7. 卡片导读

`output/{asset}/cards/card_YYYY-MM-DD.md`，按层分节（无内容的层自动跳过并重新编号）：

| 节 | 回答的问题 | 怎么用 |
|---|---|---|
| 1 市场状态 | 现在什么状态？后验、黏性、模型多旧 | 先确认状态可信（Bhattacharyya、模型时效） |
| 2 结构层 | 制度/政策处于什么状态 | 只有定性，看证伪条件什么时候会被触发 |
| 3 供需层（3.1~3.4） | 总量/增量/宏观/转化各自在什么位置 | 看分位与传导结构，不看单点数值 |
| 4 净指向分解（信号层） | 今天哪几条机制在贡献 | **主看这节**：看机制，不看 α |

第 4 节的事件边会标出**滞后期**（如「窗口 1~5 日」）和**是否激活**；
关联型边（如放量）标「关联·方向不定」，不做方向冲突判定。

`detail_YYYY-MM-DD.md` 八节：状态模型、边清单、**供需层明细（位置/边际的原始值与窗口）**、
分状态 β/CI、分布滞后逐期系数（lag0 进 α，lag1+ 仅参考）、贡献计算、数据新鲜度、
**供需层口径说明（含 2×2 读法矩阵）**。

---

## 8. 日常命令

```bash
# 出卡（秒级，读快照 + 外推最新标签）
python -m causual.causual_core.daily --asset ashare
python -m causual.causual_core.daily --asset gold

# 数据（在 datahub 里）
python -m datahub.fin.update
python -m datahub.fin.check

# 维护
python -m causual.causual_core.cli regime --asset ashare   # 重拟状态模型
python -m causual.causual_core.cli edges  --asset ashare   # 重估边 → 快照
python -m causual.causual_core.cli audit  --asset ashare   # 方向一致性审计

# 年度分红率（缓存，最多重跑一次/年）
python -m causual.causual_assets.ashare.dividend
```

**手工维护的数据**（全部在 `datahub/intake/`）：

| 文件 | 内容 | 更新方式 |
|---|---|---|
| `raw/ETF_Flows_*.xlsx` | WGC ETF 月报 | `python -m datahub.fin.intake wgc_etf <file> --load` |
| `raw/GDT / WGC 季报` | WGC 央行储备季报 | `python -m datahub.fin.intake wgc <file> --load` |
| `clean/insider_net.csv` | 内部人净增持（东财导出） | 手工追加行 |
| `clean/equity_fin_monthly.csv` | 人行社融表解析后导出 | 手工追加行 |

`data/manual/structural_state.csv` 仍是结构层的定性判断来源（文本，不入库）。

---

## 9. 评估口径

| 指标 | 定义 | 为什么 |
|---|---|---|
| **Rank IC** | `Spearman(α_t, r_{t+h})`，带 20 日块 bootstrap 95%CI | α 是方向判断，排序一致性比幅度拟合更贴切；日频自相关强，需块重采样 |
| Rank IC 稳定性 | 样本外分 4 段，看均值/标准差/同号比例 | 单点 IC 可能是运气 |
| 分层收益 | α 五分位对应的未来收益 | 检查单调性，而不只是相关系数 |
| 参数回收 | 合成数据上估计值 vs DGP 真值 + CI 是否覆盖 | 唯一能验证"估计器在估什么"的硬标准 |

`--synthetic` 参考结果（seed=42，**只证明管线正确，不代表真实可得收益**）：

| 指标 | 结果 |
|---|---|
| 状态数 | K=3（BIC 容忍带 + Bhattacharyya ≥ 0.4） |
| 最小 Bhattacharyya | ≈1.06 |
| Rank IC（次日） | 0.238，95%CI [0.174, 0.286] |
| Rank IC 稳定性 | 四段 0.262 / 0.164 / 0.297 / 0.217，均值 0.235，同号 100% |
| 分层（次日） | Q1 −21.8bp → Q5 +47.2bp，单调，Q5−Q1 +0.69% |
| 参数回收 | 各边 95% CI 基本覆盖 DGP 真值 |

> 合成数据的信噪比是人为设定的。真实数据上，日频单品种的 Rank IC 能稳定在
> 0.03~0.05 就已经不错；这里 0.24 是 DGP 决定的，不是框架的功劳。

### 真实数据（2016-12 ~ 2026-10，2382 个交易日，v3.1）

| 指标 | 结果 |
|---|---|
| 数据覆盖 | 23 列中 22 列自动（COMEX 库存边已注释，不再需要） |
| 状态数 | K=4（Bhattacharyya 1.13） |
| 滚动重估 | 15 次，样本外 1823 天（2019-04 起），最近重估 2026-04-21 |
| 可用边 | 14 条直接 + 4 条上游 |
| 结构估计 | `XAU_USD→GOLD` β=+0.65、`RY→GOLD` β=−0.28（符号均正确） |
| Rank IC（收盘→收盘，次日） | 0.523 |
| Rank IC（**开盘→收盘**，次日） | **0.414**，95%CI [0.362, 0.464] |

⚠ **0.41 这个数字我仍不敢当作可交易 alpha**，理由见下。

五个值得记录的真实数据发现：

1. **时区造成的假 alpha（最重要）**。COMEX/美债/VIX 的 t 日收盘在上金所收盘之后。
   直接把 t 日面板当信号，Rank IC 0.53；整体右移一天后掉到 −0.03——那 0.5 全是
   「用了当时还不存在的信息」。
2. **开盘价不是「等于前收」**。实测上金所 Au99.99 开盘等于前收的只占 5.3%，
   与 AU0 开盘的缺口相关只有 0.35，说明开盘**部分**反映隔夜外盘、剩余在日内补涨。
   这正是 `fwd_oc` 高 IC 的可能来源。
3. **合成数据复现不出这个效应**。同样的 DAG、同样的 30% 滞后结构，合成数据的
   `fwd_oc1` 只有 0.067，而真实数据是 0.414。两者差 6 倍，说明高 IC 不是"因果传导"
   带来的，而是真实市场里 SGE 开盘滞后于外盘的**微观结构**效应。
4. **实际利率的效应是隔夜显现的**。`RY_CHG` 与当日金价相关 −0.09，与**次日** −0.21。
5. **持仓拥挤的反转假设被数据否定**。`OI_CHG` 与金价同日相关 +0.17，方向与 DAG 相反，
   正好命中该边 `falsifier` 里写的情形——这是一条应该被改写的边。
   未解决，已在 CHANGES 里记录。

---

## 10. 已知限制

| 限制 | 影响 |
|---|---|
| `industrial_yoy` / `exports_yoy` 停在 2025-08 | 卡片第 3.3 节这两行是陈数据 |
| `geopolitic_risk` 源已停更 | 旧序列冻结；新数据用 `world_tension`（2026-03 起，仅 7 个月） |
| `cb_holdings` 2026Q2 有 23/87 国 `AWAITED` | 按 Q1 补齐 → 净购金**低估** |
| 季频供需只有 ~42 个季度观测 | 央行购金的置信区间宽，单条边难以显著 |
| 慢变量用固定期数摊销 | 真实滞后分布被压平 |
| 结构层是人工文本 | 与供需层的实证可能互相矛盾，卡片不会自动调和 |
| 状态模型靠外推 | 模型超过 2 个月没重训就会失真，卡片只标"龄期"不阻断 |

## 11. 使用建议

1. 每天读第 2 节：看**哪条机制在推动**，而不是 α 的数值。
2. 读第 4 节：符号冲突是最有价值的信号——要么 DAG 错了，要么市场结构变了。
3. 读第 8 节：它告诉你"什么情况下该推翻这个判断"，这是量化模型最缺的东西。
4. 把 `α`/bp 只当作方向倾斜与事后归因的依据，不当作收益率预测。

---

## 12. 版本

当前 v5.40（2026-11-14）。逐版变更见 [`CHANGES.md`](CHANGES.md)。
