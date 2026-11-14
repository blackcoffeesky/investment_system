"""派生数据：由库里已有的原始表现算，**不落表**。

目前只有一件：市场宽度。

    from datahub.fin.derive import breadth
    df = breadth("2026-01-01", "2026-11-10")   # index=date，列见下

返回列：`adv / dec / flat / limit_up / limit_down / total`。

口径（最容易各算各的地方，写死在这里）
----------------------------------------
1. **涨 / 跌 / 平**：当日收盘 vs **该股上一个有成交的交易日**收盘。
   `close > prev` 算涨，`<` 算跌，`==` 算平。
   停牌期间没有记录，所以"上一个有成交的交易日"就是除权/复权都该对的那个基准
   （交易所的涨跌幅基准也是这么滚的，不因停牌重置）。
2. **停牌**：当日 `stock_daily` 里没有该 code 的行 = 停牌，**不计入**任何一列。
3. **只统计当日有成交的股票**（`volume > 0`）。
4. **涨停 / 跌停阈值按板块分档**（`>=` / `<=`，单位 %）：

   | 板块 | 判定方式 | 阈值 |
   |---|---|---|
   | ST | `company.name` 含 `ST` | ±4.8 |
   | 创业板 / 科创板 | `sz300` `sz301` `sh688` `sh689` | ±19.8 |
   | 北交所 | `bj` 前缀（含 920xxx / 8xx / 4xx） | ±29.8 |
   | 其余（`sh600/601/603/605`、`sz000/001/002/003`…） | 兜底 | ±9.8 |

   ⚠️ 全库 code 自 2026-11-14 起统一带 `sh/sz/bj` 前缀（含 `stock_daily`），
   北交所一律靠 `bj` 判——不要用"8xx/4xx 号码段"，920xxx 也是北交所。

   ST 判定**优先级最高**（先按板给阈值，再被 ST 覆盖）。
5. `limit_up` / `limit_down` 是 `adv` / `dec` 的**子集**，不再单独加减。

已知偏差（本轮刻意不处理，写清楚免得被当成 bug）
--------------------------------------------------
- **除权日**：`stock_daily` 是**未复权**价，除权当天 `close/prev-1` 会算成大跌，
  而交易所的"涨跌幅"是按除权后基准算的。要精确，得先用 `adj_factor` 把前收还原。
  影响集中在 6~7 月分红季，量级是"几十只"。
- **ST 是当前快照**：`company` 只有现在的名字，历史某天是不是 ST 无从得知。
  另外创业板/科创板的 ST 实际是 ±20%，这里按本轮定下的口径统一按 ±4.8 处理。
- **`total` ≥ `adv+dec+flat`**：新股上市首日（或区间首日）没有前收，
  能算进 `total` 但分不出涨跌平。差额就是"无前收"的家数。
- **区间起点**：传了 `start` 时内部会多取 40 天来算前收，`start` 当天不会因为
  "前收缺失"而少算。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import numpy as np
import pandas as pd

from datahub import DB_PATH

# 算前收要多取的日历天数（长假 + 停牌够用；端午/国庆最长 8~10 天）
_LOOKBACK_DAYS = 40

# 板块阈值：按**带前缀的 code** 判定（2026-11-14 起全库统一 sh/sz/bj 前缀），ST 最后覆盖
_LIMIT_MAIN = 9.8
_LIMIT_GROWTH = 19.8        # 创业板 sz300/sz301、科创板 sh688/sh689
_LIMIT_BJ = 29.8            # 北交所：一律 bj 前缀（含 920xxx / 8xx / 4xx）
_LIMIT_ST = 4.8

_GROWTH_PREFIX = ("sz300", "sz301", "sh688", "sh689")

_COLUMNS = ["adv", "dec", "flat", "limit_up", "limit_down", "total"]


def _conn(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or DB_PATH)
    if not p.exists():
        raise FileNotFoundError(f"没有 datahub 数据库：{p}")
    return sqlite3.connect(str(p), timeout=60)


def _st_codes(path: Path | None = None) -> set[str]:
    """`company.name` 含 ST 的代码集合（带前缀，与 stock_daily 同格式）。"""
    try:
        with _conn(path) as con:
            d = pd.read_sql("SELECT code, name FROM company", con)
    except Exception:  # noqa: BLE001
        return set()
    if d.empty:
        return set()
    name = d["name"].fillna("").astype(str)
    return set(d["code"].astype(str)[name.str.contains("ST")])


def _limit_thresholds(codes: pd.Series, st: set[str]) -> np.ndarray:
    """每股一个涨停阈值（跌停取相反数）。ST 优先于板块。

    全部按**带前缀**的 code 判：北交所看 `bj` 前缀（比"8xx/4xx"的号码段可靠，
    920xxx 也是 bj），创业板/科创板看 `sz300/sz301/sh688/sh689`。
    """
    thr = np.full(len(codes), float(_LIMIT_MAIN))
    thr[codes.str.startswith(_GROWTH_PREFIX).to_numpy()] = _LIMIT_GROWTH
    thr[codes.str.startswith("bj").to_numpy()] = _LIMIT_BJ
    if st:
        thr[codes.isin(st).to_numpy()] = _LIMIT_ST
    return thr


def _load_daily(start: str | None, end: str | None,
                path: Path | None = None) -> pd.DataFrame:
    """读 stock_daily 的 (code, date, close, volume)。

    按 `(code, date)` 排序——正好走主键索引，不用额外排序；
    `start` 会往前多取 `_LOOKBACK_DAYS` 天，供算前收用。
    """
    cond, args = [], []
    if end:
        cond.append("date <= ?")
        args.append(str(end))
    if start:
        lo = (pd.Timestamp(start) - pd.Timedelta(days=_LOOKBACK_DAYS)).date()
        cond.append("date >= ?")
        args.append(str(lo))
    where = (" WHERE " + " AND ".join(cond)) if cond else ""
    sql = (f"SELECT code, date, close, volume FROM stock_daily{where} "
           f"ORDER BY code, date")
    with _conn(path) as con:
        return pd.read_sql(sql, con, params=args)


def breadth(start: str | None = None, end: str | None = None, *,
            path: Path | None = None) -> pd.DataFrame:
    """市场宽度：按日聚合 `stock_daily`。返回 index=date，列 `_COLUMNS`。

    这是**派生量**：不建表、不注册成 Source，每次现算（口径见模块 docstring）。
    """
    df = _load_daily(start, end, path)
    if df.empty:
        return pd.DataFrame(columns=_COLUMNS, index=pd.DatetimeIndex([], name="date"))

    # SQL 已按 (code, date) 排序，shift(1) 就是该股上一个有成交的交易日收盘
    prev = df.groupby("code", sort=False)["close"].shift(1)

    tradable = df["volume"].fillna(0) > 0
    has_prev = prev.notna()
    pct = (df["close"] / prev - 1.0) * 100.0

    keep = tradable
    if start:
        keep = keep & (df["date"] >= str(start))
    if end:
        keep = keep & (df["date"] <= str(end))

    sub = df.loc[keep]
    if sub.empty:
        return pd.DataFrame(columns=_COLUMNS, index=pd.DatetimeIndex([], name="date"))

    p = pct.loc[keep]
    hp = has_prev.loc[keep]
    thr = _limit_thresholds(sub["code"], _st_codes(path))

    flags = pd.DataFrame({
        "adv": (hp & (p > 0)).to_numpy(),
        "dec": (hp & (p < 0)).to_numpy(),
        "flat": (hp & (p == 0)).to_numpy(),
        "limit_up": (hp & (p >= thr)).to_numpy(),
        "limit_down": (hp & (p <= -thr)).to_numpy(),
        "total": np.ones(len(sub), dtype=bool),
        "date": sub["date"].to_numpy(),
    })
    out = flags.groupby("date", sort=True)[_COLUMNS].sum().astype(int)
    out.index = pd.DatetimeIndex(pd.to_datetime(out.index), name="date")
    return cast(pd.DataFrame, out)


# ======================================================================
# 股息率
# ======================================================================
_YIELD_COLUMNS = ["yield_mcap_weighted", "yield_median", "n_stocks"]


def dividend_yield(year: int | None = None, *, path: Path | None = None) -> pd.DataFrame:
    """全市场年度股息率。`index = year`（int），列固定为 `_YIELD_COLUMNS`。

    | 列 | 含义 | 单位 |
    |---|---|---|
    | `yield_mcap_weighted` | 市值加权股息率（**主口径**） | **百分数**（2.5 = 2.5%） |
    | `yield_median` | 当年**分红股**的股息率中位数 | 百分数 |
    | `n_stocks` | 参与计算的**分红股**数（覆盖率的分母见下） | 只 |

    口径
    ----
    1. **分子**：该年（按**除权日**落在这一自然年）每股现金分红**合计**，来自
       `adj_factor.dividend`。注意这与 `dividend_annual` 的"按报告期"口径不同——
       年报分红多数在次年除权，两套口径天然错位一年。
    2. **分母**：该年每只股票**最后一个交易日**的收盘价（停牌取该年最后一条），
       来自 `stock_daily`。
    3. **送转折算**：送转会让"每股"的基数变。第 i 笔分红按
       `cash_i / Π_{j≥i}(1+bonus_j)` 折算到"该年最后一次送转之后"的股本口径，
       与年末收盘价（已是送转后）对齐。
    4. **`yield_mcap_weighted` 的分母是"全市场"**：所有"有年末价 + 有股本"的股票
       都进权重，**不分红的股票分子记 0、分母照算**——这才是"投 1 元到全市场拿多少
       现金"的含义（与指数公司口径一致）。
    5. **`yield_median` 只在分红股内算**：把不分红股（yield=0）掺进来，中位数会被
       直接拉到 0，失去意义。
    6. **`n_stocks` 是分红股数**；覆盖率 = `n_stocks / 当年有年末价的股票数`
       （2025 年约 3785 / 5432）。

    ⚠️ 已知近似（刻意，不修）
    - **权重用"当前股本"**（`shares_history` 只有 2026-11-14 的一份快照）：
      `w = 年末价 × 当前总股本`。对近年误差小（股本年变动通常 <5%），**越往前越不准**
      （送转/增发会累积偏差）。
    - **`stock_daily` 不含退市股**（元信息自己写了）→ 存量幸存者偏差：早年只统计
      "活到今天"的股票，股息率可能偏高。
    - **2016 年不完整**（`stock_daily` 从 2016-11-14 起），只作参考。
    - **当年（2026）是 YTD**：用的是"截至最新交易日"，不是年末价，会随后续数据变。

    ⚠️ 全历史一次调用约 2~4 秒，绝大部分在读 `stock_daily`（千万行）。
    """
    con = _conn(path)
    try:
        # 每年每只股票的最后一个交易日（停牌就取该年最后一条记录）。
        # 不加年份过滤：不带 WHERE 时 SQLite 沿 PK(code,date) 顺序流式分组，10M 行只要
        # 3.4s；加了 `date BETWEEN` 反而会走 idx_date、拿不到 code 顺序，要额外排序 → 更慢。
        con.execute("DROP TABLE IF EXISTS temp._ye")
        con.execute("CREATE TEMP TABLE _ye(code TEXT, y TEXT, d TEXT, PRIMARY KEY(code, y))")
        con.execute("INSERT INTO _ye(code, y, d) "
                    "SELECT code, substr(date, 1, 4), MAX(date) FROM stock_daily "
                    "GROUP BY code, substr(date, 1, 4)")
        px = pd.read_sql(
            "SELECT s.code AS code, CAST(y.y AS INTEGER) AS year, s.close AS close "
            "FROM stock_daily s JOIN _ye y ON s.code = y.code AND s.date = y.d", con)
        ev = pd.read_sql("SELECT code, date, dividend, bonus FROM adj_factor", con)
        sh = pd.read_sql("SELECT code, total_after FROM shares_history", con)
    finally:
        con.close()

    if px.empty or ev.empty or sh.empty:
        raise RuntimeError("股息率缺数据：需要 stock_daily + adj_factor + shares_history")

    # 分红事件：按除权日归年，送转折算到"年末股本口径"。
    # 折算系数 = Π_{j≥i}(1+bonus_j)：按日期**降序**做组内累乘即可（Cython 实现，别用
    # transform + lambda —— 5 万个组的 Python 回调要 20 多秒）。
    ev["year"] = pd.to_datetime(ev["date"]).dt.year
    ev["cash"] = pd.to_numeric(ev["dividend"], errors="coerce").fillna(0.0)
    ev["bonus"] = pd.to_numeric(ev["bonus"], errors="coerce").fillna(0.0)
    ev["one_plus_bonus"] = 1.0 + ev["bonus"]
    ev = ev.sort_values(["code", "year", "date"], ascending=[True, True, False])
    ev["factor"] = ev.groupby(["code", "year"], sort=False)["one_plus_bonus"].cumprod()
    ev["cash_end"] = ev["cash"] / ev["factor"].replace(0.0, np.nan)
    per = cast(pd.DataFrame, ev.groupby(["code", "year"], as_index=False)
                                .agg(cash=("cash_end", "sum")))

    sh["shares"] = pd.to_numeric(sh["total_after"], errors="coerce")
    sh = sh.dropna(subset=["shares"])[["code", "shares"]]

    df = cast(pd.DataFrame, px).merge(cast(pd.DataFrame, sh), on="code", how="inner")
    df = df[df["close"] > 0]
    df["mcap"] = df["close"] * df["shares"]
    df = df.merge(per, on=["code", "year"], how="left")
    df["cash"] = df["cash"].fillna(0.0)
    df["y"] = df["cash"] / df["close"] * 100.0

    rows: list[dict] = []
    for y, g0 in df.groupby("year", sort=True):
        g = cast(pd.DataFrame, g0)
        cash = cast(pd.Series, g["cash"])
        shares = cast(pd.Series, g["shares"])
        mcap = cast(pd.Series, g["mcap"])
        payers = g[g["cash"] > 0]
        w = float(mcap.sum())
        div_amt = float(cash.mul(shares).sum())
        rows.append({
            "year": int(str(y)),
            "yield_mcap_weighted": (div_amt / w * 100.0) if w else np.nan,
            "yield_median": (float(cast(pd.Series, payers["y"]).median())
                             if len(payers) else np.nan),
            "n_stocks": int(len(payers)),
        })
    res = cast(pd.DataFrame, pd.DataFrame(rows).set_index("year")[_YIELD_COLUMNS].round(4))

    if year is not None:
        if int(year) not in res.index:
            raise KeyError(f"没有 {year} 年的股息率；可用年份：{list(res.index)}")
        return res.loc[[int(year)]]
    return res
