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

from datahub.core.schema import DATA_DDL
from datahub.fin import check as C
from datahub.fin.check import find_gaps, is_trading_day
from datahub.fin.sources.guard import write_guard
from datahub.fin.sources import fuyao as F


def _now(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="Asia/Shanghai")


def _db_with(dates, freq: str = "D") -> Path:
    dates = [str(pd.Timestamp(d).date()) for d in dates]   # 兼容 DatetimeIndex
    db = Path(tempfile.gettempdir()) / f"dh_gaps_{abs(hash(tuple(dates))) % 10**8}.sqlite"
    if db.exists():
        db.unlink()
    con = sqlite3.connect(str(db))
    con.executescript(DATA_DDL)                      # find_gaps 要读 metadata
    con.execute("CREATE TABLE t (date TEXT PRIMARY KEY, value REAL)")
    con.executemany("INSERT INTO t VALUES (?,1.0)", [(d,) for d in dates])
    con.execute("INSERT INTO metadata(name, kind, freq, market, start_date, end_date, n_rows) "
                "VALUES ('t','single',?, 'CN', ?, ?, ?)",
                (freq, dates[0], dates[-1], len(dates)))
    con.commit()
    con.close()
    return db


# ---------------------------------------------------------------- find_gaps
def test_find_gaps_catches_hole_in_the_middle():
    """**核心用例**：11-18 有、11-19 没有、11-20 有 —— `MAX(date)` 完全正常。"""
    if True:
        db = _db_with(["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-20"])
        gaps = find_gaps("t", lookback=5, today="2026-11-21 16:00", path=db)["missing"]
        assert gaps == ["2026-11-19"], gaps
        db.unlink()


def test_find_gaps_empty_when_table_complete():
    if True:
        days = C.trading_days("2026-10-01", "2026-11-20")
        db = _db_with(days)
        assert find_gaps("t", lookback=10, today="2026-11-21 16:00", path=db)["missing"] == []
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


def test_guard_and_calendar_have_single_home():
    """判定逻辑**只能有一份**（AGENTS §4）。fuyao 只许调用，不许自己实现。

    2026-11-22 定下的家：日历 → `shared.utils.trading_calendar`；
    `find_gaps` → `fin/check.py`；`write_guard` → `sources/guard.py`。
    """
    from datahub.fin import check as C2
    from datahub.fin.sources import guard as G

    assert hasattr(C2, "find_gaps"), "find_gaps 应该在 fin/check.py"
    assert hasattr(G, "write_guard"), "write_guard 应该在 sources/guard.py"
    assert not hasattr(C2, "write_guard"), "write_guard 不该留在 check.py（那是写库概念）"
    for name in ("trading_days", "expected_periods", "is_trading_day",
                 "last_closed_trading_day"):
        assert getattr(C2, name).__module__ == "shared.utils.trading_calendar", \
            f"{name} 应该来自 shared.utils.trading_calendar"
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
    """`expected_periods` 依赖的 `trading_days` **只能有一份**（现在在 shared）。

    2026-11-22 的 bug：`check.py` 里出现了第二个同名 `trading_days`（返回 `list[str]`），
    把带 `market` 的那个**覆盖**了 → `expected_dates` 按位置传参变成
    `trading_days("CN", "2026-11-10", ...)` → `DateParseError` →
    `check.run()` 里 17 张日频表全报「日历失败」，**逐日缺口检查整体失效**。
    """
    from shared.utils.trading_calendar import expected_periods

    d = expected_periods("D", "CN", "2026-11-10", "2026-11-20")
    assert len(d) >= 7, f"CN 交易日只有 {len(d)} 天"
    assert all(x.weekday() < 5 for x in d), "交易日里出现了周末"
    assert str(d[0].date()) == "2026-11-10" and str(d[-1].date()) == "2026-11-20"


def test_expected_dates_us_market():
    """`xau_usd` 是 US 市场（就是这个 market 在 11-22 报的 `DateParseError`）。"""
    from shared.utils.trading_calendar import expected_periods

    d = expected_periods("D", "US", "2026-11-10", "2026-11-20")
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
    days = days[-10:]                    # 只看最近 10 个交易日，洞（11-11/12）在里面
    holes = [d for d in days if d in ("2026-11-11", "2026-11-12")]
    assert len(holes) == 2, "前提：11-11/11-12 是交易日"
    keep = [d for d in days if d not in holes]
    assert len(holes) == 2

    con = sqlite3.connect(str(db))
    con.executescript(DATA_DDL)
    con.execute("CREATE TABLE t (date TEXT PRIMARY KEY, value REAL)")
    con.executemany("INSERT INTO t VALUES (?,1.0)", [(d,) for d in keep])
    con.execute("INSERT INTO metadata(name, kind, freq, market, start_date, end_date, n_rows) "
                "VALUES ('t','single','D','CN',?,?,?)", (keep[0], keep[-1], len(keep)))
    con.commit()
    con.close()

    gaps = find_gaps("t", lookback=10, today="2026-11-21 16:00", path=db)["missing"]
    assert set(holes) <= set(gaps), f"find_gaps 没报出洞：{gaps}"

    rows = run(path=db, today="2026-11-21 16:00")
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


# ------------------------------------------------- find_gaps 三频率 + stale
def test_find_gaps_monthly_compares_by_period_not_date():
    """月表按**周期**比：数据日期是"披露日"（如 10-15），不是月末 → 不能算缺口。"""
    db = _db_with(["2026-08-31", "2026-09-15", "2026-10-15"], freq="M")
    g = find_gaps("t", lookback=3, today="2026-11-21 16:00", path=db)
    # 8/9/10 月都有数据 → 不该报这三个月的缺口；期望 10 月是最后一个已结束的月
    assert g["missing"] == [], g["missing"]
    assert g["expected_latest"] == "2026-10-31", g
    assert g["stale"] is None, g
    db.unlink()


def test_find_gaps_quarterly_reports_missing_period():
    """季表：缺 2026Q2 → `missing` 里是 2026-06-30。"""
    db = _db_with(["2026-03-31", "2026-09-30"], freq="Q")
    g = find_gaps("t", lookback=3, today="2026-11-21 16:00", path=db)
    assert "2026-06-30" in g["missing"], g["missing"]
    db.unlink()


def test_find_gaps_reports_stale_tail():
    """**尾部**：表停在 11-13、应到 11-20 → `stale` 报出来（**不套 lag_days**）。"""
    days = [d for d in C.trading_days("2026-10-01", "2026-11-20")
            if str(d.date()) <= "2026-11-13"]
    db = _db_with(days)
    g = find_gaps("t", lookback=10, today="2026-11-21 16:00", path=db)
    assert g["stale"] == "2026-11-13", g
    assert g["expected_latest"] == "2026-11-20", g
    # 缺口里包含 11-16 之后的所有交易日
    assert "2026-11-16" in g["missing"] and "2026-11-20" in g["missing"], g["missing"]
    db.unlink()


def test_unacked_filters_by_day_and_by_period():
    from datahub.fin.check import unacked

    # D：按日去掉
    assert unacked(["2026-11-19", "2026-11-20"], {"2026-11-19"}, "D") == ["2026-11-20"]
    # M：按月去掉——ack 了 9 月的任意一天，9 月的缺口就不报了
    assert unacked(["2026-09-30", "2026-10-31"], {"2026-09-15"}, "M") == ["2026-10-31"]
    # Q：按季
    assert unacked(["2026-06-30", "2026-09-30"], {"2026-05-01"}, "Q") == ["2026-09-30"]


def test_run_skips_frozen_tables():
    """**有意冻结**的表不进 `check.run()`（2026-11-22 回归：切分 check 时丢过这段）。

    用户要求"eav / frozen 的处理函数照旧"。丢了过滤的后果是同一张表
    `freshness` 说"有意冻结、跳过"、`check` 说"缺 24 天"——两处结论互相打脸。

    造两张都停更的表：`industrial_yoy`（`SOURCES` 里 `enabled=False`，冻结）
    和 `cpi_yoy`（正常源，真缺数）→ `run()` **只报后者**。
    """
    from datahub.core.schema import DATA_DDL
    from datahub.fin.check import _frozen_tables, run

    assert "industrial_yoy" in _frozen_tables(), "前提：industrial_yoy 是冻结源"
    assert "cpi_yoy" not in _frozen_tables(), "前提：cpi_yoy 不是冻结源"

    db = Path(tempfile.gettempdir()) / "dh_frozen_run.sqlite"
    if db.exists():
        db.unlink()
    con = sqlite3.connect(str(db))
    con.executescript(DATA_DDL)
    for name in ("industrial_yoy", "cpi_yoy"):
        con.execute(f'CREATE TABLE "{name}" (date TEXT PRIMARY KEY, value REAL)')
        con.executemany(f'INSERT INTO "{name}" VALUES (?,1.0)',
                        [("2025-06-30",), ("2025-07-31",), ("2025-08-31",)])
        con.execute("INSERT INTO metadata(name, kind, freq, market, start_date, end_date, n_rows) "
                    "VALUES (?,?, 'M', 'CN', '2025-06-30', '2025-08-31', 3)", (name, "single"))
    con.commit()
    con.close()

    rows = run(path=db)
    names = set(rows["name"]) if len(rows) else set()
    assert "industrial_yoy" not in names, f"冻结表不该被报：{rows.to_string()}"
    assert "cpi_yoy" in names, f"正常源缺数却没报：{rows.to_string()}"
    db.unlink()


# ------------------------------------------------ 非交易日脏数据（2026-11-24）
def test_find_gaps_reports_non_trading_rows():
    """库里存在**非交易日**的行 → `non_trading` 报出来。

    背景：服务器上曾有 5,572 行 `2026-11-22`（周日）的 `stock_daily`，是守卫补上之前
    写进去的。**守卫是"写入前拦"，已入库的脏数据它不清也报不出来**，所以要有这条检查。
    """
    days = [str(d.date()) for d in C.trading_days("2026-11-02", "2026-11-20")]
    # 窗口 = [start, 最后一个已收盘交易日] = ...~11-20；塞一个窗口内的周六（11-14）
    db = _db_with(days + ["2026-11-14"])
    g = find_gaps("t", lookback=20, today="2026-11-21 16:00", path=db)
    assert g["non_trading"] == ["2026-11-14"], g["non_trading"]
    assert g["missing"] == [], "周六那两行不该被算成缺数"
    db.unlink()


def test_non_trading_uses_exchange_calendar_not_weekday():
    """必须走 `is_trading_day`：A 股有**调休开市**（周末上班）。周末判断是错的。"""
    from shared.utils.trading_calendar import is_trading_day

    # 只断言"日历判定的结论被采用"：把日历认为非交易日的日期塞进表，必须被抓
    day = "2026-11-14"                                        # 周六（非交易日）
    assert is_trading_day(day) is False
    days = [str(d.date()) for d in C.trading_days("2026-11-02", "2026-11-20")]
    db = _db_with(days + [day])
    g = find_gaps("t", lookback=20, today="2026-11-21 16:00", path=db)
    assert day in g["non_trading"], g["non_trading"]
    db.unlink()


def test_non_trading_empty_on_real_db():
    """当前库里**A 股**的 D 表不该有非交易日数据（2026-11-22 那批已删）。

    只查 `market=CN`：美股/全球序列（`fed_rate` / `vix` / `world_tension`）本来就有
    周末与假日数据，它们不算脏数据。
    """
    from datahub.core.conn import connect

    con = connect()
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM metadata WHERE freq='D' AND kind!='eav' "
            "AND market IN ('CN','SSE','SZSE')")]
    finally:
        con.close()
    bad = {}
    for n in names:
        try:
            nt = find_gaps(n, today="2026-11-24 16:00")["non_trading"]
        except Exception:  # noqa: BLE001
            continue
        if nt:
            bad[n] = nt
    assert not bad, f"这些 D 表里有非交易日数据：{bad}"


def test_monthly_tables_are_not_checked_for_non_trading():
    """`M/Q/Y`（和 `W`）不查：它们的日期是**周期末日**，落在周末是正常的。

    `gold_cot_net_long` 是周线、日期全在周日；`cpi_yoy` 有月末是周六的。
    实测对 W 开启会一次报 33 天，全是误报。
    """
    db = _db_with(["2026-08-31", "2026-09-30"], freq="M")
    g = find_gaps("t", lookback=3, today="2026-11-24 16:00", path=db)
    assert g["non_trading"] == [], g["non_trading"]
    db.unlink()
