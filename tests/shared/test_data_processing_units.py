"""`shared.utils.data_processing` 的三个**单位原语**。

2026-11-16：`SHIBOR_CHG_BP` / `MARGIN_CHG` / `VOLUME_Z` 原本在
`causual/.../pipeline.py::prepare()` 与 `state_layer/.../features.py::_derive()` 各写一遍，
靠一条测试守着"两边算得一样"。收成原语后，两个模块调同一个函数——但**原语本身必须
与那两处逐位相同**，否则"统一"的过程就是一次静默的数值变化。

这组测试就钉这一点：原语 == 原来的手写公式（逐位，含 NaN 位置）。
用合成数据，不碰库。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from shared.utils.data_processing import diff_bp, pct_change_pct, rolling_z

IDX = pd.date_range("2025-01-01", periods=200, freq="B")


def _series(kind: str = "rate") -> pd.Series:
    rng = np.random.default_rng(42)
    if kind == "rate":
        return pd.Series(2.0 + rng.normal(0, 0.05, len(IDX)), index=IDX)
    if kind == "level":
        return pd.Series(15000.0 + rng.normal(0, 300, len(IDX)).cumsum(), index=IDX)
    return pd.Series(rng.lognormal(15, 0.4, len(IDX)), index=IDX)


def test_diff_bp_matches_inline():
    s = _series("rate")
    pd.testing.assert_series_equal(diff_bp(s), s.diff() * 100.0, check_exact=True)
    pd.testing.assert_series_equal(diff_bp(s, 5), s.diff(5) * 100.0, check_exact=True)


def test_pct_change_pct_matches_inline():
    s = _series("level")
    pd.testing.assert_series_equal(pct_change_pct(s), s.pct_change() * 100.0,
                                   check_exact=True)
    pd.testing.assert_series_equal(pct_change_pct(s, 20), s.pct_change(20) * 100.0,
                                   check_exact=True)


def test_rolling_z_matches_inline():
    """`VOLUME_Z` 的手写版本：`(s - mu) / sd.replace(0, nan)`，60 窗口 / 20 最小期。"""
    s = _series("volume")
    mu = s.rolling(60, min_periods=20).mean()
    sd = s.rolling(60, min_periods=20).std().replace(0, np.nan)
    pd.testing.assert_series_equal(rolling_z(s), (s - mu) / sd, check_exact=True)


def test_rolling_z_zero_variance_is_nan_not_inf():
    """整段横盘（标准差 0）必须给 NaN：`inf` 进 HMM/分位会污染整列。"""
    flat = pd.Series(1.0, index=IDX)
    z = rolling_z(flat)
    assert z.isna().all(), "标准差为 0 时应整列 NaN"
    assert not np.isinf(z.to_numpy()).any()
