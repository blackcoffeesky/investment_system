"""黄金特征构造（P1 为 causual 侧 `build_features` 的副本）。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from state_layer.config import gold as C


def build(df: pd.DataFrame) -> pd.DataFrame:
    """状态识别用的 9 维特征（与 A 股完全不同，所以各自实现）。"""
    out = pd.DataFrame(index=df.index)
    ret = df["CLOSE"].pct_change()
    out["log_return"] = np.log1p(ret)
    out["realized_vol_5d"] = ret.rolling(5).std()
    out["range_pct"] = (df["HIGH"] - df["LOW"]) / df["CLOSE"].replace(0, np.nan)
    vol = out["realized_vol_5d"]
    out["vol_z"] = ((vol - vol.rolling(60, min_periods=20).mean())
                    / vol.rolling(60, min_periods=20).std())
    out["usdcny_vol_5d"] = df["USDCNY"].pct_change().rolling(5).std()
    out["vix_level"] = df["VIX"] / 100.0
    out["mom_20"] = df["CLOSE"] / df["CLOSE"].shift(20) - 1.0
    out["rv_ratio"] = ret.rolling(5).std() / ret.rolling(20).std()
    out["gap"] = df["OPEN"] / df["CLOSE"].shift(1) - 1.0
    return out[C.FEATURES]
