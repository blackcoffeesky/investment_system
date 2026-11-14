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
   | 创业板 300/301、科创板 688/689 | 代码前缀 | ±19.8 |
   | 北交所 8xx / 4xx | 代码前缀 | ±29.8 |
   | 其余（600/601/603/605/000/001/002/003…） | 兜底 | ±9.8 |

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

import numpy as np
import pandas as pd

from datahub import DB_PATH

# 算前收要多取的日历天数（长假 + 停牌够用；端午/国庆最长 8~10 天）
_LOOKBACK_DAYS = 40

# 板块阈值：按代码前缀判定，ST 最后覆盖
_LIMIT_MAIN = 9.8
_LIMIT_GROWTH = 19.8        # 创业板 300/301、科创板 688/689
_LIMIT_BJ = 29.8            # 北交所 8xx / 4xx
_LIMIT_ST = 4.8

_GROWTH_PREFIX = ("300", "301", "688", "689")
_BJ_PREFIX = ("8", "4")

_COLUMNS = ["adv", "dec", "flat", "limit_up", "limit_down", "total"]


def _conn(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or DB_PATH)
    if not p.exists():
        raise FileNotFoundError(f"没有 datahub 数据库：{p}")
    return sqlite3.connect(str(p), timeout=60)


def _bare_code(codes: pd.Series) -> pd.Series:
    """`sh600519` / `600519` → `600519`（stock_daily 是裸 6 位，company 带前缀）。"""
    return codes.astype(str).str[-6:]


def _st_codes(path: Path | None = None) -> set[str]:
    """`company.name` 含 ST 的代码集合（裸 6 位）。表没建/没数据就当没有 ST。"""
    try:
        with _conn(path) as con:
            d = pd.read_sql("SELECT code, name FROM company", con)
    except Exception:  # noqa: BLE001
        return set()
    if d.empty:
        return set()
    name = d["name"].fillna("").astype(str)
    return set(_bare_code(d["code"])[name.str.contains("ST")])


def _limit_thresholds(bare: pd.Series, st: set[str]) -> np.ndarray:
    """每股一个涨停阈值（跌停取相反数）。ST 优先于板块。"""
    thr = np.full(len(bare), float(_LIMIT_MAIN))
    arr = bare.to_numpy()
    growth = bare.str.startswith(_GROWTH_PREFIX).to_numpy()
    bj = bare.str.startswith(_BJ_PREFIX).to_numpy()
    thr[growth] = _LIMIT_GROWTH
    thr[bj] = _LIMIT_BJ
    if st:
        thr[bare.isin(st).to_numpy()] = _LIMIT_ST
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
    thr = _limit_thresholds(_bare_code(sub["code"]), _st_codes(path))

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
    return out
