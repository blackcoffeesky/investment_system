"""四组特征的定义（长期窗口可切换）。

    from experiment.hmm_state.features import build_groups, ORDER_COL

## 为什么单独抽出来

`stage1.py` 里的特征定义已经用于阶段一/二/三的历史结论，**不能改**（改了旧结论就对不上）。
这里提供两套长期窗口，用参数选：

| 配置 | 长价格 | 长波动 |
|---|---|---|
| `"20-60"` | `ret_20 ret_60 er_20 er_60 snr_20` | `gk_20 atr_20 rv_40 vol_rank_250` |
| `"60-120"` | `ret_60 ret_120 er_60 er_120 snr_60` | `gk_60 atr_60 rv_120 vol_rank_250` |

2026-11-21 用户要求把长期窗口拉到 60–120：原先 20–60 与短周期 5–10 的图"看不出区别"，
说明两个尺度离得太近。窗口分开之后，短周期描述"这周怎么走"，中长期描述"这季度怎么走"。

短周期两组两套配置下**完全一致**，这样对照才干净。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

SHORT = "5-10"
LONG_DEFAULT = "60-120"

#: 状态排序用的列（越大越"强"/越"危险"）
ORDER_COL = {
    "短价格": "ret_10",
    "短波动": "rv_5",
    "长价格": "ret_120",     # 20-60 版用 ret_60，见 order_col_for()
    "长波动": "gk_60",
}


def order_col_for(name: str, long_window: str = LONG_DEFAULT) -> str:
    if name == "长价格":
        return "ret_60" if long_window == "20-60" else "ret_120"
    if name == "长波动":
        return "gk_20" if long_window == "20-60" else "gk_60"
    return ORDER_COL[name]


# ---------------------------------------------------------------- 原语


def _er(c: pd.Series, n: int) -> pd.Series:
    change = (c - c.shift(n)).abs()
    path = c.diff().abs().rolling(n, min_periods=n).sum()
    return (change / path.replace(0, np.nan)).clip(0, 1)


def _snr(c: pd.Series, n: int) -> pd.Series:
    r = c.pct_change()
    mu = r.rolling(n, min_periods=max(3, n // 2)).mean()
    se = r.rolling(n, min_periods=max(3, n // 2)).std() / np.sqrt(n)
    return mu / se.replace(0, np.nan)


def _tr(h, l, c) -> pd.Series:
    pc = c.shift(1)
    return pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)


def _gk(c, h, l, o, n) -> pd.Series:
    hl = np.log(h / l) ** 2
    co = np.log(c / o) ** 2
    var = (0.5 * hl - (2 * np.log(2) - 1) * co).rolling(n, min_periods=max(3, n // 2)).mean()
    return np.sqrt(var.clip(lower=0)) * np.sqrt(252) * 100.0


def _rv(c: pd.Series, n: int) -> pd.Series:
    return c.pct_change().rolling(n, min_periods=max(3, n // 2)).std() * np.sqrt(252) * 100.0


def _vol_rank(c: pd.Series, n_sig: int = 20, n_rank: int = 250) -> pd.Series:
    v = _rv(c, n_sig)
    return v.rolling(n_rank, min_periods=n_rank // 2).apply(
        lambda x: float((x[:-1] < x[-1]).mean()), raw=True)


# ---------------------------------------------------------------- 组装


def build_groups(m: pd.DataFrame, long_window: str = LONG_DEFAULT) -> dict[str, pd.DataFrame]:
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

    if long_window == "20-60":
        long_price = pd.DataFrame({
            "ret_20": (c / c.shift(20) - 1.0) * 100.0,
            "ret_60": (c / c.shift(60) - 1.0) * 100.0,
            "er_20": _er(c, 20),
            "er_60": _er(c, 60),
            "snr_20": _snr(c, 20),
        })
        long_vol = pd.DataFrame({
            "gk_20": _gk(c, h, l, o, 20),
            "atr_20": (_tr(h, l, c).rolling(20, min_periods=10).mean() / c * 100.0),
            "rv_40": _rv(c, 40),
            "vol_rank_250": _vol_rank(c, 20, 250),
        })
    elif long_window == "60-120":
        long_price = pd.DataFrame({
            "ret_60": (c / c.shift(60) - 1.0) * 100.0,
            "ret_120": (c / c.shift(120) - 1.0) * 100.0,
            "er_60": _er(c, 60),
            "er_120": _er(c, 120),
            "snr_60": _snr(c, 60),
        })
        long_vol = pd.DataFrame({
            "gk_60": _gk(c, h, l, o, 60),
            "atr_60": (_tr(h, l, c).rolling(60, min_periods=30).mean() / c * 100.0),
            "rv_120": _rv(c, 120),
            "vol_rank_250": _vol_rank(c, 60, 250),
        })
    else:
        raise ValueError(f"未知长窗口配置：{long_window}")

    return {"短价格": short_price, "短波动": short_vol,
            "长价格": long_price, "长波动": long_vol}


def agreement(a: pd.Series, b: pd.Series) -> float:
    """两个状态序列的一致率（同一日期同编号的比例）。

    这一致率**只是粗略指标**：两套状态的编号各自按画像排过，所以"同号"约等于"同档"。
    值越低说明两个尺度切出来的东西越不一样。
    """
    j = pd.concat([a.rename("a"), b.rename("b")], axis=1).dropna()
    return float((j["a"] == j["b"]).mean())
