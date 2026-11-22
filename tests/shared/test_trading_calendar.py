"""`shared.utils.trading_calendar` 的单元测试。

从 `datahub/fin/check.py` 搬出来（2026-11-22），顺手补了两个它一直没测到的边界：

1. `expected_periods` 的四种频率 + `market="NONE"`（不挂交易日历）
2. `last_closed_trading_day` 的四种时刻：周末 / 盘中 / 收盘后 / 非交易日

第 2 条是**真的抓到了 bug**：原实现把"今天不是交易日"也走成"退回上一个"，
于是周日返回周五的**前一天**。写数据守卫依赖它，错一天就会拦住/放行错的数据。
"""

from __future__ import annotations

import pandas as pd

from shared.utils.trading_calendar import (expected_periods, is_trading_day,
                                           last_closed_trading_day, trading_days)


def test_trading_days_excludes_weekends_and_respects_market() -> None:
    cn = trading_days("2026-11-10", "2026-11-20")
    assert len(cn) >= 7
    assert not any(d.weekday() >= 5 for d in cn), "交易日里不该有周末"
    us = trading_days("2026-11-10", "2026-11-20", "US")
    assert len(us) >= 7 and not any(d.weekday() >= 5 for d in us)


def test_expected_periods_daily_matches_trading_days() -> None:
    assert (expected_periods("D", "CN", "2026-11-10", "2026-11-20")
            == trading_days("2026-11-10", "2026-11-20")).all()


def test_expected_periods_takes_last_trading_day_of_each_period() -> None:
    """周/月/季/年：取每个周期的**最后一个交易日**，不是自然日历的月末。"""
    week = [str(d.date()) for d in expected_periods("W", "CN", "2026-11-01", "2026-11-20")]
    assert week == ["2026-11-06", "2026-11-13", "2026-11-20"], week

    mon = [str(d.date()) for d in expected_periods("M", "CN", "2026-09-01", "2026-11-20")]
    assert mon == ["2026-09-30", "2026-10-30", "2026-11-20"], mon

    # 季度必须**切到完整周期为止**：end 落在 Q4 中间时会多出"Q4 到现在为止的最后一天"，
    # 那是"窗口内最后一个交易日"，不是"Q4 的期末"。
    # 缺口检查里 end 取的是表里已有的最大日期，所以不会碰到这种半截周期。
    quarter = [str(d.date()) for d in expected_periods("Q", "CN", "2026-01-01", "2026-09-30")]
    assert quarter == ["2026-03-31", "2026-06-30", "2026-09-30"], quarter


def test_expected_periods_none_market_ignores_calendar() -> None:
    """`market="NONE"`：只用自然周期末，不挂交易日历。"""
    days = expected_periods("D", "NONE", "2026-11-10", "2026-11-20")
    assert len(days) == 11, "自然日应为 11 天"
    assert any(d.weekday() >= 5 for d in days), "NONE 市场不该剔除周末"


def test_is_trading_day() -> None:
    assert is_trading_day("2026-11-20")          # 周五
    assert not is_trading_day("2026-11-21")       # 周六
    assert not is_trading_day("2026-11-22")       # 周日


def test_last_closed_trading_day_four_moments() -> None:
    """周末 / 盘中 / 收盘后 / 非交易日。

    2026-11-22 是周日，最后收盘日是 11-20（周五）。
    原实现对"今天不是交易日"也退回 `days[-2]`，会返回 11-19 —— 错一天。
    """
    tz = "Asia/Shanghai"
    cases = {
        pd.Timestamp("2026-11-22 12:00", tz=tz): "2026-11-20",  # 周日
        pd.Timestamp("2026-11-21 09:00", tz=tz): "2026-11-20",  # 周六
        pd.Timestamp("2026-11-20 10:00", tz=tz): "2026-11-19",  # 周五盘中
        pd.Timestamp("2026-11-20 16:00", tz=tz): "2026-11-20",  # 周五收盘后
    }
    for now, want in cases.items():
        got = last_closed_trading_day(now)
        assert got == want, f"{now} 期望 {want}，实际 {got}"
