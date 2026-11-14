# datahub 设计文档

> **更新日期：2026-11-14**　｜　使用说明见 [`README.md`](README.md)

---

## 1. 一句话定位

**把五花八门的外部数据，变成一张张"一看就懂、拿来就用"的表。**
它不判断数据有没有用、不计算派生指标、不关心谁在消费。

三条硬约束：

1. **一个表 = 一条（或一组同源同频的）序列**
2. **写接口不对外**：对外只有 `fin.read`（进程内）与 `port.DataPort`（跨模块），
   写库只发生在 `update` / `intake` 内部
3. **数据层不依赖任何上游包**（`causual` 不能反过来被 import）

---

## 2. 分层与数据流

```
            外部世界
   FRED / akshare / CFTC / XAUS / WTM / WGC 文件
                    │
        ┌───────────┴────────────┐
        │  sources.py (SOURCES)  │  自动源注册表
        │  intake/ (parsers)     │  手工源解析
        └───────────┬────────────┘
                    │  update.py / intake/cli.py
                    ▼
          datahub.sqlite
          ├── metadata   每个变量的元信息
          ├── missing    已确认的正常缺口
          ├── sync_log   事件型表的上次抓取时间
          └── <表>       一表一序列
                    │
                    │  read() / port.DataPort
                    ▼
              任何消费方
```

**关键分界**：`sources.py` 只负责"从哪拿"，`update.py` 只负责"怎么写"。
两者之间只有 `Series / DataFrame` 一种约定，没有别的情感纠葛。

---

## 3. 文件职责

```
datahub/
├── __init__.py            只放 ROOT / DB_PATH / __version__（+ 读仓库根 .env）
├── port.py                **唯一对外面**：DataPort / get_port，其他模块只能走这里
├── core/
│   ├── conn.py            connect / transaction / connection / exec_ddl
│   ├── schema.py          DATA_DDL / OPS_DDL / METADATA_MIGRATIONS（DDL 与改表唯一出处）
│   └── errors.py          DatahubError / TableNotFound / OpsTableAccess / SchemaError
├── fin/
│   ├── __init__.py        对外：read / read_many / read_long / metadata / tables / run_check / missing
│   ├── read.py            读接口 + **四种表结构**（single/multi/long/static）+ pivot
│   ├── update.py          写库（四种 kind 各一个 writer）+ 按频率跳过 + 元数据
│   ├── sources.py         取数函数 + SOURCES 注册表（含 ashareapi 客户端）
│   ├── check.py           交易日历 + 完整性检查
│   ├── derive.py          派生量（市场宽度），**不落表**、不注册成源
│   ├── write.py           单值 upsert（intake 用）
│   └── intake/            raw / clean / parsers / registry / cli / stock_daily
├── ops/
│   ├── __init__.py        对外：OpsStore
│   └── store.py           状态 + 消息，单事务 save_result
└── web/
    └── routes.py          /data 页（表清单 + 手工录入），经 port 取数
```

| 文件 | 职责 | 谁调用 |
|---|---|---|
| `port.py` | 跨模块唯一面：读、装配、受控动作（update / intake）；`series_map` 跳过 static 表并告警 | service / 各模块 cli / web |
| `core/conn.py` | 连接超时、事务边界、DDL 执行 | fin / ops |
| `core/schema.py` | 所有 DDL 的唯一出处 + 老库补列（`migrate_metadata`） | update / intake / ops |
| `fin/read.py` | 只读 API + 表结构定义（列名与类型） | 服务层 / 脚本 / port |
| `fin/sources.py` | "从哪拿"（含外部 API 客户端） | update |
| `fin/update.py` | "怎么写"（四种 kind + 元数据 + 该不该跑） | 人 / 服务层 / port |
| `fin/check.py` | 缺口判断（日历驱动；低频表按周期比，报「同月多行」） | 状态机 / 人 / port |
| `fin/derive.py` | 派生量现算（市场宽度），不建表、不注册成源 | 消费方 / 脚本 |
| `fin/write.py` | 单值/整表写入（手工录入页用） | `web/routes.py` |
| `fin/intake/` | 手工源解析 + 一次性建库脚本 | `web/routes.py` / 人 |
| `ops/store.py` | 运维状态持久化 | Manager |
| `web/routes.py` | `/data` 页：清单 + 上传 + 录一个数 | `service.registry` |

**隔离原则**：`ops` 不 import `service`（消息以 dict 传入）；
`fin.tables()` 只读 `metadata`，所以 `ops_*` 永远不会混进金融表清单。

## 4. 关键设计决定

### 4.1 为什么一表一序列，而不是宽表

宽表（一行日期、几十列）看着省事，但：
- 每列的频率不同（日/周/月/季），宽表必然要 ffill，**ffill 出来的假行会污染分位与差分**
- 加一列要改表结构，SQLite 的 `ALTER TABLE` 又笨

代价是表多（当前 45+ 张），但每张表的语义单一，`SELECT * FROM vix` 就是全部。

**例外**：同源、同频、同生命周期的多列才合并（OHLCV、ETF 份额）。

### 4.2 为什么频率要还原

历史面板是日频 ffill 过的，月频变量每天一行。若原样入库，`cpi_yoy` 就成了"日频"，
之后任何 `resample` 或分位计算都会错。所以入库前要还原成月末一行
（`sources._cn_month` 把统计局/央行的「月份」标签统一推到月末），
并且**截断尾部 ffill 撑出来的假行**（源数据 8 月停了，9/10 月仍被填成 8 月的值）。
当年做这件事的 `import_panels.py` 已删除，口径留在这里。

### 4.3 为什么要有 `lag_days`

「2026-09-30 的 CPI」指的是**9 月 CPI**，但它 10 月 9 日才公布。
如果消费层在 9-30 就用它，回测会偏乐观。`lag_days` 把这个"数据期 → 可见日"的滞后
变成数据层的属性，消费层照着推后即可。

### 4.4 为什么检查要用交易日历

"缺行"有两种：真的漏了，市场本来就没开市。用 SSE/NYSE 日历把后者排除后，
剩下的才需要人看。否则 A 股每年 ~10 天长假会造成几十张表同时告警，告警就废了。

### 4.5 为什么 intake 要分 raw / clean / parsers

手工数据来源太杂（WGC 的 xlsx、东财的 csv、新闻里看到的一个数）。分开的理由：

- `raw/` 原样留档 → 半年后能回溯"当时文件长什么样"
- `clean/` 是可审阅的中间产物 → 入库前能人眼扫一遍
- 入库可重放 → 写错了删掉重跑

`clean/` 只放**要写的行**（新增 + 被修订），不是全量快照——否则无法区分"这次真的更新了"
和"又写了一遍没变的数"。

---

## 5. 日常调用顺序

### 5.1 每天（自动源）

```
python -m datahub.fin.update
  │
  ├─ for s in SOURCES if s.enabled:
  │     obj = s.func(start=, end=, table=, **s.params)     # sources.py
  │     if "|" in s.table: 按列拆成多张单表
  │     _write(con, table, obj, end)                       # upsert，不删历史
  │     _apply_note(con, s, tables)                        # 把 note 落到 metadata
  │
  ├─ _ensure_meta(con)          # 补 metadata 行 + 老库补列 + 清掉误登的基础设施表
  ├─ refresh_metadata(con)      # 重算 start/end/n_rows（static 表只算行数）
  └─ 失败 → data/missing/update_failures.log
```

### 5.2 每月/每季（手工源）

```
① 把文件丢进 fin/intake/raw/
② python -m datahub.fin.intake wgc <file> --load
     ├─ registry.SOURCES["wgc"].parser(raw)                # 解析
     ├─ diff_to_write(new, 库里的旧值)                       # 只留新增 + 修订
     ├─ 写 fin/intake/clean/<table>.csv
     └─ upsert 入库 + 刷新 metadata
③ 解析失败 → 提示"raw 的结构可能变了"，指出哪一环对不上
```

### 5.3 检查

```
python -m datahub.fin.check
  ├─ 按 metadata.freq + market 生成"应有日期"（交易日历）
  ├─ 与实际日期做差（M/Q/Y 按**周期**比，不按天）
  ├─ 减掉 missing 表里已登记的缺口
  ├─ 低频表同期多行 → 报「同月多行」，算重复不算缺口
  └─ 分「近期缺」与「历史缺口」两档报告（只报不改）
```

### 5.4 消费方

```
from datahub.fin import read
s = read("vix")               # Series
d = read("csi_all")           # DataFrame
```

跨模块**不走这条**——其他模块只准 `from datahub.port import DataPort`（AGENTS §2①）。

---

## 6. 扩展点

| 想加什么 | 改哪里 |
|---|---|
| 新自动源 | `sources.py` 加一个 `def xxx(start, end, table="", **_)`，然后在 `SOURCES` 加一行（`shape=` 声明 long/static） |
| 新的 long/static 表 | `read.py` 的 `LONG_SCHEMAS` / `STATIC_SCHEMAS` 登记列与类型 |
| 新手工源 | `intake/parsers/` 加解析函数 → `registry.py` 注册一行 |
| 新元信息字段 | `schema.py` 的 `METADATA_DDL` + `update._ensure_meta` + `_apply_note` |
| 新市场日历 | `check._MARKET_ALIAS` |

**都不要改 `fin/read.py` 的公开函数签名**——读接口是稳定契约，改了所有消费方都要跟着动。

---

## 7. 已知的技术债

| 项 | 影响 | 建议 |
|---|---|---|
| `sources.fred` 里 `os.environ.setdefault` 设代理 | 全局副作用，可能污染其他请求 | 改成显式传 `proxies=` |
| `cfc` 查询 `$limit=50000` 硬编码 | CFTC 历史若继续增长会被截断 | 加分页或检测截断 |
| `check` 只检查「有没有这个日期」 | 不检查数值突变（如 10 倍跳变） | 加一层"与前值比值"的合理性检查 |
| **月频表同月两行**：`m1_yoy`/`m2_yoy`/`m1_level`/`m2_level`（397 行 / 225 月）、`total_shares`/`total_mktcap`（398/226）、`equity_fin_monthly`（162/81） | 月初 + 月末两行并存（值多数相同）→ `resample`/分位/差分**重复计数** | **已能报出来**（`check` 的「同月多行 N 期」）；数据迁移单独做，本轮不动库 |
| `check` 只查观测区间内的**空洞**，不查**尾部停更** | `vix` 停在 11-06、`csi_all` 停在 11-10 都不告警 | 等 FRED 那批表的 `market` 从 NYSE 改成自然日/联邦工作日后再加 |
| `check` 的 `long` 表只看日期集合，不看**标的覆盖度** | `financials`/`dividend`/`shares_history` 只有 5 只也全部通过 | 加一层"标的数 vs `company` 行数"的覆盖检查 |
| `static` 表没有日期轴 | `check` 实际不检查它（只算行数） | 由 `company` 之外的来源保证新鲜度，或给 static 表加 `updated_at` |

---

## 8. 四种表结构

| kind | 形状 | 主键 | 例 |
|---|---|---|---|
| `single` | `date, value` | date | `vix`、`csi300` |
| `multi` | `date, 列…` | date | `csi_all`（OHLCV + 成交额/换手） |
| `long` | `code, date, 字段…` | (code, date) | `stock_daily`、`dividend`、`financials` |
| `static` | `code, 字段…` | code | `company` |

- **列名与类型**在 `read.py` 的 `LONG_SCHEMAS` / `STATIC_SCHEMAS` 里，是唯一出处
  （`metadata.columns` 只有列名、没有类型，建表需要类型）
- **写库**在 `update.py`：`write_long` / `write_static`，按 `Source.shape` 分派
- `multi` 用于"列是写死的"场景；`long` 用于"标的是数据"（五千只股票没法一列一只）

## 9. 什么时候跑（低频跳过）

| freq | 规则 |
|---|---|
| `D` | 每次都跑 |
| `W/M/Q/Y` | 按 `lag_days` 算"应到哪一期"，`metadata.end_date` 已覆盖就跳过 |
| `SPARSE` | 事件表，按 `sync_log.last_at` + 各自间隔（分红 7 天、股本 90 天） |

`--force` 可跳过判断强制更新。

---

## 10. 外部数据源（ashareapi）

key 只在 `.env` / 环境变量 `ASHARE_API_KEY`，代码不存。

| 端点 | 用途 | 频率 |
|---|---|---|
| `/v1/stocks` | 全市场清单 → `company` | 每月 |
| `/v1/finance` | 三大报表 → `financials` | 每季 |
| `/v1/dividend` | 分红送转 → `dividend`（**接口是每 10 股，入库除以 10**） | 事件，7 天节流 |
| `/v1/snapshot` | 当日股本 → `shares_history`（`kind=base`） | 季度 |

**没有接的两个**：`/v1/changedist`（涨跌家数）与 `/v1/market-kline`（全市场单日 K 线，专业版）。
涨跌家数不要单独采：它是 `stock_daily` 的按日聚合，属于派生（见 §11）。

**额度**：标准版买断 10 万次。`finance`/`dividend`/`snapshot` 都是**单只一次调用**，
所以《每天全量 5000 只》是不可行的（10 天烧完）。按上表的频率，一年约 5 万次。

**专业版（¥99，50 万次）解锁的是批量端点**：`market-kline` 一次拿全市场单日 K 线、
`finance-batch` 一次 10 只——这才是日更的正确姿势。

---

## 11. 派生数据（待实现）

这些**不建表**，由 `read` 现算（将来可能缓存）：

| 派生量 | 公式 | 依赖 |
|---|---|---|
| 市场宽度 ✅ | 按日聚合 `stock_daily`：涨/跌/平、涨跌停（**已实现**，见 `fin/derive.py`） | `stock_daily` + `company`(ST) |
| 总市值 | `close × total_shares`（股本按日期 ffill） | `stock_daily` + `shares_history` |
| PE_ttm | `总市值 / 净利润TTM` | + `financials` |
| PB | `总市值 / 净资产` | + `financials` |
| ETF 资金流 | `etf_shares` 差分 | `etf_shares` |

**市场宽度**已落地（2026-11-14）：

```python
from datahub.fin.derive import breadth
df = breadth("2026-01-01", "2026-11-10")   # index=date，列 adv/dec/flat/limit_up/limit_down/total
```

口径与已知偏差写在 `derive.py` 的模块 docstring 里（涨跌基准、停牌、板块阈值、ST、未复权除权坑）。
它**不建表、不注册成 Source**；等与旧表 `breadth_*` 比对确认后，第三轮再删旧表并切换 `state_layer`。

**为什么现算**：估值入库约 5000×2500×4 个浮点 ≈ 3 GB，且都是可推导量；
一旦入库就要操心"股本变了要重算历史"。

### 11.1 怎么标记"原始 / 派生"

`metadata` 有两个列区分（2026-11-14 加）：

| 列 | 值 | 含义 |
|---|---|---|
| `source` | `raw`（默认）/ `derived` | 这张表是原始数据，还是由别的表算出来的 |
| `depends` | 表名，逗号分隔 | 派生表依赖哪几张原生表；`raw` 留空 |

本轮只**把列准备好**，不写派生数据、不建派生表。将来派生函数落地时，
用 `source='derived' + depends=...` 登记，`check` 才知道"这张表缺了不用去抓"。

### 11.2 名字撞车：`total_shares` 有三种含义（刻意不改名）

| 出现的地方 | 指的是 |
|---|---|
| `total_shares`（表） | **交易所月度合计**：沪+深全部上市公司的总股本（亿股），月频 |
| `company.total_shares`（字段） | **单只个股**的总股本（股），静态快照 |
| §11 派生市值公式里的 `total_shares` | 同上，取自 **`shares_history` 按日期 ffill** 的个股股本 |

`shares_history` 是按事件累积的权威口径，`company.total_shares` 只是当前快照
（而且这一列现在还是空的，要 `/v1/snapshot` 补）。**不改名**：改名要动
`read()` 的消费方，收益只是少一层注释——这里写清楚就够了。
