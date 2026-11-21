"""新鲜度：`stale` 与 `overdue` 是两个不同的布尔量。

2026-11-18 从 `causual/causual_core/freshness.py` 搬回数据层。搬的时候**必须**保留
原语义（`_period_end` 的四个分支、只判 D/M/Q/Y），但把原来被 `continue` 吞掉的
"宽限内"那一支**显式化**：

| stale | overdue | 含义 |
|---|---|---|
| False | False | 已是最新 |
| True  | False | 源还没发布（在 `lag_days` 宽限内）→ **不可抗力，不用报警** |
| True  | True  | 宽限已过还没到 → **忘记更新，该报警** |

⚠️ 这组测试**自建临时库**，不依赖线上库的 `metadata`——2026-11-21 在服务器上发现：
本机把 `lag_days` 调成 20、服务器还是 40，同一份测试两边结果不同。
**测试要钉的是逻辑，不是当前数据状态。**
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pandas as pd

from datahub.core.schema import DATA_DDL
from datahub.fin.check import _period_end, freshness
from shared.adapter import Panel, freshness as adapter_freshness


def _mkdb(rows: list[tuple[str, str, int, str]]) -> Path:
    """建一个只有 `metadata` 的临时库：rows = [(name, freq, lag_days, end_date)]。"""
    db = Path(tempfile.gettempdir()) / f"dh_fresh_{abs(hash(tuple(rows))) % 10**8}.sqlite"
    if db.exists():
        db.unlink()
    con = sqlite3.connect(str(db))
    con.executescript(DATA_DDL)
    for name, freq, lag, end in rows:
        con.execute("INSERT INTO metadata(name, kind, freq, lag_days, end_date) "
                    "VALUES (?,?,?,?,?)", (name, "single", freq, lag, end))
    con.commit()
    con.close()
    return db


def test_period_end_semantics_unchanged():
    """搬过来的 `_period_end` 必须与原实现逐条一致。"""
    d = pd.Timestamp("2026-11-18")          # 周三
    assert _period_end("D", d) == d
    assert _period_end("D", pd.Timestamp("2026-11-15")) == pd.Timestamp("2026-11-13")  # 周日→周五
    assert _period_end("M", d) == pd.Timestamp("2026-10-31")
    assert _period_end("Q", d) == pd.Timestamp("2026-09-30")
    assert _period_end("Y", d) == pd.Timestamp("2025-12-31")
    assert _period_end("W", d) is None       # 有意不判
    assert _period_end("SPARSE", d) is None


def test_grace_then_overdue_boundary():
    """月频 lag=20、停在 09-30：截止日 = 10-31+20 = 11-20，**当天不报、次日才报**。"""
    db = _mkdb([("m_tab", "M", 20, "2026-09-30")])
    want = {"2026-11-18": False, "2026-11-19": False,
            "2026-11-20": False,   # 发布截止日当天：数据可能收盘后才进库
            "2026-11-21": True}
    for day, flag in want.items():
        r = {x["table"]: x for x in freshness(["m_tab"], today=day, path=db)}["m_tab"]
        assert r["freq"] == "M" and r["expect"] == "2026-10-31", r
        assert r["stale"] is True, f"{day}: 停在 09-30 应判 stale"
        assert r["overdue"] is flag, f"{day}: overdue 应为 {flag}，实际 {r['overdue']}"
    db.unlink()


def test_daily_tables_never_overdue_under_strict_rule():
    """连带事实：`lag=0` 的日频表在严格 `>` 下**永远不会 overdue**。

    日频的 `expect` 就是今天，`today > expect + 0` 恒假。这与"截止日当天数据可能
    收盘后才入库"的理由一致；日频表靠 `stale` 单独报。
    """
    db = _mkdb([("d_tab", "D", 0, "2026-11-13")])
    r = {x["table"]: x for x in freshness(["d_tab"], today="2026-11-18", path=db)}["d_tab"]
    assert r["freq"] == "D" and r["expect"] == "2026-11-18"
    assert r["stale"] is True and r["overdue"] is False
    db.unlink()


def test_every_panel_returns_rows_with_freq():
    """四个 Panel 各跑一次：结果非空、每行都带 `freq`（下游靠它筛月频表）。"""
    for p in Panel:
        rows = adapter_freshness(p, today="2026-11-18")
        assert rows, f"{p.name} 没有返回任何新鲜度行"
        for r in rows:
            assert r["freq"] in ("D", "M", "Q", "Y"), r
            for k in ("table", "expect", "actual", "lag_days",
                      "stale", "overdue", "days_behind", "text"):
                assert k in r, f"{p.name} 缺字段 {k}"


def test_frozen_sources_are_skipped():
    """**有意冻结**的表（`SOURCES` 里 `enabled=False`）不进新鲜度。"""
    from datahub.fin.check import _frozen_tables

    frozen = _frozen_tables()
    assert {"geopolitic_risk", "industrial_yoy", "exports_yoy"} <= frozen
    rows = {r["table"] for r in freshness(["geopolitic_risk", "industrial_yoy",
                                           "exports_yoy"], today="2026-11-21")}
    assert rows == set(), f"冻结表不该进结果：{rows}"


def test_manual_tables_are_still_monitored():
    """**手工导入表不在 SOURCES 里，绝不能被"跳过冻结表"顺手跳掉**。"""
    from datahub.fin.sources import SOURCES

    assert not any("gold_etf_holdings" in s.table.split("|") for s in SOURCES)
    rows = {x["table"]: x for x in freshness(["gold_etf_holdings"], today="2027-01-01")}
    assert "gold_etf_holdings" in rows, "手工表必须继续被监控"
    assert rows["gold_etf_holdings"]["stale"] is True
