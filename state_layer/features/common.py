"""特征预处理：**唯一实现**。

三条路径（训练、逐日解码、在线后验）必须走同一个函数，否则"fit 时写下的后验"
和"daily 重算的后验"会对不上——v5.8 就是这么出的 bug：
先标准化、再拿**原始分位**去裁剪 z 值，几乎把所有点压到边界。

顺序固定为：**先按原始分位 winsorize，再标准化**。

放在 `features/` 下而不是 `model/`：这是特征的属性，任何模型都该用同一套。
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
