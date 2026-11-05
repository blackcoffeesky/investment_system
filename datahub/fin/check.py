"""完整性检查：用交易日历判断"该有的日期有没有缺"。

    python -m datahub.check              # 列出问题
    python -m datahub.check --ack vix:2026-10-20 --reason "美股休市数据源未更新"

判定逻辑
--------
1. 按 metadata 的 `freq` + `market` 生成"应该存在的日期"（交易日历 + 频率聚合）
2. 与表里实际存在的日期做差
3. 减掉 `missing` 表里**已人工确认**的缺口
4. 剩下的才报出来

`missing` 只有一张表，不论频率；没有缺口时它是空的。
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pandas as pd

from datahub import DB_PATH
import pandas as pd

_MARKET_ALIAS = {"CN": "SSE", "US": "NYSE"}


def trading_days(market: str, start: str, end: str) -> pd.DatetimeIndex:
    """某市场的交易日（不含周末与节假日）。"""
    import pandas_market_calendars as mcal

    name = _MARKET_ALIAS.get(str(market).upper(), str(market))
    cal = mcal.get_calendar(name)
    sched = cal.schedule(start_date=pd.Timestamp(start), end_date=pd.Timestamp(end))
    return pd.DatetimeIndex(sched.index).normalize()


def expected_dates(freq: str, market: str, start: str, end: str) -> pd.DatetimeIndex:
    """按频率 + 市场推出"应该有哪些日期"。

    D：该市场每个交易日；W/M/Q/Y：每个周期最后一个交易日；
    market=NONE 时不挂交易日历，只用自然周期末。
    """
    if str(market).upper() == "NONE" and freq == "D":
        return pd.DatetimeIndex(pd.date_range(start, end, freq="D")).normalize()
    if freq == "D":
        return trading_days(market, start, end)
    if str(market).upper() == "NONE":
        rule = {"W": "W", "M": "ME", "Q": "QE", "Y": "YE"}[freq]
        return pd.DatetimeIndex(pd.date_range(start, end, freq=rule)).normalize()
    days = trading_days(market, start, end)
    if len(days) == 0:
        return days
    s = pd.Series(1, index=days)
    if freq == "W":
        return pd.DatetimeIndex(s.resample("W").apply(lambda x: x.index[-1] if len(x) else pd.NaT).dropna()).normalize()
    per = {"M": "M", "Q": "Q", "Y": "Y"}.get(freq, "M")
    return pd.DatetimeIndex(s.groupby(days.to_period(per)).apply(lambda x: x.index[-1]).dropna()).normalize()
from datahub.core.schema import DATA_DDL


def _conn(path: Path | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(str(path or DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def missing_table(path: Path | None = None) -> pd.DataFrame:
    with _conn(path) as con:
        con.executescript(DATA_DDL)
        return pd.read_sql("SELECT * FROM missing ORDER BY name, date", con)


def ack(name: str, dates: list[str], reason: str = "", path: Path | None = None) -> int:
    """登记"这些缺口是已知的、正常的"。"""
    con = _conn(path)
    con.executescript(DATA_DDL)
    now = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")
    rows = [(name, str(pd.Timestamp(d).date()), reason, now) for d in dates]
    con.executemany("INSERT OR REPLACE INTO missing(name, date, reason, noted_at) VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()
    return len(rows)


def run(path: Path | None = None, recent_days: int = 90) -> pd.DataFrame:
    """返回问题清单。

    `recent_days`：只有最近这么多天内的缺口算"问题"（多半是忘了更新）；
    更早的缺口单独计数——那是历史源数据留下的，不是运维该天天盯的。
    """
    con = _conn(path)
    meta = pd.read_sql("SELECT * FROM metadata", con)
    con.executescript(DATA_DDL)
    acked = pd.read_sql("SELECT name, date FROM missing", con)
    con.close()

    ack_map: dict[str, set[str]] = {}
    for _, r in acked.iterrows():
        ack_map.setdefault(r["name"], set()).add(str(r["date"]))

    import datahub.fin as db

    cutoff = str((pd.Timestamp.today() - pd.Timedelta(days=recent_days)).date())
    rows = []
    for _, m in meta.iterrows():
        name = m["name"]
        try:
            obj = db.read(name, path=path)
        except Exception as e:  # noqa: BLE001
            rows.append({"name": name, "issues": f"读取失败 {type(e).__name__}"})
            continue
        if len(obj) == 0:
            rows.append({"name": name, "issues": "空表"})
            continue

        actual = set(str(d.date()) for d in obj.index)
        start, end = min(actual), max(actual)
        freq = str(m["freq"] or "D")
        market = str(m["market"] or "CN")
        try:
            exp = expected_dates(freq, market, start, end)
        except Exception as e:  # noqa: BLE001
            rows.append({"name": name, "issues": f"日历失败 {type(e).__name__}: {e}"})
            continue

        expected = set(str(d.date()) for d in exp)
        missing = sorted(expected - actual - ack_map.get(name, set()))
        recent = [d for d in missing if d >= cutoff]
        old = len(missing) - len(recent)

        issues = []
        if recent:
            sample = ", ".join(recent[:5])
            more = f" … 共 {len(recent)} 天" if len(recent) > 5 else ""
            issues.append(f"近期缺 {len(recent)} 天（{sample}{more}）")
        if old:
            issues.append(f"历史缺口 {old} 天")
        if isinstance(obj, pd.Series) and obj.isna().any():
            issues.append(f"空值 {int(obj.isna().sum())}")
        if issues:
            rows.append({"name": name, "freq": freq, "market": market,
                         "n": len(obj), "issues": "；".join(issues)})
    return pd.DataFrame(rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ack", help="登记已知缺口：表名:日期[,日期…]")
    ap.add_argument("--reason", default="")
    a = ap.parse_args()
    if a.ack:
        name, _, dates = a.ack.partition(":")
        n = ack(name.strip(), [d.strip() for d in dates.split(",") if d.strip()], a.reason)
        print(f"已登记 {n} 条")
    df = run()
    print(f"\n问题表数 {len(df)}")
    print(df.to_string(index=False) if len(df) else "全部通过")
