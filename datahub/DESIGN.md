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
│   ├── update.py          写库（四种 kind 各一个 writer）+ 按频率跳过 + 元数据 + `backfill`
│   ├── sources/           取数（2026-11-14 从单文件拆成包，按来源分）
│   │   ├── __init__.py    SOURCES 注册表（汇总各组）；只导出 SOURCES / Source
│   │   ├── base.py        Source 契约 + 共享工具（_clip/_num/_with_prefix/_read_cached/_existing_keys）
│   │   ├── ashare.py      ashareapi：HTTP 客户端 + 限速器 + 4 个取数函数
│   │   ├── akshare.py     akshare：指数 / 期货 / 宏观 / ETF 份额 / 年度分红
│   │   └── other.py       FRED / CFTC / 黄金与地缘 JSON / 手工 CSV
│   ├── check.py           交易日历 + 完整性检查
│   ├── derive.py          派生量（市场宽度 / 年度股息率），**不落表**、不注册成源
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
| `port.py` | 跨模块唯一面：读、装配、受控动作（update / intake / backfill）、派生量（`breadth` / `dividend_yield`）、`catalog` | service / 各模块 cli / web |
| `core/conn.py` | 连接超时、事务边界、DDL 执行 | fin / ops |
| `core/schema.py` | 所有 DDL 的唯一出处 + 老库补列（`migrate_metadata` / `migrate_data_tables`） | update / intake / ops |
| `fin/read.py` | 只读 API + 表结构定义（列名与类型） | 服务层 / 脚本 / port |
| `fin/sources/base.py` | `Source` 契约 + 跨源共享的清洗工具 | 各取数模块 |
| `fin/sources/ashare.py` | ashareapi 的 HTTP 客户端与 4 个取数函数（限速器必须同模块） | `sources/__init__` |
| `fin/sources/akshare.py` | akshare 的全部取数（含 `INDEX_ETFS` / `ALL_ETFS`） | `sources/__init__` |
| `fin/sources/other.py` | FRED / CFTC / 黄金与地缘 JSON / 手工 CSV | `sources/__init__` |
| `fin/sources/__init__.py` | `SOURCES` 注册表（唯一事实来源） | update / port |
| `fin/update.py` | "怎么写"（四种 kind + 元数据 + 该不该跑 + 逐批落库的 `backfill`） | 人 / 服务层 / port |
| `fin/check.py` | 缺口判断（日历驱动；低频表按周期比，报「同月多行」） | 状态机 / 人 / port |
| `fin/derive.py` | 派生量现算（市场宽度 / 年度股息率），不建表、不注册成源 | port / 脚本 |
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
- **`code` 一律带市场前缀**（2026-11-14 起）：`sh600519` / `sz000001` / `bj920045`。
  `stock_daily`(1040 万行) / `adj_factor` / `etf_shares` 已从 Fuyao 的裸 6 位迁过来。
  新增数据一律过 `sources._with_prefix()` 归一，**不要再写裸码**——混格式会让
  `isin`/合并/派生全部对不上（`derive.py` 里原本有个 `_bare_code`，已删除）。
  迁移方式：`VACUUM INTO` 备份 → 建 `_new` 表 → `INSERT … SELECT` 加前缀 →
  校验行数 → `DROP` → `RENAME` → 重建 `idx_<表>_date`（不用 `UPDATE`，WAL 会炸）。

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

| 端点 | 用途 | 频率 | 单次上限 / 独立日额度 |
|---|---|---|---|
| `/v1/stocks` | 全市场清单 → `company` | 每月 | 1 次拿全 5578 只 |
| `/v1/snapshot-batch` | 当日股本 → `shares_history`（`kind=base`） | 季度 | 200 只/次 · 200 次/日 |
| `/v1/finance-batch` | 三大报表 → `financials` | 每季 | **10 只/次**（且 `只数×periods≤40` → 最多 4 期）· **100 次/日** |
| `/v1/dividend` | 分红送转 → **已停用**（2026-11-14 并入 `adj_factor`） | — | 无批量端点，只能逐只 |

**2026-11-14 额度实测**（专业版）：`total_quota=500000`、`per_min=300`、
**每个批量端点有独立日额度**（`heavy_quotas`），**没有全局日额度**（`daily_quota=null`）。
计费：批量 **1 次/只**（与单只同价）、`market-kline` **500 次/次**、被 422 拒 **0 扣费**、
非交易日打 `market-kline` 也是 **0 扣费**（返回 200 + `ok:false` + 空）。

**⚠️ 上游会降级（2026-11-14 踩到）**：持续调用同一个数据源，会返回
`reason=temporarily_unavailable` + `retry_after_s=60`。两种表现都要防：
- `ok:false`——看起来像"这只没数据"，实际是被限流；
- `ok:true` 但**少表**（实测某只只回 `balance`）——这是静默缺数据。

所以取数函数**逐只校验 `income`+`balance` 都在**，缺的记失败、不写半条。
实测单只 `/v1/finance` 约 1.8s/次且速率一高就降级（全市场要 1~3 小时），
**batch 才是可行路径**（1 请求 10 只）。

**`dividend` 已并入 `adj_factor`（2026-11-14）**：以 Fuyao 的合计值为准（1991 起、
覆盖 5437 只、每股现金是"年度+特别"合计），`dividend` 只贡献 `report_end` / `plan`
两列。27 条里 25 条数值一致；**2 条冲突（都 sz300750）**：

| code | date | adj_factor（采用） | dividend 表（弃用） | 处置 |
|---|---|---|---|---|
| sz300750 | 2024-04-30 | 5.028（10派50.28） | 2.011（10派20.11） | 写 `report_end`，`plan` 留空 |
| sz300750 | 2026-04-22 | 6.957（10派69.57） | 2.178（10派21.78） | 同上 |

`plan` 留空是刻意的：冲突时把"10派21.78"写进一行为 6.957 的记录里，会造出一行
自相矛盾的数据。两笔差额（4.779 / 3.017）正好是另一笔分红的量级，怀疑 ashareapi
对"同一除权日年度+特别两笔"只返回其一，**待全市场分红回填时再核**。
`dividend` 表**先留、标"待删"**（metadata.note），等股息率派生与拆包都验证过再删。

**续跑**：`shares`/`financials` 都只抓"库里还没有的 code"，`financials` 的
`max_calls` 默认等于日额度（100 次 = 1000 只）→ 全市场 5578 只需 6 天，
每天跑一次 `update` 自动接着补，不需要跨天游标。
`shares_history` 一次 28 次调用覆盖全市场（实测 37 秒）。

**另外两个可用但没接的端点**：`/v1/market-kline`（全市场单日 K 线，5571 行含北交所，
1 次=500 额度，200 次/日）与 `/v1/changedist`（当期涨跌家数，**只能对账不能回补历史**，
用来校验 `derive.breadth()` 的口径漂移）。

---

## 11. 派生数据（待实现）

这些**不建表**，由 `read` 现算（将来可能缓存）：

| 派生量 | 公式 | 依赖 |
|---|---|---|
| 市场宽度 ✅ | 按日聚合 `stock_daily`：涨/跌/平、涨跌停（**已实现**，见 `fin/derive.py`） | `stock_daily` + `company`(ST) |
| 年度股息率 ✅ | 每股现金分红（按**除权日**归年、送转折算）/ 年末收盘价；市值加权 + 分红股中位数（**已实现** `derive.dividend_yield`） | `adj_factor` + `stock_daily` + `shares_history` |
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

**年度股息率**已落地（2026-11-14），签名固定（`causual` 依赖）：

```python
from datahub.fin.derive import dividend_yield
dividend_yield()        # index=year(int)，列 yield_mcap_weighted / yield_median / n_stocks
dividend_yield(2025)    # 单年（同样要全表扫一遍，3.7s；循环里按年调是浪费）
```

三个必须知道的口径：
- **单位是百分数**（2.1258 = 2.13%），不是小数。
- **按除权日归年**，不是按报告期——所以和 `dividend_annual`（报告期口径）**不可直接比**，
  年报分红多数在次年除权，两套口径天然错开一年。
- **市值加权的分母是全市场**（不分红股分子记 0、分母照算）；**中位数只统计分红股**
  （掺入 0 会把中位数拉到 0）。
- 权重用**当前股本**近似（历史股本不可得，见 §11.2），越往前越不准；
  `stock_daily` 不含退市股 → 存量幸存者偏差；2016 年不完整；当年是 YTD。

实测 `n_stocks / 当年有年末价的股票数` ≈ 67~80%（2025：3760 / 5444）。
全历史一次 3.7 秒，其中 `stock_daily` 的"每年末收盘价"占 3.4 秒
（`GROUP BY code, substr(date,1,4)` 沿 PK 流式分组；加 `date BETWEEN` 反而更慢）。

**消费方怎么拿到派生量**（2026-11-14 起走 port，不许直接 import `fin.derive`）：

| `DataPort` 方法 | 返回 | 备注 |
|---|---|---|
| `breadth(start=None, end=None)` | DataFrame | 市场宽度，现算 |
| `dividend_yield(year=None)` | DataFrame | 年度股息率（%）；3.7s，**一次拿全表，别按年循环** |
| `catalog()` | DataFrame | 所有表 + 这 2 个派生量的清单（43 行 = 41 表 + 2） |

`catalog` 的**定位**：描述性、**不稳定**、仅供展示与调度决策，**不要当契约**——
字段名和行数都可能变。要取数据请用 `read` / `read_many` / `series_map` 或上面两个派生方法。

### 10.1 窗口与代码过滤（2026-11-16 下推）

`series_map(names, *, start=None, end=None, codes=None)`——三个参数**都落到 SQL**：

| 参数 | 落到哪 | 适用 kind |
|---|---|---|
| `start` / `end` | `WHERE date >= ? AND date <= ?`（闭区间） | 全部（static 无日期轴，跳过） |
| `codes` | `WHERE code IN (...)` | **只有 long**；打到 single/multi **告警并忽略** |

为什么不静默忽略：调用方以为筛过了、实际拿到全量，是最难查的一类问题
（`stock_daily` 1040 万行整表读进内存就是这样发生的）。

**long 表的 key 随窗口收缩**：窗口内没有数据的代码不出现（`etf_shares` 2025 上半年
是 22/24）。这不是"被筛掉"，别拿 key 数当"有多少只"。

**`read()` 也接受 `codes`**：long 透传给 `read_long`、static 透传给 `read_static`，
其它 kind 告警忽略——避免"`series_map` 绕过 `read` 直接调 `read_long`"造成两条路漂移。

⚠️ **窗口起点还没有 warmup**：`get_data(start=...)` 把起点直接当取数起点，低频列在
窗口头部没有历史可 ffill。实测 2025-01-01 起取：`STATE_ASHARE` 逐位相同（0 行差异），
`CAUSUAL_GOLD` 头部 **23 行**差异（`usdcny/vix/usd_index/fed_rate`）。
warmup（起点前多取 N 天、算完再切）是下一步。

### 10.2 派生量注册表 + warmup（2026-11-16）

派生量（`breadth` / `dividend_yield`）注册在 `fin/derive.DERIVED`：

```python
DERIVED["breadth"] = Derived(func=breadth, depends=("stock_daily",), period="D",
                             columns=("adv",…,"total"), warmup_days=400, doc=…)
```

- **不进 `metadata`**：派生量不落表、没有 `sync_log`，塞进 metadata 会让
  `check` / `/data` 页分不清"缺表"和"该现算"。
- **`series_map` 认名字**：`series_map(["breadth"], start, end)` 返回
  `{breadth:adv: Series, …}`，**与普通表形态完全同构**，调用方分不出哪个是算的。
- **不变量**：`func(start, end)` 恒等于 `func()` 再 `.loc[start:end]`。
  为此 `breadth` 的回看从 40 天提到 **400 天**——40 天时 `breadth(2025)` 与
  全量切片有 **0.82%** 的格子不同（停牌超过 40 天的股票在窗口起点算不出前收）。

**`get_data` 的 warmup**：每列在 adapter 里声明往前多取多少天
（`_WARMUP`，0/10/45/130），取数起点 = `start − max(warmup)`，build 完再
`df.loc[start:end]`。**只有传了 `start` 才生效**，不传就是全量（老行为，逐位不变）。

| 列 | warmup | 为什么 |
|---|---|---|
| 价格 / 指数 | 0 | 本身就是日频，窗口内每天都有 |
| 拆借 / 两融 / VIX / 汇率 | 10 天 | 跨周末与长假 |
| 月频（PMI/CPI/M1/M2/固投…） | 45 天 | 1 个月 + 公布滞后余量 |
| 月频存量（ETF 持仓） | 45 天 | 同上（含 40 天公布滞后） |
| 季频（`cb_holdings`） | 130 天 | 1 个季度 + 45 天公布滞后 |

验收：2025-01-01 起取，四个面板 **0 行差异**（原来是 `CAUSUAL_GOLD` 23 行）。

### 10.3 `etf_group_shares` 也沉进数据层（2026-11-16）

ETF 分组（哪几只 ETF 算"上证50/沪深300/中证1000 宽基"）与组内求和，2026-11-16
从 `shared/adapter` 搬进 `fin/derive.py`：

```python
DERIVED["etf_group_shares"] = Derived(func=etf_group_shares, depends=("etf_shares",),
                                      period="D", columns=("sse50","csi300","csi1000"),
                                      warmup_obs=2, …)
```

- adapter 里**不再有** ETF 代码 / `_ETF_GROUPS` / `bare_code` / 组内求和；
  它只知道"列叫 `etf_sse50`"和"派生量叫 `etf_group_shares`"（有测试钉住）
- `series_map(["etf_group_shares"])` → `{"etf_group_shares:sse50": Series, …}`，
  与表同构；窗口不变量同样成立（`warmup_obs=2` 保证窗口内第一条也有 pct_change 基准）
- ⚠️ **已知错误（欠账，不是"设计如此"）**：`etf_group_shares` 里某组当天**没有观测**
  时会被写成 `0.0`（pandas 对全 NaN 求和的默认行为，旧 `sum(axis=1)` 的遗产），
  2016 年共 **162 格**。这不是"少一个值"，而是**伪造了一次 −100% 下跌再 +∞ 回升**：
  `t0=100`（有观测）→ `t1=0`（无观测，被写成 0）→ `t2=120`（有观测）。
  这批假尖峰会污染 `ETF_DISPERSION` / `SIZE_FLOW` / `NATIONAL_ETF` 的 2016 段，
  **而 2016 正是 HMM 的训练区间**（抬高那段的波动估计）。

  **修复方式**：`reindex` 到观测日并集后**保持 NaN**，不要 `fillna(0)`。
  ⚠️ 修它会改变上述三个派生列的 2016 段 → 特征变 → 标签变 → **边快照失效**，
  所以**必须同轮重拟合 + 重估 edges，不能单独改**（2026-11-16 决定：先记账，等下一次
  计划内的重拟合一起做）。

### 10.4 通用业务派生量也进了注册表（2026-11-16）

原来放在 `shared/utils/features.py`（**该文件已于 2026-11-16 删除**）、吃**面板列**的 6 个通用特征，改成在 derive 里
**自己读库**并注册（`series_map` 按名字给，`get_data` 直接出现在面板里）：

| 派生量 | 列 | 依赖 | warmup | 出现处 |
|---|---|---|---|---|
| `dividend_yield` | `yield_mcap_weighted` | `adj_factor`/`stock_daily`/`shares_history` | `warmup_years=0` | **只给 causual.ashare**（`daily`） |
| `etf_dispersion` | `dispersion` | `etf_shares` | `warmup_obs=2` | A 股面板（`daily`，ffill） |
| `size_flow` | `flow` | `etf_shares` | `warmup_obs=2` | 同上 |
| `national_etf` | `net` | `etf_shares` | `warmup_obs=2` | 同上 |
| `national_etf_event` | `event` | `etf_shares` | `warmup_days=120` | 同上（日频 0/1，头部为 0） |
| `cb_gold_buy` | `flow` | `cb_holdings` | `warmup_obs=3` | 黄金面板（`raw`，落在可见日） |
| `etf_flow` | `flow` | `gold_etf_holdings` | `warmup_obs=3` | 同上 |

**warmup 各填各的**（不再一个全局常量）：pct_change 只要 1 个历史观测，
60 日滚动分位要 60 个，差分还要"上上个"作基准（`_gold_flow` 因此是 **3** 不是 2——
只取 2 个的话窗口内第一笔会缺，实测 2025-01-09 差一格）。

**`dividend_yield` 的年度对齐也在 adapter**：年 Y 的值对齐到 **Y+1-01-01**（年 Y 的除息日
全落在 Y 内，数据到 Y+1 才完整），再 ffill 到日频。这条换算原来散在
`causual/cli.derived_series`（改 index）+ `pipeline._dividend_yield`（`to_daily`）两处，
现在归适配层——它和 `lag_days` 是同一类"数据期→可见日"的换算。
实测与旧两步路径**逐位相同**（2394 个非空、末值 2.1258）。
它**只挂 causual.ashare**：算一次 3.7 秒，状态层不用——`_PANEL_DERIVED` 按 **Panel**
而不是 builder 挂载，就是为了不让共享 `ashare_base` 的 state 也付这个代价。

**两种落位口径**：`etf_*` 返回**观测日**（`daily` 策略由 adapter ffill），
`national_etf_event` 返回**日频**（0/1 标记，"没有观测"应当是 0 不是 NaN），
黄金两条返回**稀疏**（`raw` 策略，落在可见日之后的第一个交易日）。

验收：9 个派生量窗口不变量全过；四个面板**原有列逐位不变**（35/17/14/7 列），
新增 4/2/4/0 列；6 个新列与旧的 `features.*` 算法**逐位相同**——
唯一例外是 `etf_flow` 多了 **1 个值**（2008-01-09）：derive 直接读库，有基期观测，
而旧的"吃面板列"版本因为面板起点之前没有那一行，算不出第一笔。**新的是更完整的**。

**为什么现算**：估值入库约 5000×2500×4 个浮点 ≈ 3 GB，且都是可推导量；
一旦入库就要操心"股本变了要重算历史"。

### 10.5 派生量总表（9 条，warmup 三种单位）

| 派生量 | 列 | 依赖 | warmup | 落位 |
|---|---|---|---|---|
| `breadth` | `adv/dec/…` | `stock_daily` | **`warmup_days=400`** | 交易日 |
| `dividend_yield` | `yield_mcap_weighted` 等 3 列 | `adj_factor`/`stock_daily`/`shares_history` | **`warmup_years=0`** | 年 Y → Y+1-01-01 → ffill |
| `etf_group_shares` | `sse50/csi300/csi1000` | `etf_shares` | **`warmup_obs=2`** | 观测日（不 ffill） |
| `etf_dispersion` | `dispersion` | `etf_shares` | `warmup_obs=2` | 观测日 → `daily` |
| `size_flow` | `flow` | `etf_shares` | `warmup_obs=2` | 观测日 → `daily` |
| `national_etf` | `net` | `etf_shares` | `warmup_obs=2` | 观测日 → `daily` |
| `national_etf_event` | `event` | `etf_shares` | **`warmup_days=120`** | 日频 0/1（头部为 0） |
| `cb_gold_buy` | `flow` | `cb_holdings` | **`warmup_obs=3`** | 稀疏（可见日） |
| `etf_flow` | `flow` | `gold_etf_holdings` | `warmup_obs=3` | 稀疏（可见日） |

**warmup 三种单位**，按"这个量要多少历史"选，不搞一个全局常量：

- `warmup_days` —— 日期单位（`breadth` 400 天：停牌超 40 天的股票在窗口起点算不出前收；
  `national_etf_event` 120 天兜住 60 个观测的滚动分位 + 长假）
- `warmup_obs` —— **观测**单位（周频/月频表"往前 3 天"可能一个观测都没有）
- `warmup_years` —— 年度口径（`dividend_yield` 是 0：每年独立，没有跨年滚动）

**`depends_note` 是"什么条件下这个数会变味"**，不是装饰：写的是"换源 / 改口径 /
分组变了 / 用当前股本近似"这类**会让历史不可比**的条件。改派生量前先读它。

**`period="Y"` 的对齐规则**：年 Y 的值 → **Y+1-01-01** → ffill 到日频。
理由：年 Y 的除息日全落在 Y 内，数据到 Y+1 才完整；这是"数据期→可见日"的换算，
和 `lag_days` 同类，所以由**适配层**做（不在消费方各写一遍）。

### 11.1 "原始 / 派生"不再由 `metadata` 区分（2026-11-16 改）

2026-11-14 曾在 `metadata` 加了 `source` / `depends` 两列，想用它们标记"这张表是原始
数据还是算出来的"。**2026-11-16 删掉了**，理由：

- 派生量**不落表**（现算、无 `sync_log`、不在 `/data` 页），塞进 `metadata` 会让
  `check` / `/data` 分不清"缺表"和"该现算"；
- 唯一真正的消费者是 `check`（"派生表缺了不用抓"），而派生量根本不在 `tables()` 里，
  这条判断从来不会触发。

现在**派生量的登记只有一个地方**：`fin/derive.DERIVED`（`depends` + `depends_note`），
消费方通过 `series_map` 按名字取，形态与表同构（见 §10.2）。

迁移：老库由 `core/schema.METADATA_DROPS` 幂等删列（SQLite 3.35+ 的
`ALTER TABLE … DROP COLUMN`）；新库的 `DATA_DDL` 直接不带这两列。

### 11.2 名字撞车：`total_shares` 有三种含义（刻意不改名）

| 出现的地方 | 指的是 |
|---|---|
| `total_shares`（表） | **交易所月度合计**：沪+深全部上市公司的总股本（亿股），月频 |
| `company.total_shares`（字段） | **单只个股**的总股本（股），静态快照 |
| §11 派生市值公式里的 `total_shares` | 同上，取自 **`shares_history` 按日期 ffill** 的个股股本 |

`shares_history` 是按事件累积的权威口径，`company.total_shares` 只是当前快照
（而且这一列现在还是空的，要 `/v1/snapshot` 补）。**不改名**：改名要动
`read()` 的消费方，收益只是少一层注释——这里写清楚就够了。

---

## 12. 已删除的表（2026-11-14）

| 表 | 删除前 | 为什么删 | 取代者 |
|---|---|---|---|
| `breadth_adv` / `breadth_dec` / `breadth_limitup` | 各 1 行（2026-11-10） | 只能取**当天快照**、不回溯；口径也错（统一 `pct>=9.8`：把创业板 10% 的大涨当涨停、漏掉 ST 的 5% 涨停）。与交易所口径对账：旧表 42/58 vs 交易所 38/39 | `derive.breadth()` |
| `dividend_annual` | 5 行（2021–2025） | 按**报告期**汇总，且**近年数据本身不完整**：2021→2025 单调下降（1.74 → 1.59 万亿），与 A 股分红逐年增长的事实矛盾 | `derive.dividend_yield()` |
| `dividend` | 27 行（5 只） | 已并入 `adj_factor`（补 `report_end` / `plan` 两列；数值以 Fuyao 合计为准，2/27 冲突） | `adj_factor` |

**留档（半年后唯一能知道"曾经有这张表、为什么没了"的东西）**

- 删除前全量快照：`fin/intake/clean/dropped_20261114/*.csv`（含 `_README.md` 说明）
- 删除前整库备份：`datahub/datahub_backup_20261114_predrop.sqlite`（1.31 GB，已 gitignore）

**删除要四处一起清，少一处就会复活**：① 表本身 `DROP` ② `metadata` 行
③ `SOURCES` 注册（连源函数一起，含 `FREQ_HINT`）④ 任何按表名硬编码的地方。
本次除了上面四条，还额外清了三处 grep 才发现的残留：

- `LONG_SCHEMAS["dividend"]`（留着它 `write_long` 会把表重新建出来）
- `web/routes.py` 手工白名单里的 `dividend_annual`（否则 `/data` 页会列一张不存在的表）
- `REFRESH_DAYS["dividend"]`（源已删，节流配置成了死配置）

结果：表数 **46 → 41**，`catalog()` = **43 行**（41 表 + 2 派生）；
`derive.breadth()` / `derive.dividend_yield()` 不读这些表，删后照常工作。

### 12.1 不要再回填 `dividend`（哪怕"只差全市场"）

`/v1/dividend` 是**单只一次**的端点（无批量，全市场要 5578 次），看着像"补一下就行"。
**别补**——它有两处硬伤，2026-11-14 实测：

1. **同一除权日多笔派息只返回其中一笔**：`sz300750` 的年度 + 特别分红只回了一笔。
2. **再抓值会变**：同一只、同一除权日，库里的 `2.178` 后来抓到 `47.79`，
   而 Fuyao 的合计是 `6.957`。**同一个接口两次结果不同**，不存在"补一次就稳定"。

分红权威是 Fuyao 的 `adj_factor`（**合计值**、1991 年起、覆盖全市场），
ashareapi 那份只贡献 `report_end` / `plan` 两个字段，且已合并进 `adj_factor`。
⇒ **再补 `dividend` 等于拿一份会漂的数据覆盖权威数据**，永久关闭这条路。
全市场分红只能从 Fuyao / 交易所公告另找来源，不能用 ashareapi。

## 13. 已知限制：`--backfill` 的主块必须在文件末尾

`python -m datahub.fin.update` 会**在 import 过程中**执行 `if __name__ == "__main__":`
块。它若排在 `write_long` / `write_static` 定义之前，`backfill` 的 `flush` 就会
`NameError`——而 `--backfill` 是"先打 10 次接口再落库"，于是**接口额度白烧、
一行都没写**（2026-11-15 踩过：100 次调用全白跑）。

规则：**命令行主块永远放文件最后**；新增模块级函数时不要往它后面追加。
`tests/datahub/test_datahub_update.py` 用一条结构性断言钉住这一点。

## 14. 额度与降级（2026-11-15 实测）

**`finance-batch` 的真实吞吐远低于额度名义值**：

| 指标 | 数值 |
|---|---|
| 日额度 | 100 次（= 1000 只的名义值） |
| 当天实际 | 100 次调用 → **只写成 204 只**（16 → 220） |
| 批次失败率 | **约 76%**（`temporarily_unavailable`） |

⇒ 别人看到"5562 只缺口要 27 天"时**不要以为是代码慢**，是上游在降级。

**降级是"累计型"，不是"速率型"**（同一天对单只 `/v1/finance` 做的三组实验，每组 10 只）：

| 节奏 | 成功 | 现象 |
|---|---|---|
| `gap=0.3s` | 10/10 | 1.66 s/只（服务端 ~1.0 s，客户端延迟占大头） |
| `gap=0.8s` | 6/10 | 第 7 只开始降级——**不是慢就能撑住**，是前一组已经把额度打掉 |
| `gap=2.0s` | 0/10 | 从头就失败，服务端 8 ms 直接返回 `temporarily_unavailable` |

连打三轮（每轮打到失败 → 等 60 s）：

```
第 1 轮 29 只 → 第 2 轮 18 只 → 第 3 轮 15 只     （递减：有累积惩罚）
```

- **恢复很快**：停手后再打，秒级~分钟级就恢复（实测 1 秒）；但**连续打会一轮比一轮少**。
- **单只路径没有 heavy 日额度**（只有 `per_min=300`），所以 5562 只按 10~20 只/分钟
  ≈ **5~9 小时**，可以过夜跑；比 batch 的 27 天现实得多。

  **2026-11-15 实跑 100 只后的修正**：实际 **5.9 只/分**（1020 秒 / 100 只，100% 成功、
  0 失败），推算全市场 5358 只 ≈ **15 小时**。慢的原因不是请求慢（1.3 s/只），
  而是约 30% 的只会撞上降级、各等 20 秒。**"5~9 小时"是乐观估算，15 小时才是
  按当前退避参数的实测推算**；调小第一次退避（20s → 5~10s）可以压到 9~11 小时。
- 降级时的表现是 `ok:false` + `retry_after_s=60`，**不是** HTTP 错误——所以只能靠
  `reason` 判断。

**代码里的对策**（`backfill` / `ashare_financials`）：

1. **额度类失败早停**：`heavy_quotas["finance-batch"].left == 0`、HTTP 402、
   或正文出现 `quota` / `额度` → 抛 `QuotaExhausted`，`backfill` 捕获后打印
   「今日额度用完，明天续跑」并**保留已落库的行**。绝不重试（确定性失败）。
2. **降级类只重试 1 次**（原来 3 次 × 60 s ≈ 3 分钟空转/批）。
3. **`finally` 里无条件刷 `metadata` + `sync_log`**：部分落库后被打断也要让
   `/data` 页的行数跟上（否则显示 84、实际 900）。

## 15. 日更：`stock_daily` 走 `market-kline`（只写 1 天）

2026-11-16 起 `stock_daily` 多了一个**日更源** `ashare_daily_delta`
（`SOURCES` 末尾，`FREQ_HINT = ("D", "CN", 0)`）：每次只取**当天**全市场 OHLC，
1 次调用 → 5571 只、3.3 秒、**500 credits**。

**为什么只写一天**（实测）：补 2500 个交易日（10 年）= **125 万 credits**，
是池子（489k）的 2.5 倍，补不起；一天 500，一年 12.5 万 → 池子够 3.8 年。
**历史回补继续用同花顺 Fuyao 的 parquet**（它还自带复权事件，比这个端点值钱）。

**三道闸**（少一道就会在周末/停更时白烧 500 credits）：

1. **该不该跑**：交易日历（akshare，免费、不占额度）说今天不是交易日、
   或还没到 15:30 → 跳过，**0 次调用**
2. **缺口多大**：与库里最大日期比，**落后 > 1 个交易日 → 警告并跳过**，
   **绝不自动补**（那是人工导 Fuyao 的事）
3. **有没有新数据**：落后 0 天 → 跳过

**单位**（实测，与 wiki 说法不同）：`/v1/market-kline` 的 `volume` **已经是股**
（600519 回 4,177,980 vs Fuyao 4,177,984，差 4 股是两源取整）；`amount` 是元 →
直接进 `turnover`（库里这一列就是成交额）。wiki 的「volume 一律是手」只适用于 `/v1/kline`。

**硬依赖**：日更只解决**价格**。除权除息仍靠 Fuyao 的 `adj_factor`（人工导入）。
Fuyao 一旦断供，`stock_daily` 会处于"价格已更新、复权事件没跟上"的状态 →
历史收益率**全是错的**。别把日更当成"数据全自动"。

**`check` 的盲区**：`check` 不查"尾部停更"，漏跑只能靠
`tests/shared/test_adapter_contract.py::test_last_row_is_fresh_and_not_future`（阈值 5 天）抓。
2026-11-16 实测 `csi_all / gold_cny / csi300 / csi1000 / shibor3m / margin_balance`
全停在 11-06~11-10 → **日更管线整体没在跑**，不是单表问题。

> ⚠️ 那条测试的阈值按**日历日**算，长周末/长假后会**假红**（比如 5 天阈值遇上三天连休）。
> 这是刻意的：**假红比假绿好**——把阈值改成"按交易日"会让真停更被咽掉
> （3 个交易日 < 5 → 绿），那才是哨兵不睁眼。**看到它红，先查数据管线，别改阈值。**

## 16. multi 表的写路径：只更新本次返回的列（2026-11-16 修）

**事故**：`_write` 的 multi 分支用 `INSERT OR REPLACE`。REPLACE 是"删旧行 + 插新行"，
**本次没返回的列会变成 NULL**。`csi_all` 的源 `index_tx` 只回
`open/high/low/close/volume` 五列，于是任何一次刷新都把 `amount` / `turnover`
从 **3503 清成 0**——而**没有任何测试或 `check` 响过**，是 `state-layer-agent`
偶然看 `/state` 显示「缺」才发现的。

**改法**：

```sql
-- 改前（会清空没返回的列）      -- 改后（只更新返回的列）
INSERT OR REPLACE INTO t (date, c1, c2) …
INSERT INTO t (date, c1, c2) … ON CONFLICT(date) DO UPDATE SET c1=excluded.c1, c2=excluded.c2
```

**四条契约**（`tests/datahub/test_write_upsert.py` 钉住）：

1. **等价**：同一份数据写两次 == 写一次（实测 `csi_all` / `gold_cny` 两张 multi 表逐位相同）
2. **缺列不清空**：先写全列、再只写 5 列 → `amount`/`turnover` 仍 3506 非空、值逐位未变
3. **主键/冲突语义不变**：同一 date 写两次只有一行、值被更新。`DATA_DDL` 里**没有
   TRIGGER / AUTOINCREMENT / WITHOUT ROWID**（2026-11-16 核查），所以"就地更新"与
   "删了重插"没有可观察差异
4. **`single` / `long` 分支不动**：它们本来就是 `INSERT OR REPLACE (date,value)` /
   `ON CONFLICT(code,date) DO UPDATE`，没有这个坑

**列熔断**（`_warn_column_loss`）：每次 multi 写入后比较各列非空数，
若某列从 **> 50%** 掉到 **0** → 打 `[列熔断]` 到 stderr。整列被清空不可能是正常业务，
只可能是源缺列/写路径错——**宁可吵**。这次的事故如果早有这条，会当场报出来。

**环境限制（已过时，2026-11-17 更新）**：2026-11-16 本机 **eastmoney 不通**
（akshare `index_zh_a_hist` 报 `ProxyError` / `RemoteDisconnected`，有代理/无代理都试过），
所以 `csi_all` 缺的 3 天（11-11~11-13）当时用 **ashareapi `/kline`（`sh000985`）** 补的：
`amount` 元 ÷ 1e8 = **亿元**（与腾讯口径一致，已对账）、`turnover` 直接用（%）。

**2026-11-17 已恢复**：`index_zh_a_hist('000985')` 正常返回（11-17 成交额 1.394e12 元、
换手 1.32），eastmoney 也回到 HTTP 200。⇒ **再遇到就当"临时故障"处理：先测连通性，
通了就用 akshare（免费），别急着判定为环境限制、更别为此改口径。**

## 17. 取数入口的唯一性 + 进程内缓存（2026-11-16）

**`get_data` 是唯一取数入口，而且只在装配点调**：`*/cli.py`、`*/web/routes.py`、
`service/**`。核心代码（`port.py` / `pipeline.py` / `features.py` / `analysis/*`）
**只接收传进来的 DataFrame**。

这条**有静态测试钉住**（`tests/cross/test_layering.py`）：用 AST 扫
"核心文件里有没有 `get_data(` 调用"，违反了直接红。为什么必须静态查——
违反方式是静默的（`state_view` 里偷偷取一次数，测试全绿、行为也对，只是分层破了）。
另有一条反向校验：定义处与至少一个装配点必须真的存在，否则检查器会"永远绿"。

**进程内缓存**（`shared/adapter` 的 `_CACHE`）：

```python
get_data(panel, start=None, end=None, fresh=False)   # 命中直接返回副本（<1ms）
clear_cache()                                        # 数据变了就丢
```

- 命中率来自"service 一次运行取 4 次 panel（日频/月频 × 2 资产）"，其中
  `dividend_yield` 一次 4 秒 —— 实测：首次 **4.8s** → 命中 **0.0008s** → `clear_cache()` 后 5.6s
- 失效策略**最朴素**：`DataPort.run_update()` 结束时调一次 `clear_cache()`
  （`finally` 里，异常路径也清），外加 `fresh=True` 强制绕过
- **不做 TTL / mtime 校验**（后话）；返回的是**副本**，调用方改它不会污染缓存
- **边界（知道比不知道好）**：多 worker 时**每个进程各一份**；**别的进程写库不会让它失效**
  （只有本进程的 `run_update` 会清）。看到"库里更新了、服务还读旧值"，第一嫌疑是这里

## 18. 换源到 Fuyao（同花顺，2026-11-17）

四个源迁到 Fuyao：**行情快照 / 10 年日 K dump / 财务指标 / 除权事件**。
客户端在 `datahub/fin/sources/fuyao.py`（**所有 fuyao 调用只走它**）。

| 用途 | 端点 / dump | 成本 |
|---|---|---|
| `stock_daily` 日更 | dump **`daily-k-10d`** | **1.1 MB / 2 秒**，一次覆盖最近 10 个交易日 |
| 历史回补 | dump **`daily-k`** | **182.6 MB / 366 秒**，1,040 万行 / 5,580 只 / 10 年 |
| 财务指标（`finances`） | `financials/indicators` | 一季 5,578 次 ≈ 31 分钟（**默认关**） |
| 除权事件（`adj_factor`） | `corporate-actions/adjustment-factors` | 5,578 次 ≈ 30 分钟（**默认关**） |
| 全市场行情快照 | `prices/snapshot` | ~56 页；**盘中实时，别当日线用** |

**历史回补只用 dump，不要逐只抓**（`daily-k` 一次就是全市场 10 年）。
日更走 `daily-k-10d`：它同时解决了原来"落后 >1 天就只能手工导 parquet"的问题。

### 18.1 三个必须知道的坑（都实测踩过）

1. **`date_ms` 是 UTC 毫秒戳**：`pd.to_datetime(ms, unit="ms")` 会得到**前一天**
   （上海 00:00 = UTC 前一日 16:00）。必须 `tz_convert("Asia/Shanghai")`。
   第一次没转，把 2026-11-13 的数据读成了 11-12。
2. **`turnover` 是成交额（元）**，不是换手率（别处这个名字常指换手率）。
3. **`thscode` 必须带交易所后缀**（`600519.SH`），库内是 `sh600519`。
   转换只许有一份：`fuyao.to_thscode()` / `fuyao.to_raw_code()`。

单位已与库里**逐字段对账**：2026-11-13 平安银行的
open/high/low/close/volume/turnover **6 个字段与 dump 完全相同** ⇒ 可直接落。

### 18.2 额度：不限累计调用，只限流

**不要拿 ashareapi 的"额度/降级"思路套 fuyao**：它不限累计调用次数，
只有限流（HTTP 429 / `code=4001`，按 1/2/4/8 秒退避即可）。
所以"全市场逐只抓"（5,578 次）是可行的常态操作。

⚠️ 一个反例：`code=3002`（"No adjustment events"）是**这票没有分红**，
**不是错误**。2026-11-17 第一次回填时把它当异常抛，几百只"失败"全是这个原因。

### 18.3 `finances` 是 EAV，不走 `read`

`finances`（4,096,812 行 / 5,575 只 / 39 期 2017-1~2026-3）是 EAV 表：
主键 **`(thscode, report, abilities, index_id)`**（注意是 4 列，同一指标名可能属于
两个 ability），`value` 是 **TEXT**（接口返回字符串，原样入库避免浮点往返）。
它没有 `code`/`date` 列 ⇒ **不能 `read` / `read_long`**；`metadata.kind` 自动猜成
`multi` 只是显示，别当真。写入用 `update.write_eav()`。

### 18.4 `adj_factor` 回填踩的坑：**只增不改**

2026-11-17 第一次全市场回填后核对发现：**985 行的 `allot_ratio`/`allot_price` 被清成 0**
——fuyao 的除权接口只给分红与送股，**不含配股**，而我在源函数里把这两列写死 `0.0`，
写路径"只更新返回了的列"就把真实配股覆盖了。

两条规矩（都已落实）：

1. **源函数不返回它不认识的列**。`adjustment_factors()` 现在只输出
   `code/date/dividend/bonus`，配股两列**原值不动**。
2. **历史值优先**：回填只往里**加新行**，与旧值冲突时不覆盖。
   2026-11-17 的实际差异只有 2 行 `dividend`（`sz000812` 1998 年 0.20→0.00、
   `sh603883` 2024 年 0.66→0.50）——**保留旧值**，因为历史收益率是按旧值算的、
   下游快照都建立在它上面。

回填结果：**57,796 → 57,829 行（+33）、5,437 → 5,439 只**，
重叠 57,796 行**四列全部逐位相同**，新增 33 行的除权日全在 **2026-11-20 及以后**
（都是未来事件）⇒ **历史收益率不可能变**。

### 18.5 日更改用 `prices/snapshot` 分页（2026-11-17 用户拍板）

| 路径 | 成本 |
|---|---|
| ~~`daily-k` dump~~ | 182 MB / **366 秒**（仅历史回补） |
| ~~`daily-k-10d` dump~~ | 1.1 MB / 2 秒（也够用，但日更只需当天） |
| **`prices/snapshot` 分页** | **3 页（`limit=2000`）/ ~2 秒** |

日更 = `limit=2000` 翻页，**本页条数 < limit 即到末尾**。`update --only stock_daily --force`
实测 **2.8 秒**（含写库 + 元数据刷新）；重复跑 **2.9 秒**并打印"已存在，跳过"。

五条实现细节：

1. **交易日归属用 `data.timestamp`**（毫秒戳 → Asia/Shanghai），不用本机日期。
   只在"**今天已收盘（≥15:30）且 timestamp 的日期 == 今天**"时写；周末跑 timestamp
   是上个交易日 → 直接跳过（不会重复写）。
2. **单位全市场核对**（不是抽查一只）：

   | 字段 | 5,573 只里的不一致 |
   |---|---|
   | open / high / low / close | **0** |
   | volume | 4 只实质差异（其余 76 只差 1~4 股，源头按手取整） |
   | turnover | 4 只实质差异（其余差 < 1 元，源头取整到元） |

   那 **4 只全是北交所 `bj920*`**（如 `bj920076` volume +300,000 股、turnover +107%），
   原因是 **snapshot 含北交所盘后固定价格交易、dump 不含**。⇒ 这一天的这 4 只
   与历史（dump 口径）不同源，**已知并接受**（盘后交易是真实成交，snapshot 更完整）。
3. **不复权**：snapshot 是原始价，`stock_daily` 也是原始价（复权靠 `adj_factor` 现算）；
   `prev_price` 同样是未复权前收，**不使用**。
4. **分页边界**：空页必须**报错**（上游异常/全停牌不能被当成"到末尾"静默退出）；
   另有 60 页上限防死循环；跨零点导致 `timestamp` 变日期也报错。
5. **dump 保留为历史回补手段**：补 10 年用 `daily-k`，366 秒一次无所谓；
   **日更绝不走 dump**。

⚠️ 一个坑：判断"这天有没有写过"必须用 SQL `MAX(date)`。第一版用了
`read("stock_daily")`（整表 1,040 万行）→ **56.9 秒**，把日更的意义全吃掉了。

## 19. 外部网页源：UA 与非 200 必须响（2026-11-17）

**事故**：`world_tension` 断了 **12 天**（库里停在 2026-11-05），状态机一直报失败。

根因不是网络：**Cloudflare 拦 `python-requests` 的默认 UA**。

```
requests 默认 UA ("python-requests/2.x") → 403 "Attention Required!"
浏览器 UA                                 → 200 + 正常 JSON
```

而代码写的是：

```python
if r.status_code == 200:
    ...
# 否则什么都不做，继续下一段
```

⇒ 每个分段都被静默跳过，最后只抛一句"没有返回任何数据"，**看不出是 403 还是真空**。
（服务器上报的是 `ReadTimeout` 而不是 403——Cloudflare 挑战页挂住时会这样，**同一堵墙两种表现**。）

**这和 `INSERT OR REPLACE` 清空列、`fillna(0)` 伪造 −100% 下跌是同一类：
该响的地方没响。** 三者的共同点都是"失败被降级成看起来正常的结果"。

### 19.1 规矩

1. **外部 GET 一律带浏览器 UA**（`other._BROWSER_UA`，**一个常量，别复制**）
2. **非 200 必须抛**（`other._get()` 统一做：UA + 超时重试一次 + 非 200 抛 `RuntimeError`
   并带上响应前 120 字符）
3. 不允许"只在 200 时做事、其余静默 continue"的写法（有静态测试钉住）

### 19.2 顺手扫出来的同类停更（都已补上）

| 表 | 补之前 | 补之后 |
|---|---|---|
| `world_tension` | 2026-11-05（12 天） | **2026-11-17** |
| `xau_usd` | 2026-11-10（7 天） | **2026-11-17** |
| `gold_cot_net_long` | 2026-11-01（16 天） | **2026-11-15**（周频） |
| `gold_cpi_surprise` | 2026-09-30（48 天） | **2026-10-31**（月频） |

⇒ **Cloudflare 不会只拦一家**；停更的表要按"网页源 vs API 源"分类看，
网页源优先怀疑 UA/反爬，API 源优先怀疑额度/限流。

### 19.3 一个例外：源端修订

回填后与旧数据重叠的 227 行里，**只有 1 行变了**：`2026-11-05` 由 64 → 69。
这是**源端修正了自己的历史值**（当天抓到的可能是临时分），不是我们写坏。
这类差异要单独记：**"只增不改"防的是我们自己覆盖历史，不防源端修订**。

## 20. 非标准形态的表要**登记**，不能靠列名猜（2026-11-17）

**事故**：服务器状态机卡在 `validate`：

```
structural 数据不可用：finances：读取失败 DatabaseError
```

`finances` 是 EAV（`thscode, report, abilities, index_id, value`），但
`_ensure_meta` 的分类是"列 == `["date","value"]` → single，否则 multi"，
于是它被当成 `multi`；`check` 对所有表都调 `fin.read()` → `DatabaseError` →
**validate 整个挂掉 → `generate_cards` 不跑 → 服务器出不了新卡**。

**根因是"我能写进去"和"我能读出来"被混为一谈**：写入路径给了 EAV 一条
`write_eav`，但检查路径假设所有表都能 `read`。

### 规矩

1. **形态登记在 `core/schema.EAV_TABLES`**（`pk` / `entity` / `period` 三样），
   `_ensure_meta` **先查登记表**再兜底猜；老库的错 kind 会被就地纠正（幂等）。
2. **`check` 遇到 `kind="eav"` 不调 `read`**，按真实结构验：
   行数 / `COUNT(DISTINCT entity)` / `COUNT(DISTINCT period)` / 最新报告期 /
   **主键唯一性**（`GROUP BY pk HAVING COUNT(*)>1`）。
   报出来是 `ok（eav，4,096,812 行 / 5575 只 / 39 期，最新 2026-3）`，
   **不再是"读取失败"**。
3. **新鲜度按报告期比**：预期期 = 上一季度（`2026-11-17` → `2026-3`），
   落后就报 `最新报告期 X 落后于预期 Y`。

### 以后加表

形态不是 `single` / `multi` / `long` / `static` 的（宽表转置、多层嵌套、EAV…），
**先在 `EAV_TABLES` 这种登记处声明**，并让 `check` / `read` / 写路径都认它。
**默认路径一定假设它是那四种之一**——猜错的下场是整条流水线被 block。

### 20.1 `kind` 是缓存，登记表才是真相（2026-11-17 二次修复）

第一次修复只改了**写路径**（`_ensure_meta` 会把错 kind 纠正成 `eav`），
但**读路径仍然信任存储值**：

```
本地 metadata:  ('finances', 'eav')     ← 本地跑过 update
服务器 metadata: ('finances', 'multi')  ← 没跑过 update，还是旧值
服务器 check:    finances 读取失败 DatabaseError
```

⇒ **纠正逻辑跑在 `update` 里，`check` 根本不经过 `update`。**

改法：`kind_of()` **登记表第一优先**：

```python
spec = EAV_TABLES.get(name)
if spec is not None:
    return "eav"          # 不看 metadata
```

凡是按 kind 分派的地方都因此受益：

| 位置 | 行为 |
|---|---|
| `read()` | 明确报错：**"是 EAV，没有 code/date 轴，不能用 read()"**（不是 `DatabaseError`——一个是"用法不对"，一个是"库坏了"） |
| `series_map()` | **跳过并告警**（与 static 同待遇），不把整张面板带崩 |
| `check` | 走 EAV 检查分支（登记表优先，其次才看 `metadata.kind`） |

**回归测试**：把 `metadata.kind` 手动写回 `multi`，`check` 仍输出 `ok（eav…）`、
`finances` 不进问题清单（`tests/datahub/test_eav_tables.py::test_check_ignores_stale_kind_cache`）。
之前 3 条测试都在"kind 已经是 eav"的前提下过的，所以没抓到——**测试要造"缓存是坏的"这个前提**。
