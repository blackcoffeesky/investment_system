"""验收 1：日期对齐。

黄金用上金所交易日、A 股用沪深300 交易日，二者节假日不同；
把自然日的宏观数据（美联储利率）合并进各自面板时，**绝不能互相污染**。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from shared.calendar import align_to_trading_calendar, merge_shared_nodes


# 上金所：含 2026 春节休市（2026-02-16 ~ 02-20）
GOLD_CAL = pd.DatetimeIndex([
    "2026-02-12", "2026-02-13", "2026-02-23", "2026-02-24", "2026-02-25",
])
# 沪深300：含 2026 国庆休市（2026-10-01 ~ 10-08），但 02-16~02-20 也休市
ASHARE_CAL = pd.DatetimeIndex([
    "2026-02-12", "2026-02-13", "2026-06-01", "2026-06-02", "2026-10-09",
])


def _natural_days() -> pd.DataFrame:
    """自然日序列：每个自然日都有值（包括周末与节假日）。"""
    idx = pd.date_range("2026-02-01", "2026-10-31", freq="D")
    return pd.DataFrame({"FED_RATE": np.linspace(4.0, 3.5, len(idx))}, index=idx)


def test_alignment_keeps_only_asset_calendar():
    src = _natural_days()
    for cal in (GOLD_CAL, ASHARE_CAL):
        out, rep = align_to_trading_calendar(src["FED_RATE"], cal)
        assert list(out.index) == list(cal), "对齐后索引必须与资产日历完全一致"
        assert out.notna().all()
        assert rep.dropped > 0, "自然日里必然有非交易日被丢弃"


def test_no_cross_contamination():
    """A 股的交易日不能出现在黄金面板里，反之亦然。"""
    src = _natural_days()

    panel_gold = pd.DataFrame({"CLOSE": 900.0}, index=GOLD_CAL)
    panel_ash = pd.DataFrame({"CLOSE": 4100.0}, index=ASHARE_CAL)

    merged_gold, _ = merge_shared_nodes(panel_gold, src, verbose=False)
    merged_ash, _ = merge_shared_nodes(panel_ash, src, verbose=False)

    assert set(merged_gold.index) == set(GOLD_CAL)
    assert set(merged_ash.index) == set(ASHARE_CAL)

    # 2026-06-01 是 A 股交易日，但不是黄金交易日
    assert pd.Timestamp("2026-06-01") in merged_ash.index
    assert pd.Timestamp("2026-06-01") not in merged_gold.index

    # 2026-02-23 是黄金交易日，但不是 A 股交易日
    assert pd.Timestamp("2026-02-23") in merged_gold.index
    assert pd.Timestamp("2026-02-23") not in merged_ash.index


def test_ffill_across_gap():
    """外部序列缺某个交易日时，用**前一个有效值**延续，不插值。"""
    src = _natural_days()
    # 人为制造缺口：把 2026-02-23 从外部序列里去掉（模拟数据源当天没更新）
    src = src.drop(pd.Timestamp("2026-02-23"))
    out, _ = align_to_trading_calendar(src["FED_RATE"], GOLD_CAL)
    raw = src["FED_RATE"]
    assert pd.Timestamp("2026-02-23") in out.index
    assert out.loc["2026-02-23"] == raw.loc["2026-02-22"]
    assert out.notna().all(), "缺口必须被 ffill 填上，不能留下 NaN"
