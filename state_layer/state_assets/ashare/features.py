"""A 股特征构造（P1 为 causual 侧 `build_features` 的副本）。

输入是**已准备好的面板**（大写列名，含 CLOSE / VOLUME_Z / MARGIN_CHG …），
输出是 `config.FEATURES` 顺序的特征矩阵。

注意：这里不读数据、不做滞后处理——面板怎么来是调用方的事。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from state_layer.state_assets.ashare import config as C


def _derive(df: pd.DataFrame) -> pd.DataFrame:
    """补上状态特征需要的派生列。

    因果层的 `prepare()` 也算了这几列，但那是给边模型用的。
    状态层自带一份**最小派生**，这样它不依赖因果层的面板处理——
    两边公式必须一致，`test_state_features_match_causual` 守着。
    """
    df = df.copy()
    if "SHIBOR_CHG_BP" not in df and "SHIBOR3M" in df:
        df["SHIBOR_CHG_BP"] = df["SHIBOR3M"].diff() * 100.0
    if "MARGIN_CHG" not in df and "MARGIN_BALANCE" in df:
        df["MARGIN_CHG"] = df["MARGIN_BALANCE"].pct_change() * 100.0
    if "VOLUME_Z" not in df and "VOLUME" in df:
        vol = df["VOLUME"]
        mu = vol.rolling(60, min_periods=20).mean()
        sd = vol.rolling(60, min_periods=20).std()
        df["VOLUME_Z"] = (vol - mu) / sd.replace(0, np.nan)
    return df


def build(df: pd.DataFrame) -> pd.DataFrame:
    df = _derive(df)
    out = pd.DataFrame(index=df.index)
    ret = df["CLOSE"].pct_change()
    out["log_return"] = np.log1p(ret)
    out["realized_vol_20"] = ret.rolling(20).std()
    out["volume_z"] = df["VOLUME_Z"]
    out["up_day_ratio"] = (ret > 0).astype(float).rolling(20).mean()
    out["size_mom"] = ((df["CSI1000"] / df["CSI300"]).pct_change(20)
                       if "CSI1000" in df else np.nan)
    out["margin_chg_z"] = df["MARGIN_CHG"].rolling(60, min_periods=20).apply(
        lambda x: (x.iloc[-1] - x.mean()) / x.std() if x.std() > 0 else np.nan,
        raw=False)
    out["rv_ratio"] = ret.rolling(5).std() / ret.rolling(20).std()
    out["shibor_chg_z"] = df["SHIBOR_CHG_BP"].rolling(60, min_periods=20).apply(
        lambda x: (x.iloc[-1] - x.mean()) / x.std() if x.std() > 0 else np.nan,
        raw=False)
    out["etf_dispersion"] = df.get("ETF_DISPERSION",
                                   pd.Series(np.nan, index=df.index))
    return out[C.FEATURES]
