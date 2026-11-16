"""黄金特征构造。

输入是**面板**，输出是 `config.FEATURES` 顺序的特征矩阵。

面板有两个来源、列名大小写不同（生产走 `shared.adapter.get_data(Panel.STATE_GOLD)`
是小写语义名，因果层面板是大写），在 `build()` 入口统一收口一次；
内部公式一律用大写列名。

注意：这里不读数据、不做滞后处理——面板怎么来是调用方的事。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from shared.utils.data_processing import diff_bp, pct_change_pct, rolling_z

from state_layer.state_assets.gold import config as C


def _upper(df: pd.DataFrame) -> pd.DataFrame:
    """列名统一成大写（状态层内部的列约定）；本来就全大写时原样返回。"""
    cols = [c for c in df.columns if isinstance(c, str)]
    if all(c.isupper() for c in cols):
        return df
    return df.rename(columns=lambda c: c.upper() if isinstance(c, str) else c)


def _derive(df: pd.DataFrame) -> pd.DataFrame:
    """补上状态特征需要的派生列。

    这三条走 `shared.utils.data_processing` 的**单位原语**（和 A 股、和 causual 的
    `prepare()` 同一个函数）。黄金面板里通常没有 `SHIBOR3M` / `MARGIN_BALANCE`，
    前两条是给"万一有"的守卫；`VOLUME_Z` 目前黄金特征用不到（保留为最小派生的一部分）。
    """
    df = df.copy()
    if "SHIBOR_CHG_BP" not in df and "SHIBOR3M" in df:
        df["SHIBOR_CHG_BP"] = diff_bp(df["SHIBOR3M"])
    if "MARGIN_CHG" not in df and "MARGIN_BALANCE" in df:
        df["MARGIN_CHG"] = pct_change_pct(df["MARGIN_BALANCE"])
    if "VOLUME_Z" not in df and "VOLUME" in df:
        df["VOLUME_Z"] = rolling_z(df["VOLUME"])
    return df


def build(df: pd.DataFrame) -> pd.DataFrame:
    df = _derive(_upper(df))
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
    # 返回全部列，用哪几列由 config.FEATURES 决定
    return out
