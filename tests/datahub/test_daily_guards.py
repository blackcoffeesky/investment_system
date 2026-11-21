"""日更：先补历史（historical-batch）再取最新（snapshot），守卫只有一份。

2026-11-21 服务器事故：`stock_daily` **独缺 11-19**（11-18、11-20 都在），
而 fuyao 快照只有"当前"——错过一天就永远补不回来。这次做了三件事：

1. `historical_batch()`：批量历史 K 线（个股/指数两个端点，单次 ≤10 码），
   `date_ms` 是 **UTC** 毫秒，已按 Asia/Shanghai 转（不转会整体少一天）。
2. `daily_write_guard()`：日更三条守卫抽成**一份**（日期==今天 / 已收盘 / 库里没有更新），
   `stock_daily_delta` 改为调它——原来这套逻辑写死在函数里，别人抄一份就会漂。
3. `stock_daily_sync()`：日更入口——**先查缺口（含中间的洞）→ 补历史 → 再取当天**。
   ⚠️ 只看 `MAX(date)` 查不出"中间缺一天"：11-19 就是这么漏掉的，所以按
   "最近 20 个交易日里库里缺哪几天"来查。

**绝不把旧数据当新数据写**：snapshot 的 `timestamp` 不是今天（周末/节假日/盘中）
→ 打印原因、返回空表，不落库。
"""

from __future__ import annotations

import pandas as pd

from datahub.fin.sources import fuyao as F


def _now(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="Asia/Shanghai")


def test_guard_blocks_non_trading_day():
    """周末跑：snapshot 的 timestamp 是上一个交易日 → 不能当今天写。"""
    reason = F.daily_write_guard("stock_daily", "2026-11-19", now=_now("2026-11-21 20:00"))
    assert reason and "周末/节假日" in reason, reason


def test_guard_blocks_before_close():
    reason = F.daily_write_guard("stock_daily", "2026-11-21", now=_now("2026-11-21 10:00"))
    assert reason and "未收盘" in reason, reason


def test_guard_blocks_when_day_already_present():
    """库里已有该日（或更新）→ 跳过：重复跑不写重（边界是 `>=`）。"""
    from datahub.core.conn import connect

    con = connect()
    try:
        latest = str(con.execute("SELECT MAX(date) FROM stock_daily").fetchone()[0])[:10]
    finally:
        con.close()
    reason = F.daily_write_guard("stock_daily", latest, now=_now(f"{latest} 20:00"))
    assert reason and "已存在" in reason, reason


def test_historical_batch_parses_utc_ms_and_columns():
    """解析契约：`date_ms`(UTC) → 上海日期；列名映射到库里口径。"""
    body = {
        "code": 0, "message": "success",
        "data": {"interval": "1d", "adjust": "none", "item": [
            {"thscode": "600519.SH", "timestamp": 1795104000000, "item": [
                # 2026-11-19 00:00 +08:00 == 2026-11-18T16:00Z
                {"date_ms": 1795017600000, "volume": 4269153.0, "turnover": 6234e6,
                 "open_price": 1442.0, "high_price": 1465.0,
                 "low_price": 1438.0, "close_price": 1460.21},
            ]},
        ]},
    }
    orig = F.api_get
    F.api_get = lambda *a, **k: body          # type: ignore[assignment]
    try:
        df = F.historical_batch(["600519.SH"], "2026-11-19", "2026-11-19")
    finally:
        F.api_get = orig                      # type: ignore[assignment]
    assert list(df.columns) == ["code", "date", "open", "high", "low", "close",
                                "volume", "turnover"]
    row = df.iloc[0]
    assert row["code"] == "sh600519"
    assert row["date"] == "2026-11-19", f"UTC 转换错了：{row['date']}"
    assert abs(float(row["close"]) - 1460.21) < 1e-9


def test_historical_batch_rejects_more_than_10_codes():
    try:
        F.historical_batch([f"6{i:05d}.SH" for i in range(11)], "2026-11-19", "2026-11-19")
    except ValueError as e:
        assert "10" in str(e)
        return
    raise AssertionError("超过 10 个代码应报错")


def test_trading_days_between_is_monotone_weekdays():
    days = F.trading_days_between("2026-11-16", "2026-11-20")
    assert days == sorted(days) and len(days) >= 1
    assert all(pd.Timestamp(d).weekday() < 5 for d in days)
