# datahub —— 数据层

> **更新日期：2026-11-28**　｜　配套设计文档：[`DESIGN.md`](DESIGN.md)　｜　代理约束：[`../AGENTS.md`](../AGENTS.md)

与 `state_layer`、`causual`、`service` 同级别的独立包。SQLite 存储，**按数据本身命名**。
唯一职责：把外部数据变成一张张干净的表，供任何上层消费。

---

## 0. 目录结构

```
datahub/
├── __init__.py      ROOT / DB_PATH / __version__
├── port.py          DataPort —— 其他模块只准通过它拿数据（AGENTS §2①）
├── core/            conn.py（连接与事务） schema.py（DDL + 老库补列） errors.py
├── fin/             金融数据：read / update / sources / check / intake
├── ops/             运维状态：OpsStore（状态 + 消息，单事务落盘）
└── web/             /data 页（表清单 + 手工录入）
```

**两个命名空间是隔离的**：`fin` 只认 `metadata` 里登记过的表；`ops` 只碰 `ops_*`。
它们共用一个 sqlite 文件，唯一理由是"状态 + 消息"要能一个事务写完。

## 1. 两条使用路径

| 脚本 | 用途 | 什么时候跑 |
|---|---|---|
| `update.py` | **日常更新**：按 `SOURCES` 注册表抓取、upsert、刷新元数据 | 每天 |

日常唯一入口是 `update.py`；手工数据走 `intake`。

**当前表结构的两个要点**（历史沿革见 [DESIGN §17 变更记录](DESIGN.md)）：

- `csi_all` 是**三表合一**（指数 + 成交额 + 换手率），不是"只回 5 列"的裸指数；
- `etf_shares` 是 `long`（一行一个 ETF·日期），不是 multi。

## 2. 命名原则

**数据层不为使用方考虑。** 不叫 `ashare_close`，而叫 `csi_all`——它是什么就叫什么。
一份数据可能被多方使用，加消费方前缀会把它绑死在某个使用方身上。

---

## 3. 四种表（`metadata.kind`）

| kind | 结构 | `read()` 返回 |
|---|---|---|
| `single` | `date TEXT PK, value REAL` | `Series` |
| `multi` | `date TEXT PK, <列1>, <列2>, …` | `DataFrame`（或 `column=` 取 Series） |
| `long` | `code, date, <字段…>`，PK `(code,date)` | `DataFrame`（`read_long()`；`pivot()` 转宽表） |
| `static` | `code, <字段…>`，PK `code`，无日期轴 | `DataFrame`（`read_static()`） |

**合并的前提（缺一不可）：共享 date 主键、同频、同源、同生命周期。**

### 写库契约（务必知道）

**往 multi 表写数只更新"本次返回的列"，其余列保持原值。**

```sql
INSERT INTO t (date, c1, c2) … ON CONFLICT(date) DO UPDATE SET c1=excluded.c1, c2=excluded.c2
```

改前用的是 `INSERT OR REPLACE`——REPLACE 是"删旧行 + 插新行"，**本次没返回的列会变成
NULL**。`csi_all` 的源只回 5 列，于是每次刷新都把 `amount`/`turnover` 从 3503 清成 0，
**没有任何测试响过**（state 层偶然看 `/state` 才发现）。这条现在是**契约**，不是实现细节：
写路径的四个行为（等价 / 缺列不清空 / 主键语义不变 / single·long 不受影响）由
`tests/datahub/test_write_upsert.py` 钉住。

**列熔断**：每次 multi 写入后比较各列非空数，某列从 **>50% 掉到 0** 就打
`[列熔断] …` 到 stderr。整列被清空不可能是正常业务，只可能是源缺列/写路径错——宁可吵。


| 表 | kind | 说明 |
|---|---|---|
| `csi_all` | multi | open/high/low/close/volume/amount/turnover |
| `gold_cny` | multi | 同上，上期所主力连续 |
| `etf_shares` | long | 24 只 ETF 份额，`(code, date, shares)`；各只起始日不同 |
| `dividend_annual` | multi | 分红总额/净利润/分红率/家数 |
| `stock_daily` / `adj_factor` / `dividend` / `shares_history` | long | 标的是数据（五千只没法一列一只） |
| `company` | static | 全市场清单，一个标的一行 |
| 其余 | single | 单序列 |

> **`adj_factor` 已吸收 `dividend`**：数值以 Fuyao 合计为准，
> 多出 `report_end` / `plan` 两列。`dividend` 表先留着、标了"待删"，
> 等股息率派生验证过再删；`SOURCES` 里 `ashare_dividend` 已 `enabled=False`。

**代码格式**：所有 `long` / `static` 表的 `code` 一律带市场前缀
（`sh600519` / `sz000001` / `bj920045`）。统一规则：
`stock_daily`(1040 万行) / `adj_factor` / `etf_shares` 已从 Fuyao 的裸 6 位迁过来；
新增数据用 `sources._with_prefix()` 归一，**不要再写裸码**。

---

## 4. 什么算"原始数据"

**判据不是"是不是聚合值"，而是"去掉它之后，还能不能用库里其他数据算出来"。**

- 算得出来 → 派生，**不入库**（`etf_size_flow`、`national_etf`、`cb_gold_buy`、`ETF_FLOW`）
- 算不出来 → 原始，必须入库（`dividend_annual`：库里没有逐股财务，删了就拼不回来）

派生量由消费层现算，避免"库里的是哪一版"的歧义。

---

## 5. 读接口（唯一对外暴露的部分）

```python
from datahub.fin import read, metadata, tables, run_check, missing

tables()                        # 表名列表（= metadata 里登记过的表）
metadata()                      # name/display/freq/unit/start/end/n_rows/note/
                                #   kind/columns/market/lag_days
read("vix")                     # single  → Series
read("csi_all")                 # multi   → DataFrame
read("csi_all", column="close") #         → Series
read_many(["vix", "usdcny"])    # 多个 single → DataFrame
run_check()                     # 完整性检查，空 = 通过
missing()                       # 已人工确认的正常缺口
```

注意**不要**写 `from datahub.fin import check`：`check` 是子模块名，
`datahub/fin/__init__.py` 里已把它换成 `run_check` / `missing` 两个函数名。

派生量（不落表、现算）：

```python
from datahub.fin.derive import breadth
breadth("2026-11-10", "2026-11-10")   # 市场宽度：adv/dec/flat/limit_up/limit_down/total

from datahub.fin.derive import dividend_yield
dividend_yield()   # 年度股息率（%）：yield_mcap_weighted / yield_median / n_stocks
```

**跨模块**不要直接 import `fin.derive`，走 `DataPort`：

```python
port.freshness(tables)        # 每张表的新鲜度（stale / overdue 两个布尔量）
port.series_map(["breadth"], start, end)          # 派生量：市场宽度
port.series_map(["dividend_yield"])               # 派生量：年度股息率（3.7s，别按年循环）
# ⚠️ `port.breadth()` / `port.dividend_yield()` / `port.catalog()` **均已移除**，
#    派生量一律走 `series_map`（清单见 `fin/derive.py` 的 `DERIVED`）
port.tables()                 # 表名列表
port.metadata()               # 每张表的 freq / lag_days / 行数 / 起止日期
port.run_update(...)          # 跑更新（结束时自动 clear_cache()）
```

**`series_map()` 会跳过 `static` 表并打一行警告**——static 没有日期轴，
不是时间序列；调用方把整份表清单传进来也不会炸。

**窗口和代码过滤要显式传**：

```python
port.series_map(tables)                                   # 全量（兼容旧调用）
port.series_map(tables, start="2025-01-01", end="2026-11-10")   # 闭区间，下推到 SQL
port.series_map(["stock_daily"], codes=["sh600519"])      # 只对 long 表有效
```

`start` / `end` 会下推成 `WHERE date >= ? AND date <= ?`；`codes` 下推成
`WHERE code IN (...)`（**只有 long 表支持**，打到 single/multi 会告警并忽略）。
long 表的 key 集合**随窗口收缩**（窗口内没数据的代码不出现），这不算筛掉。

**派生量也能从 `series_map` 拿**：

```python
port.series_map(["breadth"], start="2025-01-01", end="2025-12-31")
# → {"breadth:adv": Series, "breadth:dec": …, …}   形态和普通表一样
port.series_map(["dividend_yield"])   # → {"dividend_yield:yield_mcap_weighted": …}
```

派生量注册在 `datahub/fin/derive.py` 的 `DERIVED`（不落表、不进 metadata），
**传窗口 == 全量切片**（自带 warmup，见 DESIGN §10.2）。
`DataPort.breadth()` / `dividend_yield()` 是过渡期保留的旧入口，迁完就该删。

现有 9 个派生量：`breadth` / `dividend_yield` / `etf_group_shares` /
`etf_dispersion` / `size_flow` / `national_etf` / `national_etf_event` /
`cb_gold_buy` / `etf_flow`。前三个是"数据集"，后六个是**通用业务特征**
（派生量自己读库；`shared/utils/features.py` **已删**，不要再往那儿加特征）；
A 股面板直接多出 `etf_dispersion` 等 4 列，黄金面板多出 `cb_gold_buy` / `etf_flow`。

**写接口不对外。** 写库只发生在 `update.py` / `intake` 内部。

---

## 6. 更新数据（自动源）

```bash
python -m datahub.fin.update                    # 全部启用项，更新到今天
python -m datahub.fin.update --start 2024-01-01 --end 2026-10-01
python -m datahub.fin.update --only vix,csi_all
python -m datahub.fin.update --dry-run          # 只抓不写
```

**日更（`stock_daily`）**：源 = `fuyao.stock_daily_sync()` —— **先补历史、再取最新**：

1. 用 `fin.check.find_gaps()` 找最近的洞（含**中间的洞**，不只是尾巴）；
2. 用 `fuyao.historical_batch()` 补（每批 ≤10 个代码）；
3. 再取 `prices/snapshot` 当天行情，**写入前过 `sources/guard.py::write_guard()`** 四条闸：
   日期==今天 / 已收盘（≥15:30）/ **是交易日** / 库里没有该日或更新。

```bash
python -m datahub.fin.update --only stock_daily     # 收盘后跑
```

**历史回补**用 `fuyao` 的 `daily-k` dump（10 年全市场，182 MB / 366 秒，见 DESIGN §18）。
复权事件 `adj_factor` 也走 Fuyao（`corporate-actions/adjustment-factors`）。细节见 DESIGN §18 / §24。

**失败分两档**（`fin/retry.py`）：`transient`（网络/超时/5xx/限流，含 FRED 的 `RemoteDataError`）
会自动重试 2 次（1s/3s）；仍失败则明细里标 `kind`，`report.all_transient` 汇总给上层——
service 据此把"全是临时网络问题"的失败降级成 warn。见 DESIGN §27。

**当前规模**（实测）：`SOURCES` **33 个 API 源**（28 个 enabled）+ `intake` **2 个手工源**；
`tables()` = **40** 张表；`DERIVED` = **9** 条派生量。

数据源写在 `sources.py` 的 `SOURCES` 注册表里：

```python
@dataclass(frozen=True)
class Source:
    table: str          # 目标表；"a|b|c" 表示拆成多张单表
    func: Callable      # 取数函数
    params: dict = field(default_factory=dict)
    kind: str = "auto"  # auto / manual（只影响 note 前缀，不影响调度）
    shape: str = "auto"  # auto / long / static（**决定用哪个 writer**）
    enabled: bool = True  # False = 日常更新跳过
    note: str = ""      # 写进 metadata.note
```

**所有取数函数签名统一**：

```python
def f(start: str, end: str, table: str = "", **_) -> pd.Series | pd.DataFrame
```

API 不支持区间就在函数内过滤。返回值只含数据本身——对齐、ffill、写库都是 `update` 的事。

**写入语义**：只 upsert、不删历史；日期截断到 `end`；每轮结束刷新 metadata；
失败记 `data/missing/update_failures.log`，不影响其他表。

---

## 6.1 股本变动历史（`shares_history`）

一行 = **一笔股本变动事件**（1990 起，来自巨潮），列：
`code / date / event_code / event_type / delta_total / total_after / float_after`。
`event_code` 是原因编码（`015011`）、`event_type` 是中文（`转增`）。**没有 `kind` 列**。

```bash
# 首次回填：一次性脚本在 trash/（不进 intake）；大文件在 data/ 且 gitignored
# 一次性脚本放在本地 trash/（gitignored，不入库）；换机器要自己留一份
python trash/intake_shares.py "data/股本数据/股本变动历史明细.csv" --dry-run   # 先看统计
python trash/intake_shares.py "data/股本数据/股本变动历史明细.csv"             # 真写

# 日常增量（源 enabled=False，手动/季度跑；5,658 只 ≈ 5-7 分钟）
python -m datahub.fin.update --only shares_history
```

三条口径（错了会**静默错**，见 [DESIGN §18](DESIGN.md)）：
**剔掉 `定期报告`**（约 58%，那不是变动）、**单位 ×1e4**（源是万股、库是股）、
**代码按代码段加前缀**（`920xxx` → `bj`，不能按首字符猜）。

`metadata.freq = SPARSE`：事件表不按季度判缺口，只看"超过 **180 天**没新增"。

---

## 7. 手工数据：`intake/`

```
datahub/fin/intake/
├── raw/            # 原始文件（xls/csv/pdf 导出），原样留档
├── clean/          # 解析后的标准 CSV（只放待写入/覆盖的行）
├── parsers/        # 一源一个解析函数
│   ├── wgc_holdings.py    WGC 季报 → cb_holdings
│   ├── wgc_etf.py         WGC ETF 月报 → gold_etf_holdings
│   └── cb_basket.txt      87 国篮子
├── registry.py     # 源标识 → (parser, 目标表, 频率)
├── cli.py          # python -m datahub.fin.intake <source> <raw 文件>
└── stock_daily.py  # 一次性：同花顺 parquet → stock_daily / adj_factor
```

```bash
python -m datahub.fin.intake wgc <xlsx> --load    # 央行持仓（季）
python -m datahub.fin.intake wgc_etf <xlsx> --load # 全球黄金 ETF 持仓（月）
```

- `clean/<table>.csv` 只包含**要写的行**：库里没有的日期 + 被源方修订过的日期
- **解析失败会明确提示"raw 的结构可能变了"**，并指出是哪一环对不上
- `AWAITED`（源方尚未收到该国数据）按上一期补齐，即持仓不变

---

## 8. 完整性检查

```bash
python -m datahub.fin.check
python -m datahub.fin.check --ack vix:2026-10-20 --reason "美股休市数据源未更新"
```

按 `metadata.freq` + `metadata.market` 用**交易日历**生成"应该存在的日期"，与实际做差，
再减掉 `missing` 表里已确认的缺口：

- `market=CN` → 上交所日历（含春节/国庆）
- `market=US` → NYSE 日历
- `market=NONE` → 不挂交易日历，只按自然月末/周末

报告分三档：`非交易日数据 N 天`（**脏数据**：库里有、但不是交易日）、
`最新停在 X，应到 Y`（**尾部停更**）、`近期缺 N 天`（90 天内）/ `历史缺口 N 天`（源数据遗留）。

**底层是 `fin.check.find_gaps()`**（返回 dict，可单独调用）：

```python
find_gaps("stock_daily", lookback=None, max_age_days=None, today=None, path=None)
# {"table", "freq", "missing": [日期…], "stale": 实际最新|None,
#  "expected_latest": 应到最新, "non_trading": [日期…]}
```

- `today=` 可指定"今天是哪天"（默认现在）。**测试必须传**——否则结果随真时间变，一天红一次。
  `run(..., today=)` 也透传。
- **不吃 `lag_days`**："源还没发布"是 `freshness` 的宽限语义，不是"数据缺"。
- `non_trading`：**只对 `market=CN` 的 `D` 表**查（美股/全球序列本来就有周末与假日数据，
  `fed_rate` 每个日历日都有值；`W/M/Q/Y` 的日期是周期末日，落在周末正常）。
- 结果表带 `status` 列：`data`（数据有问题）/ `warning`（**只有**非交易日脏数据）/
  `check_failed`（**日历失败 = 检查没跑成**，不是数据问题）。

---

## 9. 公布滞后：`metadata.lag_days`

数据层日期是**数据所属期末**（2026-09-30 是 9 月的 CPI），但 9 月 CPI 要到 10 月 9 日才公布。
消费层按 `lag_days` 推后使用，才不会在回测里用到"未来才知道"的数字。

| 数据 | 滞后 | 依据 |
|---|---|---|
| `pmi` | 1 | 次月 1 日发布 |
| `cpi_yoy`、`ppi_yoy` | 9 | 统计局每月 9 日左右 |
| `m1_*`、`m2_*` | 12 | 央行每月 12 日左右 |
| `retail_sales`、`industrial_yoy`、`total_*`、`equity_fin_monthly` | 15 | 次月中旬 |
| `exports_yoy` | 13 | 海关次月 13 日左右 |
| `cb_holdings` | 45 | WGC 季报滞后 |
| `dividend_annual` | 90 | 年报汇总 |

**这是数据层的属性，不是消费方的假设**——所以放在 `metadata`，而不是写死在适配层代码里。

---

## 10. 已知限制

| 项 | 说明 |
|---|---|
| `industrial_yoy` / `exports_yoy` | akshare 只有财经日历接口，数据停在 2025-08 |
| `geopolitic_risk` | FRED 无此序列，旧数据冻结；新数据改用 `world_tension`（2026-03 起） |
| `etf_shares` | 各只起始日跨度大（2016-01 ~ 2025-04），部分标的有采集空洞 |
| `cb_holdings` | 2026Q2 有 23/87 国 `AWAITED`，按 Q1 补齐 → 低估 |
| `finances` | EAV 表（**不能 `read`**）；`metadata.freq` 被写成 `D`（原始数据是季报），解冻后会天天报缺口——已记 DESIGN §26.6 |
| FRED 单点 | `vix` / `usdcny` / `usd_index` / `fed_rate` / `real_yield_10y` **全在 FRED 一家**：一次抖动 5 张表同时失败。按源归并**未实现**，见 DESIGN §27.4 |

## 10.1 已删除的表

`breadth_adv` / `breadth_dec` / `breadth_limitup`、`dividend_annual`、`dividend`、
`financials`（旧财报表）、`shibor3m` **均已删除**，当前 `tables()` = **40** 张。

替代关系：`derive.breadth()` / `derive.dividend_yield()` / `adj_factor`（吸收了
`report_end`/`plan`）/ `finances`（同花顺 EAV）。

删除的**原因与结论**见 [DESIGN §17 变更记录](DESIGN.md)；
删除前的快照留在 `fin/intake/clean/dropped_20261114/`。

## 11. long 表的读法

`long` 用于"标的是数据而不是结构"的场景（五千只股票没法一列一只）。
schema 登记在 `datahub/fin/read.py` 的 `LONG_SCHEMAS` / `STATIC_SCHEMAS`，建表按登记来。

```python
from datahub.fin import read_long
from datahub.fin.read import pivot        # 注意：pivot 在 read 模块里，不在包级

df = read_long("stock_daily", start="2026-01-01", codes=["000001"])
wide = pivot(df, "close")                 # long → 日期 × 代码
```

装配点（`cli.py` / `service/` / `web/routes.py`）拿数据走 **`shared.adapter.get_data(panel)`**；
`series_map(tables)` 是数据层内部的主接口（single/multi/long 统一成 `{key: Series}`），
**上层别直接调**——那等于把装配做两遍。

`freq="SPARSE"` 的表（事件表、份额快照）不做逐日缺口检查，只看是否很久没更新。
