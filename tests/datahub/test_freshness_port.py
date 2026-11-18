"""新鲜度：`stale` 与 `overdue` 是两个不同的布尔量。

2026-11-18 从 `causual/causual_core/freshness.py` 搬回数据层。搬的时候**必须**保留
原语义（`_period_end` 的四个分支、只判 D/M/Q/Y），但把原来被 `continue` 吞掉的
"宽限内"那一支**显式化**：

| stale | overdue | 含义 |
|---|---|---|
| False | False | 已是最新 |
| True  | False | 源还没发布（在 `lag_days` 宽限内）→ **不可抗力，不用报警** |
| True  | True  | 宽限已过还没到 → **忘记更新，该报警** |

如果只有 `stale` 一个布尔量，上层会把"源没发"和"忘了跑"混为一谈。
"""

from __future__ import annotations

import datahub.fin.update as U
import pandas as pd

from datahub.fin.check import _period_end, freshness
from shared.adapter import Panel, freshness as adapter_freshness


def test_period_end_semantics_unchanged():
    """搬过来的 `_period_end` 必须与原实现逐条一致。"""
    d = pd.Timestamp("2026-11-18")          # 周三
    assert _period_end("D", d) == d          # 工作日 → 就是今天
    assert _period_end("D", pd.Timestamp("2026-11-15")) == pd.Timestamp("2026-11-13")  # 周日→周五
    assert _period_end("M", d) == pd.Timestamp("2026-10-31")
    assert _period_end("Q", d) == pd.Timestamp("2026-09-30")
    assert _period_end("Y", d) == pd.Timestamp("2025-12-31")
    assert _period_end("W", d) is None       # 有意不判
    assert _period_end("SPARSE", d) is None


def test_every_panel_returns_rows_with_freq():
    """四个 Panel 各跑一次：结果非空、每行都带 `freq`（下游靠它筛月频表）。"""
    for p in Panel:
        rows = adapter_freshness(p, today="2026-11-18")
        assert rows, f"{p.name} 没有返回任何新鲜度行"
        for r in rows:
            assert "freq" in r and r["freq"] in ("D", "M", "Q", "Y"), r
            for k in ("table", "expect", "actual", "lag_days",
                      "stale", "overdue", "days_behind", "text"):
                assert k in r, f"{p.name} 缺字段 {k}"


def test_two_booleans_distinguish_grace_from_overdue():
    """造两种场景：宽限内 → stale&~overdue；宽限外 → stale&overdue。

    用固定 `today`，不依赖真实日期。

    **宽限内**：`gold_etf_holdings`（月频，`lag_days=40`）停在 2026-09-30，
    期望 2026-10-31；11-18 < 10-31+40 → 源还没发，不报。
    **宽限外**：`csi_all`（日频，`lag_days=0`）停在 2026-11-13，期望就是 11-18 → 该报了。
    """
    r = {x["table"]: x for x in freshness(["gold_etf_holdings"], today="2026-11-18")}["gold_etf_holdings"]
    assert r["freq"] == "M" and r["expect"] == "2026-10-31"
    assert r["stale"] is True
    assert r["overdue"] is False, "停在 9-30、宽限 40 天：11-18 还没到 12-10，不该报 overdue"

    d = {x["table"]: x for x in freshness(["csi_all"], today="2026-11-18")}["csi_all"]
    assert d["freq"] == "D" and d["expect"] == "2026-11-18"
    assert d["stale"] is True and d["overdue"] is True, "日频 lag=0，落后就该 overdue"


def test_long_lag_makes_overdue_unreachable():
    """**结构性事实**（不是 bug，但要知道）：`lag_days` ≥ 周期长度时，
    `overdue` 永远为 False——因为 `expect` 会随 `today` 一起往前滚。

    所以 `gold_etf_holdings`（月频 lag=40）只会被标成"不可抗力"，
    **永远不会被判定为"忘记更新"**。想让它报 overdue 只能调小 `metadata.lag_days`
    或改用"距 end_date 多久"这种独立口径（那是另一个决定）。
    """
    for day in ("2026-12-15", "2027-03-20", "2027-08-09"):
        r = {x["table"]: x for x in freshness(["gold_etf_holdings"], today=day)}["gold_etf_holdings"]
        assert r["stale"] is True
        assert r["overdue"] is False, f"{day} 竟然 overdue 了，结构假设不成立"


def test_static_and_eav_are_skipped():
    """`static`（company）/ `eav`（finances）没有日期轴，不该出现在结果里。"""
    rows = freshness(["company", "finances"], today="2026-11-18")
    assert rows == [], f"static/eav 不该进新鲜度结果：{rows}"
