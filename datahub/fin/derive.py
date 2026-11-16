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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, cast

import numpy as np
import pandas as pd

from datahub import DB_PATH

# 算前收要多取的日历天数。
# **不是"长假够用"那种 40 天**：停牌可能长达数月（重组/立案），窗口起点那天如果某只
# 股票上一次成交在 40 天之前，全量能算出前收、窗口版算不出 → adv/dec 差几只。
# 2026-11-16 实测：40 天时 `breadth(2025)` 与 `breadth().loc["2025"]` 有 0.82% 的格子不同；
# 400 天（≈1 年 + 余量）后归零。再长的停牌属于极少数，接受为残差。
_LOOKBACK_DAYS = 400

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

    `start` / `end` 是**结果区间**（闭区间）；内部按 `_LOOKBACK_DAYS` 往前多取
    算前收，**算完再切回** `[start, end]`。所以
    `breadth(start, end)` 恒等于 `breadth()` 再 `.loc[start:end]`（窗口不变量）。

    ⚠️ **`_LOOKBACK_DAYS = 400` 不是为了分位数，是为了停牌**：
    本函数没有任何滚动统计（不分位、不均值），多取的那段**只用来算"前一个交易日
    收盘"**。而停牌可能长达数月——停牌超过 40 天的股票，在窗口起点处拿不到前收
    （`has_prev=False`），于是它算进 `total` 却进不了 `adv/dec`；全量版能算出前收，
    两边就对不上。实测 40 天时 `breadth(2025)` 与全量切片有 **0.82%** 的格子不同，
    400 天才归零。**别把它"优化"成 40 天，也别以为它和滚动窗口有关。**

    这是**派生量**：不建表、不注册成 Source，每次现算（口径见模块 docstring）。
    消费方从 `DataPort.series_map(["breadth"], start=..., end=...)` 取，
    形态和普通表完全一样。
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


def dividend_yield(year: int | None = None, *, start: str | None = None,
                   end: str | None = None,
                   path: Path | None = None) -> pd.DataFrame:
    """全市场年度股息率。`index = year`（int），列固定为 `_YIELD_COLUMNS`。

    | 列 | 含义 | 单位 |
    |---|---|---|
    | `yield_mcap_weighted` | 市值加权股息率（**主口径**） | **百分数**（2.5 = 2.5%） |
    | `yield_median` | 当年**分红股**的股息率中位数 | 百分数 |
    | `n_stocks` | 参与计算的**分红股**数（覆盖率的分母见下） | 只 |

    口径
    ----
    1. **分子**：该年（按**除权日**落在这一自然年）每股现金分红**合计**，来自
       `adj_factor.dividend`。注意这与**已删除的** `dividend_annual`（按报告期口径）不同——
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

    # `start` / `end` 是**结果区间**：年度口径每年独立（分红按除权日归年、股本用当年年末价），
    # 不存在跨年依赖，所以**不需要 warmup**，切年份即可。
    if start is not None:
        res = res[res.index >= pd.Timestamp(start).year]
    if end is not None:
        res = res[res.index <= pd.Timestamp(end).year]

    if year is not None:
        if int(year) not in res.index:
            raise KeyError(f"没有 {year} 年的股息率；可用年份：{list(res.index)}")
        return res.loc[[int(year)]]
    return res


# ======================================================================
# ETF 分组份额
# ======================================================================
#: 宽基 ETF 分组（**裸 6 位码**）。分组是**市场定义**，归数据层——消费方不该各自维护
#: 一份（2026-11-14 的事故就是两侧代码归一规则不一致，四个派生列整体消失）。
_ETF_GROUPS: dict[str, tuple[str, ...]] = {
    "sse50": ("510050", "510710", "510100", "510190"),
    "csi300": ("510300", "510310", "510330"),
    "csi1000": ("512100", "560010", "510580", "510510"),
}


def bare_code(name: object) -> str:
    """`sh510300` / `c510300` / `510300.SH` → `510300`。

    数据层 2026-11 起给代码加了市场前缀，归一逻辑**只在这里有一份**。
    """
    digits = "".join(ch for ch in str(name) if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else str(name)


def etf_group_shares(start: str | None = None, end: str | None = None, *,
                     path: Path | None = None,
                     warmup_obs: int | None = None) -> pd.DataFrame:
    """宽基 ETF **分组份额**（组内求和），`index=date`，列 = 组名 `sse50/csi300/csi1000`。

    **原生观测日有值、其余日期不出现（不 ffill）**——消费方是在观测日上算
    `pct_change`，先 ffill 会让非观测日的变化变成 0，口径就变了（实测均值差 −72%、
    66% 的日期变 0）。

    `start` 会按 `warmup_obs`（默认取注册表里的 2）往前多取几个**观测**，
    保证窗口内第一条也有 pct_change 的基准；算完由调用方切回 `[start, end]`。
    """
    obs = DERIVED["etf_group_shares"].warmup_obs if warmup_obs is None else int(warmup_obs)
    con = _conn(path)
    try:
        lo = None
        if start and obs > 0:
            rows = con.execute(
                "SELECT DISTINCT date FROM etf_shares WHERE date <= ? "
                "ORDER BY date DESC LIMIT ?", (str(start), obs)).fetchall()
            if rows:
                lo = min(str(r[0]) for r in rows)
        cond, args = [], []
        if lo:
            cond.append("date >= ?")
            args.append(lo)
        if end:
            cond.append("date <= ?")
            args.append(str(end))
        where = (" WHERE " + " AND ".join(cond)) if cond else ""
        df = pd.read_sql(f"SELECT code, date, shares FROM etf_shares{where}",
                         con, params=args)
    finally:
        con.close()

    cols = list(_ETF_GROUPS)
    if df.empty:
        return pd.DataFrame(columns=cols,
                            index=pd.DatetimeIndex([], name="date"), dtype=float)

    group_of = {c: g for g, codes in _ETF_GROUPS.items() for c in codes}
    # `all_dates` 必须取**全部 ETF**（含不在分组里的代码）的日期并集——旧实现的 `wide`
    # 就是全 24 只的透视表，`sum(axis=1)` 在"某组当天没有观测"时落 0.0。
    all_dates = pd.DatetimeIndex(pd.to_datetime(df["date"].unique())).sort_values()
    df["g"] = df["code"].map(lambda c: group_of.get(bare_code(c)))
    df = df.dropna(subset=["g"])
    if df.empty:
        return pd.DataFrame(columns=cols,
                            index=pd.DatetimeIndex([], name="date"), dtype=float)

    wide = df.pivot_table(index="date", columns="g", values="shares", aggfunc="sum")
    wide.index = pd.DatetimeIndex(pd.to_datetime(wide.index), name="date")
    # ⚠️ **刻意复刻旧实现的 bug**：旧的 `wide[cols].sum(axis=1)` 在某组当天
    # "一个观测都没有"时返回 **0.0**（pandas 对全 NaN 求和的默认行为），
    # 于是"没有数据"被写成"份额为 0"（2016 年有 162 个这样的格子）。
    # 本轮是重构、不是修语义，所以先按旧行为填 0 保住逐位一致；
    # 要修（改成 NaN）应当单独一轮，因为它会改变 ETF_DISPERSION 等派生列。
    wide = wide.reindex(index=all_dates)
    for g in cols:
        if g not in wide.columns:
            wide[g] = np.nan
    out = cast(pd.DataFrame, wide[cols].astype(float).fillna(0.0).sort_index())
    out.index.name = "date"
    return out


# ======================================================================
# 派生量注册表
# ======================================================================
@dataclass(frozen=True)
class Derived:
    """一个派生量：**名字 → 算它的函数 + 依赖 + 窗口 + 列**。

    为什么不放进 `metadata`：派生量不是库里的表（不落表、无 sync_log），
    硬塞进 metadata 会让 `check` / `/data` 页分不清"缺表"和"该现算"。
    注册表只给 `series_map` 认名字用。

    `warmup_*`：算 `[start, end]` 时**必须往前多取**多少（单位三选一）。
    - `warmup_days`：日频派生（按日历天）
    - `warmup_years`：年度派生
    - `warmup_obs`：按"上一次观测"（低频序列做 pct_change/diff 时）
    **不变量**：`func(start, end)` 恒等于 `func()` 再 `.loc[start:end]`。
    """

    func: Callable
    depends: tuple[str, ...]
    period: str                 # D / W / M / Q / Y
    columns: tuple[str, ...]
    doc: str
    warmup_days: int = 0
    warmup_years: int = 0
    warmup_obs: int = 0
    depends_note: str = ""      # 依赖的**语义边界**：换源/补数会让结果变的地方


#: 派生量注册表：`series_map` 见到这些名字就调 `func(start, end)`，不当表读。
DERIVED: dict[str, Derived] = {
    "breadth": Derived(
        func=breadth,
        depends=("stock_daily",),
        period="D",
        columns=tuple(_COLUMNS),
        doc="全市场涨跌家数（按板块分档判涨跌停）",
        # 只为了算"前收"：与 `_LOOKBACK_DAYS` 同一个数（改一处改两行，别让它漂）
        warmup_days=_LOOKBACK_DAYS,
        depends_note=(
            "依赖 `stock_daily` 的**未复权收盘 + 当日是否有成交**。"
            "三个语义边界：① 退市股不在表里 → 幸存者偏差；"
            "② 未复权 → 除权日会被算成大跌（分红季几十只）；"
            "③ `company.name` 是**当前**快照 → 历史某天是否 ST 无从得知。"
            "这三点任一变化，adv/dec/limit_up 的口径就变。"
        ),
    ),
    "dividend_yield": Derived(
        func=dividend_yield,
        depends=("adj_factor", "stock_daily", "shares_history"),
        period="Y",
        columns=tuple(_YIELD_COLUMNS),
        doc="年度股息率（市值加权 + 中位数，用当前股本近似）",
        # 年度口径**每年独立**（分红按除权日归年、分母用当年年末价），没有跨年依赖
        warmup_years=0,
        depends_note=(
            "依赖 `adj_factor` 的**除权日**口径（不是报告期）＋ `shares_history` 的"
            "**当前股本快照**（不是当年股本 → 越往前越不准）＋ `stock_daily` 的年末价。"
            "股东回报口径/股本源一变，这个数就不可比。"
        ),
    ),
    "etf_group_shares": Derived(
        func=etf_group_shares,
        depends=("etf_shares",),
        period="D",
        columns=tuple(_ETF_GROUPS),
        doc="宽基 ETF 分组份额（按组求和，原生观测日有值、不 ffill）",
        warmup_obs=2,          # pct_change 要上一次观测
        depends_note=(
            "ETF 码表/分组定义（`_ETF_GROUPS`）变了，组内求和就变——"
            "分组知识归数据层，消费方不要再自己维护一份。"
            "另外 `etf_shares` 只覆盖上交所的这 24 只，深市宽基（sz159xxx）不在里面。"
        ),
    ),
}

