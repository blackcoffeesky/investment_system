"""日更守卫 + 缺口检查（**源无关**，住在 `fin/check.py`）。

2026-11-21 的事故链：
- `stock_daily` **独缺 11-19**（11-18、11-20 都在）→ `MAX(date)` 看不出来；
- 周六跑日更，fuyao 快照仍回 `timestamp=今天`、值是周五收盘 → 原守卫只判"日期==今天"，
  把 5,572 行 11-20 的数据标成 11-21 写进库（已删除）。

所以这两件事抽成通用能力（任何源都该有），测试钉四种情形：
**中间的洞 / 重复写 / 周末 / 盘中**。
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pandas as pd

from datahub.fin import check as C
from datahub.fin.check import find_gaps, is_trading_day, write_guard
from datahub.fin.sources import fuyao as F


def _now(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="Asia/Shanghai")


def _db_with(dates) -> Path:
    dates = [str(pd.Timestamp(d).date()) for d in dates]   # 兼容 DatetimeIndex
    db = Path(tempfile.gettempdir()) / f"dh_gaps_{abs(hash(tuple(dates))) % 10**8}.sqlite"
    if db.exists():
        db.unlink()
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE t (date TEXT PRIMARY KEY, value REAL)")
    con.executemany("INSERT INTO t VALUES (?,1.0)", [(d,) for d in dates])
    con.commit()
    con.close()
    return db


def _pin_today(monkeypatch_end="2026-11-20"):
    """把"最后一个已收盘交易日"钉死，测试不依赖真实日期。"""
    orig = C.last_closed_trading_day
    C.last_closed_trading_day = lambda *a, **k: monkeypatch_end   # type: ignore[assignment]
    return orig


# ---------------------------------------------------------------- find_gaps
def test_find_gaps_catches_hole_in_the_middle():
    """**核心用例**：11-18 有、11-19 没有、11-20 有 —— `MAX(date)` 完全正常。"""
    orig = _pin_today("2026-11-20")
    try:
        db = _db_with(["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-20"])
        gaps = find_gaps("t", lookback=5, path=db)
        assert gaps == ["2026-11-19"], gaps
    finally:
        C.last_closed_trading_day = orig     # type: ignore[assignment]
        db.unlink()


def test_find_gaps_empty_when_table_complete():
    orig = _pin_today("2026-11-20")
    try:
        days = C.trading_days("2026-11-02", "2026-11-20")
        db = _db_with(days)
        assert find_gaps("t", lookback=10, path=db) == []
    finally:
        C.last_closed_trading_day = orig     # type: ignore[assignment]
        db.unlink()


# --------------------------------------------------------------- write_guard
def test_write_guard_blocks_weekend():
    """周六：快照仍回 `timestamp=今天`，但值是周五收盘 → 必须拦。"""
    ok, why = write_guard("stock_daily", "2026-11-21", now=_now("2026-11-21 23:00"))
    assert ok is False and "不是交易日" in why, why


def test_write_guard_blocks_intraday():
    """盘中（未到 15:30）：写进去是半天的量。"""
    ok, why = write_guard("stock_daily", "2026-11-20", now=_now("2026-11-20 10:00"))
    assert ok is False and "未收盘" in why, why


def test_write_guard_blocks_when_day_already_present():
    """重复写：边界 `>=` —— 库里已有该日或更新就跳过。"""
    from datahub.core.conn import connect

    con = connect()
    try:
        latest = str(con.execute("SELECT MAX(date) FROM stock_daily").fetchone()[0])[:10]
    finally:
        con.close()
    ok, why = write_guard("stock_daily", latest, now=_now(f"{latest} 20:00"))
    assert ok is False and "已存在" in why, why


def test_write_guard_allows_a_fresh_trading_day():
    """今日收盘后、库里没有这天 → 放行（返回 (True, "")）。"""
    ok, why = write_guard("no_such_table_for_test", "2026-11-20", now=_now("2026-11-20 20:00"))
    assert ok is True and why == "", (ok, why)


def test_fuyao_no_longer_implements_guard_logic():
    """守卫判定**只能有一份**（AGENTS §4）。fuyao 只许调用，不许自己实现。"""
    for name in ("write_guard", "find_gaps", "is_trading_day"):
        assert hasattr(C, name), f"{name} 应该在 fin/check.py"
    for name in ("daily_write_guard", "_is_trading_day", "trading_days_between"):
        assert not hasattr(F, name), f"fuyao.py 里不该再有自己的 {name}（会漂）"


def test_historical_batch_parses_utc_ms_and_columns():
    """解析契约：`date_ms`(UTC) → 上海日期；列名映射到库里口径。"""
    body = {"code": 0, "message": "success",
            "data": {"interval": "1d", "adjust": "none", "item": [
                {"thscode": "600519.SH", "timestamp": 1795104000000, "item": [
                    {"date_ms": 1795017600000, "volume": 4269153.0, "turnover": 6234e6,
                     "open_price": 1442.0, "high_price": 1465.0,
                     "low_price": 1438.0, "close_price": 1460.21},
                ]}]}}
    orig = F.api_get
    F.api_get = lambda *a, **k: body          # type: ignore[assignment]
    try:
        df = F.historical_batch(["600519.SH"], "2026-11-19", "2026-11-19")
    finally:
        F.api_get = orig                      # type: ignore[assignment]
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


def test_is_trading_day_saturday_is_false():
    assert is_trading_day("2026-11-21") is False


# --------------------------------------------------- 交易日历（2026-11-22 修复）
def test_expected_dates_cn_has_no_weekends():
    """`expected_dates` 依赖的 `trading_days` **只能有一份**。

    2026-11-22 的 bug：`check.py` 里出现了第二个同名 `trading_days`（返回 `list[str]`），
    把带 `market` 的那个**覆盖**了 → `expected_dates` 按位置传参变成
    `trading_days("CN", "2026-11-10", ...)` → `DateParseError` →
    `check.run()` 里 17 张日频表全报「日历失败」，**逐日缺口检查整体失效**。
    """
    from datahub.fin.check import expected_dates

    d = expected_dates("D", "CN", "2026-11-10", "2026-11-20")
    assert len(d) >= 7, f"CN 交易日只有 {len(d)} 天"
    assert all(x.weekday() < 5 for x in d), "交易日里出现了周末"
    assert str(d[0].date()) == "2026-11-10" and str(d[-1].date()) == "2026-11-20"


def test_expected_dates_us_market():
    """`xau_usd` 是 US 市场（就是这个 market 在 11-22 报的 `DateParseError`）。"""
    from datahub.fin.check import expected_dates

    d = expected_dates("D", "US", "2026-11-10", "2026-11-20")
    assert len(d) >= 7, f"US 交易日只有 {len(d)} 天"
    assert all(x.weekday() < 5 for x in d)


def test_find_gaps_agrees_with_check_run():
    """**一致性**：同一张表、同一个洞，`find_gaps` 和 `check.run` 要报同一批日期。"""
    from datahub.core.schema import DATA_DDL
    from datahub.fin.check import find_gaps, run

    db = Path(tempfile.gettempdir()) / "dh_gap_agree.sqlite"
    if db.exists():
        db.unlink()
    days = [str(d.date()) for d in C.trading_days("2026-11-02", "2026-11-20")]
    holes = [d for d in days if d in ("2026-11-11", "2026-11-12")]
    assert len(holes) == 2, "前提：11-11/11-12 是交易日"
    keep = [d for d in days if d not in holes]

    con = sqlite3.connect(str(db))
    con.executescript(DATA_DDL)
    con.execute("CREATE TABLE t (date TEXT PRIMARY KEY, value REAL)")
    con.executemany("INSERT INTO t VALUES (?,1.0)", [(d,) for d in keep])
    con.execute("INSERT INTO metadata(name, kind, freq, market, start_date, end_date, n_rows) "
                "VALUES ('t','single','D','CN',?,?,?)", (keep[0], keep[-1], len(keep)))
    con.commit()
    con.close()

    gaps = find_gaps("t", lookback=20, path=db)
    assert set(holes) <= set(gaps), f"find_gaps 没报出洞：{gaps}"

    rows = run(path=db)
    row = rows[rows["name"] == "t"]
    assert len(row) == 1, f"check.run 没报这张表：{rows.to_string()}"
    issues = str(row.iloc[0]["issues"])
    assert "缺" in issues, issues
    for h in holes:                      # 两个洞都要在 check.run 的文案里
        assert h in issues, f"{h} 没进 check.run 的文案：{issues}"
    db.unlink()


def test_check_run_marks_calendar_failure_separately():
    """**日历失败 ≠ 数据有问题**：前者是"检查没跑成"，要单独标 `check_failed`。"""
    from datahub.fin.check import run

    rows = run()
    assert "status" in rows.columns, "check.run 必须带 status 列"
    assert set(rows["status"].unique()) <= {"data", "check_failed"}
    failed = rows[rows["status"] == "check_failed"]
    assert len(failed) == 0, f"日历仍然在失败：{failed[['name', 'issues']].to_string()}"
