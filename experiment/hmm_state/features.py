"""特征定义：短价格、短波动、中长期价格；已删除中长期波动"""
from __future__ import annotations
import numpy as np
import pandas as pd


def _er(c: pd.Series, n: int) -> pd.Series:
    change = (c - c.shift(n)).abs()
    path = c.diff().abs().rolling(n, min_periods=n).sum()
    return (change / path.replace(0, np.nan)).clip(0, 1)


def _snr(c: pd.Series, n: int) -> pd.Series:
    r = c.pct_change()
    mu = r.rolling(n, min_periods=max(3, n // 2)).mean()
    se = r.rolling(n, min_periods=max(3, n // 2)).std() / np.sqrt(n)
    return mu / se.replace(0, np.nan)


def _tr(h, l, c):
    pc = c.shift(1)
    return pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)


def _gk(c, h, l, o, n) -> pd.Series:
    hl = np.log(h / l) ** 2
    co = np.log(c / o) ** 2
    var = (0.5 * hl - (2 * np.log(2) - 1) * co).rolling(n, min_periods=max(3, n // 2)).mean()
    return np.sqrt(var.clip(lower=0)) * np.sqrt(252) * 100.0


def _rv(c: pd.Series, n: int) -> pd.Series:
    return c.pct_change().rolling(n, min_periods=max(3, n // 2)).std() * np.sqrt(252) * 100.0


def build_feature_groups(m: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """
    返回：短价格、短波动、长价格；**剔除中长期波动特征**
    :param m: index=date，包含 open high low close amount
    """
    c, h, l, o = m["close"], m["high"], m["low"], m["open"]

    short_price = pd.DataFrame({
        "ret_5": (c / c.shift(5) - 1.0) * 100.0,
        "ret_10": (c / c.shift(10) - 1.0) * 100.0,
        "er_10": _er(c, 10),
        "snr_10": _snr(c, 10),
    })

    short_vol = pd.DataFrame({
        "rv_5": _rv(c, 5),
        "atr_5": (_tr(h, l, c).rolling(5, min_periods=3).mean() / c * 100.0),
        "gk_10": _gk(c, h, l, o, 10),
        "abs_ret_5": c.pct_change().abs().rolling(5, min_periods=3).mean() * 100.0,
    })

    long_price = pd.DataFrame({
        "ret_60": (c / c.shift(60) - 1.0) * 100.0,
        "ret_120": (c / c.shift(120) - 1.0) * 100.0,
        "er_60": _er(c, 60),
        "er_120": _er(c, 120),
        "snr_60": _snr(c, 60),
    })
    return {
        "短价格": short_price,
        "短波动": short_vol,
        "长价格": long_price
    }


def order_column_map(name: str):
    """训练后状态排序依据，越大代表越强/风险越高"""
    mapping = {
        "短价格": "ret_10",
        "短波动": "rv_5",
        "长价格": "ret_120"
    }
    return mapping[name]


def agreement(a: pd.Series, b: pd.Series) -> float:
    """两个状态序列一致率，仅诊断用"""
    j = pd.concat([a.rename("a"), b.rename("b")], axis=1).dropna()
    return float((j["a"] == j["b"]).mean())
