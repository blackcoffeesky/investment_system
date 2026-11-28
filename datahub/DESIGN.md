# datahub 设计文档

> **更新日期：2026-11-28**　｜　使用说明见 [`README.md`](README.md)

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
│   ├── sources/           取数（按来源分包：ashare / akshare / fuyao / other）
│   │   ├── __init__.py    SOURCES 注册表（汇总各组）；只导出 SOURCES / Source
│   │   ├── base.py        Source 契约 + 共享工具（_clip/_num/_with_prefix/_read_cached/_existing_keys）
│   │   ├── ashare.py      ashareapi：HTTP 客户端 + 限速器 + 4 个取数函数
│   │   ├── akshare.py     akshare：指数 / 期货 / 宏观 / ETF 份额 / 年度分红
│   │   ├── fuyao.py       同花顺：日更快照 / 历史批量 / 除权事件 / 财务指标
│   │   ├── guard.py       写库守卫 write_guard（"这个交易日能不能写"）
│   │   └── other.py       FRED / CFTC / 黄金与地缘 JSON / 手工 CSV
│   ├── check.py           完整性检查（缺口 / 新鲜度 / 主键）；日历在 shared
│   ├── retry.py           失败分类 transient/structural + 临时故障重试
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
| `port.py` | 跨模块唯一面：读、装配、受控动作（update / intake / backfill）、`freshness`（`catalog` 已于 2026-11 移除） | service / 各模块 cli / web |
| `core/conn.py` | 连接超时、事务边界、DDL 执行 | fin / ops |
| `core/schema.py` | 所有 DDL 的唯一出处 + 老库补列（`migrate_metadata` / `migrate_data_tables`） | update / intake / ops |
| `fin/read.py` | 只读 API + 表结构定义（列名与类型） | 服务层 / 脚本 / port |
| `fin/sources/base.py` | `Source` 契约 + 跨源共享的清洗工具 | 各取数模块 |
| `fin/sources/ashare.py` | ashareapi 的 HTTP 客户端与 4 个取数函数（限速器必须同模块） | `sources/__init__` |
| `fin/sources/akshare.py` | akshare 的全部取数（含 `INDEX_ETFS` / `ALL_ETFS`） | `sources/__init__` |
| `fin/sources/fuyao.py` | 同花顺：`stock_daily` 日更/补历史、`finances`、`adj_factor`（**所有 fuyao 调用只走它**） | `sources/__init__` / intake |
| `fin/sources/guard.py` | `write_guard()`：四条写库闸（今天 / 已收盘 / 是交易日 / 库里没有更新） | `fuyao` / 将来的快照源 |
| `fin/sources/other.py` | FRED / CFTC / 黄金与地缘 JSON / 手工 CSV | `sources/__init__` |
| `fin/sources/__init__.py` | `SOURCES` 注册表（唯一事实来源） | update / port |
| `fin/update.py` | "怎么写"（四种 kind + 元数据 + 该不该跑 + 逐批落库的 `backfill`） | 人 / 服务层 / port |
| `fin/check.py` | 查：`find_gaps`（洞 + 尾部 + 非交易日）/ `freshness`（stale/overdue）/ 主键重复 | 状态机 / 人 / port |
| `fin/retry.py` | `classify_error` / `call_with_retry`（transient 才重试） | `update` |
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
| `check` 的 `long` 表只看日期集合，不看**标的覆盖度** | `stock_daily`/`dividend`/`shares_history` 只有 5 只也全部通过 | 加一层"标的数 vs `company` 行数"的覆盖检查 |
| FRED 同源 5 表 | 一次抖动 5 张表同时失败，噪声放大 5 倍 | `update` 里按源归并报一条（**未实现**） |
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
- **`code` 一律带市场前缀**：`sh600519` / `sz000001` / `bj920045`。
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

## 10. 数据源

### 10.1 ashareapi（付费，额度制）

key 只在 `.env` / 环境变量 `ASHARE_API_KEY`，代码不存。

| 端点 | 用途 | 频率 | 单次上限 / 独立日额度 |
|---|---|---|---|
| `/v1/stocks` | 全市场清单 → `company` | 每月 | 1 次拿全 5,578 只 |
| `/v1/snapshot-batch` | 当日股本 → `shares_history` | 季度 | 200 只/次 · 200 次/日 |
| ~~`/v1/finance-batch`~~ | ~~三大报表~~ | — | **已停用**（`financials` 表已删，改 `finances`） |

**额度模型**：`total_quota=500000`、`per_min=300`；**每个批量端点有独立日额度**
（`heavy_quotas`），**没有全局日额度**（`daily_quota=null`）。计费：批量 1 次/只；
`market-kline` **500 次/次**；被 422 拒 **0 扣费**；非交易日打 `market-kline` 也 **0 扣费**。

**⚠️ 上游降级的两种表现，都必须防**：

- `ok:false` —— 看起来像"这只没数据"，实际是被限流（`reason=temporarily_unavailable`，
  `retry_after_s=60`）；
- `ok:true` 但**少表**（实测某只只回 `balance`）—— 静默缺数据。

⇒ 取数函数**逐只校验字段齐全**，缺的记失败、**不写半条**。

**续跑**：只抓"库里还没有的 code"，不靠跨天游标；`shares_history` 一次 28 次调用覆盖全市场。

**可用但没接的端点**：`/v1/changedist`（当期涨跌家数，只能对账、不能回补历史，
用来校验 `derive.breadth()` 的口径漂移）。

### 10.2 Fuyao（同花顺，不限累计调用、只限流）

客户端在 `datahub/fin/sources/fuyao.py`（**所有 fuyao 调用只走它**）：
请求头 `X-API-Key`（`Authorization: Bearer` 返回 200 但 `code≠0`，别用）；
限流（HTTP 429 / `code=4001`）按 1/2/4/8 秒退避。

| 用途 | 端点 / dump | 成本 |
|---|---|---|
| `stock_daily` 日更 | `prices/snapshot` 分页（`limit=2000`） | **3 页 / ~2 秒** |
| `stock_daily` 补历史 | `prices/historical-batch` | 单次 ≤**10 码** |
| 历史回补（10 年全市场） | dump `daily-k` | **182 MB / 366 秒** |
| 财务指标（`finances`） | `financials/indicators` | 一季 5,578 次 ≈ 31 分钟（默认关） |
| 除权事件（`adj_factor`） | `corporate-actions/adjustment-factors` | 5,578 次 ≈ 30 分钟（默认关） |

**三条必须知道的坑**：

1. **`date_ms` 是 UTC 毫秒**：直接 `pd.to_datetime(ms, unit="ms")` 会得到**前一天**
   （上海 00:00 = UTC 前一日 16:00）。必须 `tz_convert("Asia/Shanghai")`。
2. **`turnover` 是成交额（元）**，不是换手率；`volume` 是**股**。
3. **`thscode` 必须带交易所后缀**（`600519.SH`），库内是 `sh600519`；
   转换**只许有一份**：`fuyao.to_thscode()` / `to_raw_code()`。

**分页边界**：空页必须**报错**——上游异常/全市场停牌不能被当成"到末尾"静默退出。

**不限累计调用，只限流** ⇒ "全市场逐只抓"是可行的常态操作，**别拿 ashareapi 的额度/降级思路套它**。
`code=3002`（"No adjustment events"）是**这票没有分红**，不是错误。

### 10.3 网页源（`sources/other.py`）

**外部 GET 一律带浏览器 UA**（`_BROWSER_UA`，一个常量）：Cloudflare 拦 `python-requests`
默认 UA，表现为 403 或返回挑战页后挂住（`ReadTimeout`）。

**非 200 必须抛**（`_get()` 统一做：UA + 超时重试一次 + 非 200 抛 `RuntimeError` 带响应前 120 字）。
**不许写"只在 200 时做事、其余静默 continue"**——那会把 403 吞成"没有数据"。

### 10.4 手工源（`intake/`）

`wgc`（WGC 季报 → `cb_holdings`）、`wgc_etf`（WGC 月报 → `gold_etf_holdings`）。
**手工表不在 `SOURCES` 里**，`update --only` 碰到它们会报错并给出可执行命令；
`intake.registry.hint_for(table)` 是唯一出典。

---

## 11. 写库路径

### 11.1 multi 表：只更新本次返回的列

```sql
INSERT INTO t (date, c1, c2) … ON CONFLICT(date) DO UPDATE SET c1=excluded.c1, c2=excluded.c2
```

**契约**（`tests/test_write_upsert.py` 钉住）：

1. 同一份数据写两次 == 写一次；
2. **缺列不清空**：先写全列、再只写两列，其余列保持原值；
3. 主键语义不变（同一 date 一行）；
4. `single` / `long` 分支不受影响（它们本来就是 `ON CONFLICT`）。

⚠️ **禁止 `INSERT OR REPLACE`**：REPLACE = 删旧行 + 插新行，**本次没返回的列会变成 NULL**。
`csi_all` 的源只回 5 列，正是这样把 `amount`/`turnover` 清成 0 的。

**列熔断**：每次 multi 写入后比较各列非空数，某列从 **>50% 掉到 0** 就打 `[列熔断]` 到 stderr。
整列被清空不可能是正常业务——宁可吵。

### 11.2 日更守卫（`sources/guard.py::write_guard`）

任何"把当前快照当日线写"的源都必须走它。四条闸，返回 `(能不能写, 原因)`：

1. `trade_date` **就是今天**（周末/节假日接口会回上一个交易日的 `timestamp`）；
2. 今天**已收盘**（≥15:30）——盘中写进去是半天的量；
3. `trade_date` **是交易日**（快照在非交易日仍回 `timestamp=今天`，但值是上一交易日收盘）；
4. 库里**已有该日或更新**（边界 `>=`）→ 跳过（幂等，重复跑不写重）。

**判据**：问一句"这个源每一行的日期从哪来？"——来自**数据本身**（有 `date` 列）不需要守卫；
来自**"现在"**（快照/实时行情）**必须**走。

### 11.3 日更入口：先补历史、再取最新（`fuyao.stock_daily_sync`）

1. `find_gaps()` 找缺口（**含中间的洞**，不只是尾巴）；
2. `historical_batch()` 补（每批 ≤10 码）；
3. 取 `prices/snapshot` 当天，过 `write_guard`。

⚠️ **只看 `MAX(date)` 查不出"中间缺一天"**：`stock_daily` 曾"11-18 有、11-19 无、11-20 有"，
`MAX=11-20` 完全正常。

---

## 12. 派生量

### 12.1 注册表（`fin/derive.DERIVED`，9 条）

| 派生量 | 列 | 依赖 | warmup | 落位 |
|---|---|---|---|---|
| `breadth` | `adv/dec/…` | `stock_daily` | `warmup_days=400` | 交易日 |
| `dividend_yield` | 3 列 | `adj_factor`/`stock_daily`/`shares_history` | `warmup_years=0` | 年 Y → Y+1-01-01 → ffill |
| `etf_group_shares` | `sse50/csi300/csi1000` | `etf_shares` | `warmup_obs=2` | 观测日（不 ffill） |
| `etf_dispersion` | `dispersion` | `etf_shares` | `warmup_obs=2` | 观测日 → `daily` |
| `size_flow` | `flow` | `etf_shares` | `warmup_obs=2` | 观测日 → `daily` |
| `national_etf` | `net` | `etf_shares` | `warmup_obs=2` | 观测日 → `daily` |
| `national_etf_event` | `event` | `etf_shares` | `warmup_days=120` | 日频 0/1（头部为 0） |
| `cb_gold_buy` | `flow` | `cb_holdings` | `warmup_obs=3` | 稀疏（可见日） |
| `etf_flow` | `flow` | `gold_etf_holdings` | `warmup_obs=3` | 稀疏（可见日） |

**约束**：

- **不进 `metadata`**：派生量不落表、没有 `sync_log`；塞进 metadata 会让 `check` / `/data`
  分不清"缺表"和"该现算"。
- **`series_map` 认名字**，返回 `{名:列: Series}`，与普通表**形态同构**。
- **消费方不许直接 `import fin.derive`**：一律走 `port` / `series_map`
  （否则"装配"在第二处发生，两条路必然漂移）。
- **窗口不变量**：`func(start, end)` 恒等于 `func().loc[start:end]`。
- **warmup 三种单位**（`days` / `obs` / `years`），按"这个量要多少历史"选，不搞全局常量：
  pct_change 只要 1 个观测、60 日滚动分位要 60 个、**差分要"上上个"作基准**（所以是 3 不是 2）。
- **`depends_note` = "什么条件下这个数会变味"**（换源 / 改口径 / 分组变了 / 用当前股本近似），
  改派生量前先读它。

### 12.2 `dividend_yield` 的口径与对齐

| 口径 | 规则 | 为什么 |
|---|---|---|
| 单位 | **百分数**（2.1258 = 2.13%） | 与其它收益率列一致，不要再 /100 |
| 归年 | 按**除权日**归年，不是报告期 | 与报告期口径的表不可直接比（年报分红多数次年除权，两套口径错开一年） |
| 市值加权分母 | **全市场**（不分红股分子记 0、分母照算） | 否则退化成"分红股平均" |
| 中位数 | **只统计分红股** | 掺入 0 会把中位数拉到 0 |
| 股本 | 用**当前**股本近似 | 历史股本不可得；`stock_daily` 不含退市股 → 幸存者偏差 |

年 Y 的值对齐到 **Y+1-01-01**（年 Y 的除息日全落在 Y 内，数据到 Y+1 才完整），再 ffill 到日频。
这是"数据期→可见日"的换算，与 `lag_days` 同类，**归适配层**。
它**只挂 causual.ashare**（算一次 3.7 秒，状态层不用）。

**全市场分红数据只能从 Fuyao / 交易所公告来，不能用 ashareapi**（`/v1/dividend` 已停用）。

### 12.3 已知欠账：`etf_group_shares` 的 `0.0`

某组当天**没有观测**时会被写成 `0.0`（pandas 对全 NaN 求和的默认行为，旧 `sum(axis=1)` 的遗产），
2016 年共 **162 格**。这不是"少一个值"，而是**伪造了一次 −100% 下跌再 +∞ 回升**，
会污染 `ETF_DISPERSION` / `SIZE_FLOW` / `NATIONAL_ETF` 的 2016 段（HMM 训练区间）。

**修复方式**：`reindex` 到观测日并集后**保持 NaN**，不要 `fillna(0)`。
⚠️ 修它会让特征变 → 标签变 → **边快照失效**，**必须同轮重拟合 + 重估 edges，不能单独改**。

### 12.4 为什么不入库

估值入库约 5000×2500×4 个浮点 ≈ 3 GB，且都是可推导量；一旦入库就要操心"股本变了要重算历史"。

---

## 13. 新鲜度与缺口检查

### 13.1 两个布尔量（`fin.check.freshness`）

| `stale` | `overdue` | 含义 | 动作 |
|---|---|---|---|
| False | False | 已是最新 | 无 |
| True | False | **不可抗力**：源还没发布，在 `lag_days` 宽限内 | 等，不报警 |
| True | True | **忘记更新**：宽限**已过**（严格大于截止日）还没到 | 报警 |

`expect` 按 `freq` 推：`D`=上一工作日、`M`=上月月末、`Q`=上季末、`Y`=上年末；
`W` / `SPARSE` **有意不判**。每行带 `freq` —— 下游要筛月频表就**看这个字段**，
**不要维护手写清单**（`MONTH_TABLES` 已经漂过两次）。

**约束**：

- **`lag_days` 的权威来源是 `update.FREQ_HINT`**，不是 `metadata` 那一列：
  `_ensure_meta` 每轮都会用 `FREQ_HINT` 覆盖 `metadata.lag_days`，
  所以**手改 `UPDATE metadata SET lag_days=…` 会被下一轮冲掉**。
- **`lag_days` 同时承担两个职责**：①对齐日（数据何时可见）②新鲜度宽限。
  **设大了是双重损失**：既推迟可见（前视风险 / 历史对齐整体后移），又推迟报警。
- `lag_days ≥ 周期长度` 时 `overdue` **永远为 False**（`expect` 随今天往前滚）——
  低频表的 lag 不能设得比周期还长。
- 严格 `>`：`expect + lag` 当天是**发布截止日**（数据可能收盘后才入），当天不算 overdue。
  连带：`lag=0` 的日频表在严格 `>` 下**永远不会 overdue**，靠 `stale` 报。
- `static` / `eav` 跳过（没有日期轴）；**冻结表**（`SOURCES` 里 `enabled=False`）也不进，
  否则同一张表 `freshness` 说"有意停更"、`check` 说"缺 N 天"，两处结论打脸。

### 13.2 `find_gaps`（`fin.check`）

```python
find_gaps(table, *, lookback=None, max_age_days=None, today=None, path=None) -> dict
# {"table", "freq", "missing", "stale", "expected_latest", "non_trading"}
```

**约束**：

- **不吃 `lag_days`**：宽限是 `freshness` 的语义，不是"数据缺"。只报事实，调用方自己套宽限。
- **低频表按周期比，不按日期相等**：`M/Q/Y` 的数据日期是"披露日/月末"之类的任意一天，
  逐日比会把整月算成缺口。
- **窗口起点不早于表首日**：表还没建的时候不算缺口。
- `lookback` 默认**按频率**：`D` 60 个交易日、`W` 26 周、`M` 24 月、`Q` 8 季、`Y` 5 年。
- **`today=None`** 可指定"今天是哪天"。⚠️ 传 `"2026-11-20"` 会被当 **00:00（未收盘）**
  → 退回前一个交易日；要表达"已收盘"就带时间（`"2026-11-20 16:00"`）。
  **测试必须传**，否则结果随真时间变、一天红一次。
- **`non_trading`**（库里有、但不是交易日）：**只查 `market=CN` 的 `D` 表**。
  美股/全球序列本来就有非交易日数据（`fed_rate` 每日历日都有值、FRED 美股假日照发、
  `world_tension` 是新闻指数）；`W/M/Q/Y` 的日期是周期末日，落在周末正常。
  判定走 `is_trading_day`（交易所日历）——A 股有**调休开市**、也有**节假日休市**，
  `weekday() < 5` 是错的。
- **交易日历只有一份，在 `shared/utils/trading_calendar.py`**
  （`trading_days` / `expected_periods` / `is_trading_day` / `last_closed_trading_day`）。
  datahub 只 import、**不再定义**——曾因同名函数覆盖（`trading_days` 两份），
  17 张日频表的缺口检查整体失效而页面只显示一条"日历失败"。

### 13.3 `check.run()` 的报告

`run(path, recent_days=90, today=None) -> DataFrame`，每行带 `status`：

| status | 含义 |
|---|---|
| `data` | 数据真有问题（缺数 / 停更 / 重复 / 空值） |
| `warning` | **只有**非交易日脏数据 |
| `check_failed` | **检查本身没跑成**（日历失败），不是数据问题 |

报告三档：`非交易日数据 N 天` / `最新停在 X，应到 Y` / `近期缺 N 天` + `历史缺口 N 天`。
`unacked(missing, acked, freq)`：`D` 按日、`M/Q/Y` 按周期过滤已登记缺口。

⚠️ **分工**：`check` 报"数据缺了没"（事实）；面向用户的**到期告警文案**在
`service/ops/states/data.py::overdue_tables`（它自己套 `lag_days` 宽限）。两边都不许吞掉对方。

---

## 14. 取数入口与缓存

- **`get_data(panel)` 是全系统唯一取数入口**，只在装配点调（`*/cli.py`、`service/**`、
  `*/web/routes.py`）。核心代码（`port` / `pipeline` / `features` / `analysis`）**只接收 DataFrame**。
  有静态测试钉住（`tests/cross/test_layering.py`），且带**反向校验**：
  "定义处"与"至少一个装配点"都必须真的存在，否则检查器会"永远绿"。
- **进程内缓存** `_CACHE`（`(panel, start, end) → 面板`），返回**副本**；`DataPort.run_update()` 结束时
  （`finally`）`clear_cache()`，`freshness=True` 是逃生口。
  **边界**：多 worker 各一份；别的进程写库**不会**让它失效。
- `series_map(names, start, end, codes)` 三个参数**都落到 SQL**；`codes` **只对 long** 有意义，
  打到其它 kind **告警并忽略**（静默忽略 = 调用方以为筛过、实际拿全量）。
- **窗口不变量**：`get_data(p, start, end) == get_data(p).loc[start:end]`——
  每列在 adapter 的 `_WARMUP` 里声明往前多取多少天（0/10/45/130），取完再切。

---

## 15. 失败分类与重试（`fin/retry.py`）

| kind | 典型异常 |
|---|---|
| `transient` | `requests` 的 `ConnectionError`/`Timeout`/`ConnectTimeout`/`ReadTimeout`、`TimeoutError`、`ConnectionResetError`、`ssl.SSLError`、`urllib.error.URLError`、`http.client.RemoteDisconnected`、**`pandas_datareader` 的 `RemoteDataError`**、HTTP 5xx / 429 |
| `structural` | 解析失败、列名对不上、`KeyError`、认证失败、源返回空但本不该空、schema 变了 |

**约束**：

- 按**异常类型**判，不靠字符串（`type(e).__name__` 当兜底也不行）。
- **拿不准一律 `structural`**：宁可吵，不可静默。
- `RemoteDataError` **不继承** `requests` 异常（内部包 `urllib` 的 `HTTPError`）——必须按类名单独认，
  否则 FRED 的报错会被判 structural，降级失效。
- `call_with_retry(fn, retries=2, delays=(1,3))`：**只对 transient 重试**，structural 直接抛。
- `UpdateReport.all_transient`：有失败且**全是** transient 才 True（没失败 = False）。
  上层据此决定 error 还是 warn。

---

## 16. 已知限制与陷阱

| 项 | 说明 |
|---|---|
| `industrial_yoy` / `exports_yoy` | akshare 只有财经日历接口，数据停在 2025-08（`enabled=False`） |
| `geopolitic_risk` | FRED 无此序列，旧数据冻结；新数据用 `world_tension`（2026-03-23 起） |
| `share` 类字段 | `statsmodels/table` 里的股本用**当前**近似，越往前越不准 |
| `finances` | EAV 表（**不能 `read`**，4 列主键 + `value` TEXT）；`metadata.freq` 被写成 `D`（原始是季报），解冻后会天天报缺口 |
| EAV 登记 | "这张表是不是 EAV" **只认 `core/schema.EAV_TABLES`**，`metadata.kind` 只是缓存；**不要靠列名/形态去猜**（猜法一改就漏） |
| FRED 单点 | `vix`/`usdcny`/`usd_index`/`fed_rate`/`real_yield_10y` 全在 FRED 一家，一次抖动 5 表同时失败；按源归并**未实现** |
| `--backfill` | 主块必须在**文件末尾**（否则 `--backfill` 先执行、`NameError`）；**新增模块级函数不要往它后面追加** |
| 新鲜度类测试 | 阈值按**日历日**算：长周末/长假后会**假红**（5 天阈值遇上三天连休），见红先算工作日 |
| 备份 | **不能直接拷 `.sqlite`**：WAL 里有未落盘的页，要用 `sqlite3 .backup` 或 `VACUUM INTO` |

**不要做的事**：

- 不要回填 `dividend` 表（已并入 `adj_factor`；两边冲突时以 `adj_factor` 为准、`plan` 留空）。
- 不要在 `etf_group_shares` 里 `fillna(0)`（见 §12.3）。
- 不要把 ETF 代码/分组写进 adapter（分组知识只在 `fin/derive._ETF_GROUPS`）。
- 不要用 `INSERT OR REPLACE` 写 multi 表（见 §11.1）。
- 不要为"宏观覆盖更全"把 `shibor3m` 加回来（见 §17）。
- **不要拿 GPRC 去补 `world_tension`**：两套尺子拼起来比缺一段更糟。

---

## 17. 变更记录

| 日期 | 变更 | 结论 / 约束 |
|---|---|---|
| 11-14 | 一表一序列落地（宽表只合并同源同频同生命周期） | 表多但语义单一；`SELECT * FROM vix` 就是全部 |
| 11-14 | `breadth` / `dividend_yield` 改为现算，不建表 | 派生量不落表、不进 `metadata` |
| 11-14 | `dividend` 并入 `adj_factor`，删 `breadth_*` / `dividend_annual` | 表 46 → 41；冲突时 `plan` 留空、**别回填 `dividend`** |
| 11-14 | 额度实测（专业版） | 有独立 `heavy_quota`、无全局日额度；降级有 `ok:false` 与"少表"两种表现 |
| 11-16 | `metadata` 删 `source` / `depends` | 派生量不进 metadata；`check` / `/data` 分不清"缺表"与"该现算" |
| 11-16 | 窗口与 `codes` 下推 SQL | `codes` 打非 long 表要**告警**，不许静默忽略 |
| 11-16 | 派生量注册表 + warmup | 窗口不变量；warmup 三种单位 |
| 11-16 | `etf_group_shares` 沉进数据层 | adapter 不许有 ETF 代码；`0.0` 欠账待重拟合时修 |
| 11-16 | 通用特征从 `shared/utils/features.py` 迁入 derive | 该文件已删；面板列为唯一出口 |
| 11-16 | multi 写路径改 `ON CONFLICT DO UPDATE` + 列熔断 | **不许 `INSERT OR REPLACE`**（会清空未返回的列） |
| 11-17 | 接 Fuyao（`stock_daily` 先改 dump） | `date_ms` 是 UTC 毫秒；`turnover` 是成交额；`thscode` 要后缀 |
| 11-17 | 日更改 `prices/snapshot` 分页 | 3 页 ~2 秒；dump 只用于历史回补 |
| 11-17 | 删 `financials`，登记 `finances`（EAV） | EAV 不能 `read`；主键 4 列、`value` 是 TEXT |
| 11-17 | `adj_factor` 全市场回填 | **只增不改**；源函数不返回它不认识的列（曾把 985 行配股清成 0） |
| 11-17 | `world_tension` 修 Cloudflare UA | 外部 GET 必带浏览器 UA；**非 200 必响** |
| 11-18 | 因果层 `geopolitic_risk` → `world_tension` | **换测量、不缩放**；2026-03-23 前 NaN 是预期；边样本量降到 ~150 |
| 11-18 | 新鲜度搬回数据层 | `stale` / `overdue` 两个布尔量；`lag ≥ 周期` 时 overdue 不可达 |
| 11-18 | `gold_etf_holdings` lag 40 → 20 | `lag_days` 兼任"对齐日"与"宽限"，**不能设大** |
| 11-21 | 日更"先补历史再取最新" | 守卫四条；周六曾把周五数据写成新的一天 → 必须判交易日 |
| 11-22 | `check` 职责切分（日历→`shared`、`write_guard`→`sources/guard.py`） | 同名函数覆盖曾让 17 张日频表检查整体失效 |
| 11-22 | 「冻结表不进 check」回归并修复 | 冻结表两边口径必须一致，否则互相打脸 |
| 11-23 | `find_gaps` 加 `today` | 测试必须钉 `today`，否则每天红一次 |
| 11-24 | `find_gaps` 加 `non_trading` | 只查 `market=CN` 的 `D` 表；`W/M/Q/Y` 与美股/全球序列不查 |
| 11-25 | 失败分类 `transient`/`structural` + 重试 | `RemoteDataError` 不继承 `requests`；拿不准归 structural |
| 11-28 | 删 `shibor3m`（表 + 源 + 面板列 + 元数据） | 源数 34 → 33、表 41 → 40；上游滞后 8 天且用户不关注银行间流动性 |
