"""读数的标准化：**唯一实现**。

为什么必须统一
--------------
v5.9 之前 z 有四套算法：

| 位置 | 算法 |
|---|---|
| DML 估计 | 全样本均值/标准差 |
| 分布滞后估计 | 边频率上的全样本均值/标准差 |
| `dl_aggregate` | 边频率上滚动 24 期 |
| 卡片上的 z 列 | 日频滚动 252 期 |

于是"估出来的 β"和"乘上去的 z"不是同一个尺度，卡片显示的 z 也不是贡献里用的 z。
更糟的是全样本均值/标准差在实盘不可得（前视）。

统一为：**边自己频率上的滚动 z**，同一函数在估计层与信号层共用。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# 各频率的默认回看窗口（期数）
WINDOW_BY_FREQ: dict[str, int] = {"D": 252, "W": 104, "M": 24, "Q": 8}


def _window_for(freq: str, window: int | None) -> int:
    if window:
        return int(window)
    key = str(freq)[0].upper()
    return WINDOW_BY_FREQ.get(key, 252)


def periodic_z(
    series: pd.Series,
    freq: str = "D",
    window: int | None = None,
    min_periods: int | None = None,
) -> pd.Series:
    """在 series 自己的索引上做滚动标准化（无前视）。"""
    w = _window_for(freq, window)
    mp = min_periods if min_periods is not None else max(12, w // 3)
    mu = series.rolling(w, min_periods=mp).mean()
    sd = series.rolling(w, min_periods=mp).std().replace(0, np.nan)
    return (series - mu) / sd


def edge_z(
    df: pd.DataFrame,
    col: str,
    freq: str = "D",
    window: int | None = None,
    min_periods: int | None = None,
) -> pd.Series:
    """按边自己的频率算滚动 z，再对齐回日频。

    估计层用 `periodic_z`（在重采样后的面板上），信号层用本函数，
    两者窗口与 min_periods 必须一致，否则又会对不上。
    """
    if str(freq) == "D":
        return periodic_z(df[col], freq, window, min_periods)
    s = df[col].resample(freq).last()
    z = periodic_z(s, freq, window, min_periods)
    return z.reindex(df.index, method="ffill")
