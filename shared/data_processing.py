"""数据处理：标准化、滚动 z、winsorize。**纯函数，不读库、不 import 任何模块**。

三个来源合并而来（原来分散在三处，各写各的窗口口径）：

- `state_layer/prep.py`：winsorize(原始分位) + standardize，HMM 训练/解码/在线共用
- `causual_core/utils/zscore.py`：边频率上的滚动 z，估计层与信号层共用
- 两者本质都是"把原始读数变成可比尺度"，放一起才能保证同一份口径

顺序约定：**先按原始分位 winsorize，再标准化**。反了会把几乎所有点压到边界（v5.8 的 bug）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd



def prep_features(
    raw: pd.DataFrame,
    scaler,
    lo: pd.Series,
    hi: pd.Series,
) -> np.ndarray:
    """winsorize(原始量纲) → standardize。返回 (n_samples, n_features)。"""
    clipped = raw.clip(lower=lo, upper=hi, axis=1)
    if hasattr(scaler, "transform"):
        return np.asarray(scaler.transform(clipped), dtype=float)
    # 也接受直接给 mean/scale（在线重建时用）
    mean = np.asarray(getattr(scaler, "mean_", scaler["mean"]), dtype=float)
    scale = np.asarray(getattr(scaler, "scale_", scaler["scale"]), dtype=float)
    return (np.asarray(clipped, dtype=float) - mean) / scale


def fit_bounds(feat: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """训练窗口的 1% / 99% 分位，作为 winsorize 边界。"""
    hist = feat.dropna()
    if hist.empty:
        raise ValueError("训练窗口内没有有效特征行")
    return hist.quantile(0.01), hist.quantile(0.99)



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
