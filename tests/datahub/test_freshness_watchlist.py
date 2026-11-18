"""新鲜度**不能是"假装在工作"**：表名要真、频率要判得了、覆盖不能是空的。

背景（2026-11-14 发现）
---------------------
causual 手写的 `WATCH_MACRO` 写着 `cn_cpi / cn_ppi / cn_m2 / cn_exports`，
而数据层的表名早就改成 `cpi_yoy / ppi_yoy / m1_yoy / m2_yoy`：

    if t not in idx.index:
        continue          ← 8 个名字只命中 3 个，剩下 5 个静默跳过

后果**不是报错，是"没有过期表"这个结论永远成立**。你以为是有人在看，其实没有。
那份清单后来还漂过一次（`MONTH_TABLES` 也和 `_month_of()` 的副本对不上）。

2026-11-18：**手写清单彻底删除**。`shared.adapter.freshness(panel)` 直接按
`tables_of(panel)` 逐表算，`freq` / `lag_days` / `end_date` 全由数据层给，
causual 只在装配点把它传给 `pipeline.run(freshness=...)`。

所以这条测试改盯**新口径**（旧口径的三条断言跟着清单一起作废了）：

1. 每个面板的 freshness 行里，表名都在 `DataPort.tables()` 里（不可能出现幽灵表）
2. 每行 `freq` 都是判得了过期的 `D/M/Q/Y`（`W` / `SPARSE` 算不出"该有的期"）
3. 覆盖**不是空的**（空 = 等于没监控）
"""

from __future__ import annotations

import pandas as pd

PANELS = ("CAUSUAL_ASHARE", "CAUSUAL_GOLD")

#: 判得了"该有的期"的频率；`W` 周频与 `SPARSE` 不定期不在其中（有意不判）
CHECKABLE_FREQS = frozenset({"D", "M", "Q", "Y"})


def _rows(panel_name: str, today: str | None = None) -> list[dict]:
    from shared.adapter import Panel, freshness

    return freshness(getattr(Panel, panel_name), today=today)


def test_freshness_tables_exist_in_datahub():
    """新鲜度里的每张表都得是 `DataPort.tables()` 里的真表。

    表名对不上时，数据层**不会**报错——它只是拿不到 end_date，然后什么都不报。
    这条红说明改名 / 删表没同步到面板的表清单。
    """
    from datahub.port import get_port

    known = set(get_port().tables())
    assert known, "DataPort.tables() 是空的——数据库没建好？"

    bad: dict[str, list[str]] = {}
    for name in PANELS:
        missing = [r["table"] for r in _rows(name) if r["table"] not in known]
        if missing:
            bad[name] = missing
    assert not bad, f"新鲜度里有数据层不存在的表（改名/删表了？）：{bad}"


def test_freshness_freqs_are_checkable():
    """频率必须是 `D/M/Q/Y`——`W` / `SPARSE` 算不出"该有的期"，等于不判。"""
    bad: dict[str, dict[str, str]] = {}
    for name in PANELS:
        odd = {r["table"]: r["freq"] for r in _rows(name)
               if r["freq"] not in CHECKABLE_FREQS}
        if odd:
            bad[name] = odd
    assert not bad, f"这些表的 freq 判不了过期，会被静默跳过：{bad}"


def test_freshness_covers_every_panel():
    """两个面板都必须真的吐出新鲜度行，且行里字段齐全。

    "空清单"和"全部 fresh"看起来一样，但含义完全不同——空清单意味着**没有任何表
    被监控**，所以这里连长度一起断言。
    """
    need = {"table", "freq", "expect", "actual", "lag_days",
            "stale", "overdue", "days_behind", "text"}
    for name in PANELS:
        rows = _rows(name, today="2026-11-16")
        assert rows, f"{name} 一条新鲜度都没有——监控等于关掉了"
        for r in rows:
            assert need <= set(r), f"{name} 的行缺字段：{sorted(need - set(r))}"
            assert r["freq"] in CHECKABLE_FREQS, r
            # expect / actual 都是可解析的日期串
            pd.Timestamp(r["expect"])
            pd.Timestamp(r["actual"])
