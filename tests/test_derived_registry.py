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
    """派生量：传窗口 == 全量切片（**逐位**，NaN 也算相等）。"""
    cases = {
        "breadth": lambda: DERIVED["breadth"].func(START, END),
        "dividend_yield": lambda: DERIVED["dividend_yield"].func(start=START, end=END),
    }
    for name, windowed in cases.items():
        d = DERIVED[name]
        full = d.func()
        got = windowed()
        assert len(got) > 0, f"{name} 窗口内没有数据，测试前提不成立"
        expect = full.loc[got.index[0]:got.index[-1]] if isinstance(got.index, pd.DatetimeIndex) \
            else full.loc[got.index]
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
