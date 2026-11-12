"""验收：对齐报告的口径。

`filled` 必须是"只靠 ffill 才有的交易日数"，`extra` 是"早于首个观测、无法填充的天数"。
旧实现拿原始索引上的非空行数去减日历索引上的非空行数，两个索引不同，差值是假的。
"""

from __future__ import annotations

import pandas as pd

from shared.calendar import align_to_trading_calendar

CAL = pd.bdate_range("2024-01-01", periods=6)


def test_filled_counts_only_ffilled_days():
    src = pd.Series([1.0, 3.0], index=[CAL[0], CAL[2]])
    out, rep = align_to_trading_calendar(src, CAL)
    # 原生有值 2 天；ffill 后全 6 天都有值 → 填充 4 天
    assert rep.filled == 4
    assert rep.extra == 0
    assert out.notna().all()


def test_extra_counts_leading_gap():
    src = pd.Series([1.0, 2.0], index=[CAL[2], CAL[3]])
    out, rep = align_to_trading_calendar(src, CAL)
    # 前 2 天早于首个观测，无法填充
    assert rep.extra == 2
    # 原生 2 天，填充后 4 天有值 → 填充 2 天
    assert rep.filled == 2
    assert out.isna().sum() == 2


def test_empty_source_raises_unless_allowed():
    src = pd.Series([], dtype=float, index=pd.DatetimeIndex([]))
    try:
        align_to_trading_calendar(src, CAL)
    except ValueError:
        pass
    else:
        raise AssertionError("全空输入应当报错")
    out, rep = align_to_trading_calendar(src, CAL, allow_empty=True)
    assert rep.filled == 0 and rep.extra == len(CAL)


def test_dataframe_reports_are_consistent():
    src = pd.DataFrame(
        {"a": [1.0, 2.0], "b": [3.0, 4.0]}, index=[CAL[1], CAL[4]]
    )
    out, rep = align_to_trading_calendar(src, CAL)
    assert rep.extra == 1          # CAL[0] 早于首个观测
    assert rep.filled == 3         # CAL[2], CAL[3], CAL[5] 靠 ffill
    assert out.loc[CAL[3], "b"] == 3.0
