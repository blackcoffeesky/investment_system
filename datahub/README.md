# datahub —— 数据层

> **更新日期：2026-11-14**　｜　配套设计文档：[`DESIGN.md`](DESIGN.md)　｜　代理约束：[`../AGENTS.md`](../AGENTS.md)

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

**2026-11-13 起**：`csi_all` 合并了成交额/换手率（三表合一）；`etf_shares` 从 multi 改成 long。
**2026-11-14 起**：新增公司四表 `company` / `shares_history` / `dividend` / `financials`，
接入 ashareapi；`metadata` 加 `source` / `depends` 两列（标记原始 vs 派生）。
**过渡中**：`breadth_*` 已停止采集（`SOURCES` 里两条注册已删），但**表还在**
（`state_layer` 仍在读它）——等派生函数与旧表比对一致后再删表。
`csi_all_amount` / `csi_all_turnover` 同理：已合并进 `csi_all`，不再单独采集。

---

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

| 表 | kind | 说明 |
|---|---|---|
| `csi_all` | multi | open/high/low/close/volume/amount/turnover |
| `gold_cny` | multi | 同上，上期所主力连续 |
| `etf_shares` | long | 24 只 ETF 份额，`(code, date, shares)`；各只起始日不同 |
| `dividend_annual` | multi | 分红总额/净利润/分红率/家数 |
| `stock_daily` / `adj_factor` / `financials` / `dividend` / `shares_history` | long | 标的是数据（五千只没法一列一只） |
| `company` | static | 全市场清单，一个标的一行 |
| 其余 | single | 单序列 |

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
                                #   kind/columns/market/lag_days/source/depends
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
```

**`series_map()` 会跳过 `static` 表并打一行警告**——static 没有日期轴，
不是时间序列；调用方把整份表清单传进来也不会炸。

**写接口不对外。** 写库只发生在 `update.py` / `intake` 内部。

---

## 6. 更新数据（自动源）

```bash
python -m datahub.fin.update                    # 全部启用项，更新到今天
python -m datahub.fin.update --start 2024-01-01 --end 2026-10-01
python -m datahub.fin.update --only vix,csi_all
python -m datahub.fin.update --dry-run          # 只抓不写
```

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

报告分两档：`近期缺 N 天`（90 天内，多半是忘了更新）与 `历史缺口 N 天`（源数据遗留）。

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
| `financials` | 走 `finance-batch` 回填的 code 只有**最近 4 期**（`只数×periods≤40` 的硬限制）；早期 5 只是 8 期。全市场 5578 只需 **6 天**（100 次/日 × 10 只），靠"只抓缺的 code"续跑 |

## 11. long 表的读法

`long` 用于"标的是数据而不是结构"的场景（五千只股票没法一列一只）。
schema 登记在 `datahub/fin/read.py` 的 `LONG_SCHEMAS` / `STATIC_SCHEMAS`，建表按登记来。

```python
from datahub.fin import read_long
from datahub.fin.read import pivot        # 注意：pivot 在 read 模块里，不在包级

df = read_long("stock_daily", start="2026-01-01", codes=["000001"])
wide = pivot(df, "close")                 # long → 日期 × 代码
```

装配给资产面板用 `DataPort.series_map(tables)`——single/multi/long 都统一成 `{key: Series}`。

`freq="SPARSE"` 的表（事件表、份额快照）不做逐日缺口检查，只看是否很久没更新。
