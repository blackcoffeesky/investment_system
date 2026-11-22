"""A 股特征：按**分组**构造（2026-11-22 起与 experiment/hmm_state 同口径）。

**生产是唯一实现**：实验目录不进 git，方向只能是"实验 import 生产"。

当前只启用 `short_price`（用户 2026-11-22：去掉极端后 A 股状态数就是 3）。
`short_vol` / `long_price` 的构造函数已经在这里，但没进 `config.GROUPS_ENABLED`
——**多分组是"已具备、暂未启用"的能力**，加组时只动 config，不改协议。

输入是面板（`get_data(Panel.STATE_ASHARE)`，小写语义列名），只用到 OHLC。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from state_layer.state_assets.ashare import config as C


def _lower(df: pd.DataFrame) -> pd.DataFrame:
    return df.rename(columns=lambda c: c.lower() if isinstance(c, str) else c)


def _er(c: pd.Series, n: int) -> pd.Series:
    change = (c - c.shift(n)).abs()
    path = c.diff().abs().rolling(n, min_periods=n).sum()
    return (change / path.replace(0, np.nan)).clip(0, 1)


def _snr(c: pd.Series, n: int) -> pd.Series:
    r = c.pct_change()
    mu = r.rolling(n, min_periods=max(3, n // 2)).mean()
    se = r.rolling(n, min_periods=max(3, n // 2)).std() / np.sqrt(n)
    return mu / se.replace(0, np.nan)


def _tr(h: pd.Series, low: pd.Series, c: pd.Series) -> pd.Series:
    pc = c.shift(1)
    return pd.concat([(h - low).abs(), (h - pc).abs(), (low - pc).abs()],
                     axis=1).max(axis=1)


def _gk(c: pd.Series, h: pd.Series, low: pd.Series, o: pd.Series,
        n: int) -> pd.Series:
    hl = np.log(h / low) ** 2
    co = np.log(c / o) ** 2
    var = (0.5 * hl - (2 * np.log(2) - 1) * co).rolling(
        n, min_periods=max(3, n // 2)).mean()
    return np.sqrt(var.clip(lower=0)) * np.sqrt(252) * 100.0


def _rv(c: pd.Series, n: int) -> pd.Series:
    return (c.pct_change().rolling(n, min_periods=max(3, n // 2)).std()
            * np.sqrt(252) * 100.0)


def short_price(df: pd.DataFrame) -> pd.DataFrame:
    """短价格：5/10 日动量 + 效率 + 信噪比（只用 close）。"""
    c = _lower(df)["close"]
    return pd.DataFrame({
        "ret_5": (c / c.shift(5) - 1.0) * 100.0,
        "ret_10": (c / c.shift(10) - 1.0) * 100.0,
        "er_10": _er(c, 10),
        "snr_10": _snr(c, 10),
    })


def short_vol(df: pd.DataFrame) -> pd.DataFrame:
    """短波动：rv / atr / garman-klass / 绝对收益（**当前未启用**）。"""
    d = _lower(df)
    c, h, low, o = d["close"], d["high"], d["low"], d["open"]
    return pd.DataFrame({
        "rv_5": _rv(c, 5),
        "atr_5": (_tr(h, low, c).rolling(5, min_periods=3).mean() / c * 100.0),
        "gk_10": _gk(c, h, low, o, 10),
        "abs_ret_5": c.pct_change().abs().rolling(5, min_periods=3).mean() * 100.0,
    })


def long_price(df: pd.DataFrame) -> pd.DataFrame:
    """长价格：60/120 日动量 + 效率（**当前未启用**）。"""
    c = _lower(df)["close"]
    return pd.DataFrame({
        "ret_60": (c / c.shift(60) - 1.0) * 100.0,
        "ret_120": (c / c.shift(120) - 1.0) * 100.0,
        "er_60": _er(c, 60),
        "er_120": _er(c, 120),
        "snr_60": _snr(c, 60),
    })


_BUILDERS = {"short_price": short_price, "short_vol": short_vol,
             "long_price": long_price}


def build_groups(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """只返回 `config.GROUPS_ENABLED` 里的组（现在只有 short_price）。"""
    return {name: _BUILDERS[name](df) for name in C.GROUPS_ENABLED}
