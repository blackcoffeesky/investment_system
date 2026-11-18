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

    # 宽限外：同一张表推到截止日之后（11-20 是截止日，11-21 才算 overdue）
    o = {x["table"]: x for x in freshness(["gold_etf_holdings"], today="2026-11-21")}["gold_etf_holdings"]
    assert o["stale"] is True and o["overdue"] is True


def test_daily_tables_never_overdue_under_strict_rule():
    """**连带事实**：`lag_days=0` 的日频表，在严格 `>` 规则下**永远不会 overdue**。

    因为日频的 `expect` 就是今天，`today > expect + 0` 恒假。
    这不是 bug——用户定的 `>` 正是为了"截止日当天数据可能收盘后才入库"，而日频的
    截止日就是今天。日频表靠 `stale` 单独报（它们落后一天就已经显眼了）。
    """
    r = {x["table"]: x for x in freshness(["csi_all"], today="2026-11-18")}["csi_all"]
    assert r["freq"] == "D" and r["expect"] == "2026-11-18"
    assert r["stale"] is True
    assert r["overdue"] is False, "lag=0 的日频表在严格 > 下不该 overdue"


def test_overdue_is_reachable_when_lag_is_smaller_than_period():
    """**正向断言**：`lag_days` 小于周期长度时，`overdue` 在宽限后可达。

    历史：`gold_etf_holdings` 原先 `lag_days=40`（月频），`expect` 随 `today` 往前滚，
    `expect + 40` 永远追不上 ⇒ `overdue` **永远为 False**，缺月也报不出来。
    2026-11-18 用户把 lag 改成 **20** 修掉了这一点（数值不变，只影响可见日与报警日）。

    这条测试就是那个修复的回归：截止日 `expect + 20 = 2026-11-20`，
    当天**不报**（数据可能收盘后才进库），次日开始报。
    """
    cases = {"2026-11-18": False, "2026-11-19": False,
             "2026-11-20": False,   # 边界日：发布截止日当天不算 overdue
             "2026-11-21": True}
    for day, want in cases.items():
        r = {x["table"]: x for x in freshness(["gold_etf_holdings"], today=day)}["gold_etf_holdings"]
        assert r["stale"] is True, f"{day}: 停在 2026-09-30 应判 stale"
        assert r["overdue"] is want, f"{day}: overdue 应为 {want}，实际 {r['overdue']}"
