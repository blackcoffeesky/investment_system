"""派生量注册表 + 窗口不变量。

2026-11-16 起派生量（`breadth` / `dividend_yield`）注册在
`datahub/fin/derive.DERIVED`，并且**自己能处理窗口**：`func(start, end)` 恒等于
`func()` 再 `.loc[start:end]`。`series_map` 见到注册表里的名字就现算，
形态和普通表完全同构（`{名:列: Series}`）——调用方分不出哪个是算出来的。

这组测试钉三件事：
1. **注册表条目完整**（func / depends / columns / period / doc 都在）
2. **窗口不变量**：派生量传不传窗口、传多大窗口，同一日期必须逐位相同
3. **面板的窗口不变量**（warmup 的验收）：`get_data(p, start, end)` == `get_data(p)` 切片

第 3 条是 warmup 的正面证据：没有 warmup 时 `CAUSUAL_GOLD` 在 2025-01-01 起取，
头部 23 行会与全量切片不同（低频列没有历史可 ffill）。
"""

from __future__ import annotations

import pandas as pd

from datahub.fin.derive import DERIVED
from datahub.port import get_port

START, END = "2025-01-01", "2025-12-31"


def test_registry_entries_are_complete():
    assert DERIVED, "注册表是空的"
    for name, d in DERIVED.items():
        assert callable(d.func), f"{name} 没有 func"
        assert d.columns, f"{name} 没有声明 columns"
        assert d.period in ("D", "W", "M", "Q", "Y"), f"{name} 的 period 不认识：{d.period}"
        assert d.doc, f"{name} 没有 doc"
        assert d.depends, f"{name} 没声明依赖表"
        assert d.depends_note, f"{name} 没写 depends_note（依赖的语义边界）"


def test_derived_window_invariance():
    """派生量：传窗口 == 全量切片（**逐位**，NaN 也算相等）。

    **遍历整个注册表**，不写死名字——以后加派生量自动被这条测试覆盖
    （warmup 填错的典型症状就是"窗口第一行与全量切片不同"）。
    """
    for name, d in DERIVED.items():
        full = d.func()
        got = d.func(start=START, end=END)
        assert not full.empty, f"{name} 全量是空的，测试前提不成立"
        if got.empty:
            continue
        expect = full.loc[got.index]
        pd.testing.assert_frame_equal(got, expect, check_exact=True)


def test_series_map_serves_derived_with_table_shape():
    port = get_port()
    out = port.series_map(["breadth"], start=START, end=END)
    assert set(out) == {f"breadth:{c}" for c in DERIVED["breadth"].columns}
    assert all(isinstance(s, pd.Series) for s in out.values())
    assert len(out["breadth:adv"]) > 0

    # 和真表混在一批里也要正常（形态同构）
    mixed = port.series_map(["csi_all", "breadth"], start="2025-06-01", end="2025-06-30")
    assert "csi_all:close" in mixed and "breadth:adv" in mixed


def test_get_data_window_matches_full_slice():
    """warmup 的正面证据：四个面板传窗口 == 全量切片。

    没有 warmup 时这里会红：`CAUSUAL_GOLD` 2025-01-01 起取，头部 23 行与切片不同
    （`usdcny / vix / usd_index / fed_rate` 在窗口起点之前没有观测可 ffill）。
    """
    from shared.adapter import Panel, get_data

    for p in Panel:
        full = get_data(p)
        win = get_data(p, start=START, end=END)
        expect = full.loc[START:END]
        assert len(win) > 0, f"{p.name} 窗口内没有数据"
        assert win.index.equals(expect.index), f"{p.name} 窗口索引与全量切片不一致"
        pd.testing.assert_frame_equal(win, expect, check_exact=True)


def test_dividend_yield_is_causual_only_and_aligned_to_next_year():
    """`dividend_yield` 是**年度**口径，落位规则：年 Y 的值 → Y+1-01-01 → ffill 到日频。

    所以面板里它的"变化点"必须全落在**年初**（1 月的头几个交易日）。
    它也**只给 causual.ashare**：算一次 3.7 秒（扫一遍 `stock_daily`），状态层用不上。
    """
    from shared.adapter import COLUMNS, Panel, get_data

    assert "dividend_yield" in COLUMNS[Panel.CAUSUAL_ASHARE]
    assert "dividend_yield" not in COLUMNS[Panel.STATE_ASHARE], (
        "dividend_yield 不该进状态层面板——它每次要 3.7 秒，state 也不用它")

    col = get_data(Panel.CAUSUAL_ASHARE)["dividend_yield"]
    assert col.notna().any(), "整列 NaN，测试前提不成立"
    v = col.dropna()
    change = v[v.diff().fillna(1.0) != 0.0]
    assert len(change) > 1
    bad = [d for d in change.index if d.month != 1]
    assert not bad, f"年度值的变化点必须落在 1 月（Y+1-01-01 → 首个交易日），这些不在：{bad[:3]}"


def test_adapter_has_no_etf_group_knowledge():
    """ETF 分组知识必须只在数据层（`fin/derive._ETF_GROUPS`），adapter 里不许再出现。

    2026-11-14 的事故根因就是这份知识散在消费方（两套代码归一规则）。
    adapter 现在只该知道"列名叫 etf_sse50"和"派生量叫 etf_group_shares"。
    """
    import inspect

    import shared.adapter.datahub_adapter as A

    src = inspect.getsource(A)
    for code in ("510050", "510710", "512100", "560010", "510580"):
        assert code not in src, f"adapter 里还有 ETF 代码 {code}——分组知识该归数据层"
    assert "def bare_code" not in src, "adapter 里不该再有自己的代码归一"
