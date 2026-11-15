"""A 股特征构造。

输入是**面板**，输出是 `config.FEATURES` 顺序的特征矩阵。

面板有两个来源、列名大小写不同，在 `build()` 入口统一收口一次：

- 生产：`shared.adapter.get_data(Panel.STATE_ASHARE)` → 小写语义名（`close` / `csi300`）
- 交叉校验：因果层的面板（`load_raw` / `prepare`）→ 大写（`CLOSE` / `CSI300`）

内部公式一律用**大写**列名，和因果层那份实现逐字对应（`test_card_smoke` 守着）。
注意：这里不读数据、不做滞后处理——面板怎么来是调用方的事。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from shared.utils import features as shared_features

from state_layer.state_assets.ashare import config as C


def _upper(df: pd.DataFrame) -> pd.DataFrame:
    """列名统一成大写（状态层内部的列约定）；本来就全大写时原样返回。"""
    cols = [c for c in df.columns if isinstance(c, str)]
    if all(c.isupper() for c in cols):
        return df
    return df.rename(columns=lambda c: c.upper() if isinstance(c, str) else c)


def _etf_dispersion(df: pd.DataFrame) -> pd.Series:
    """ETF 分歧度：面板自带就用，否则从分组份额现算。

    - 因果层面板：自带 `ETF_DISPERSION`
    - 耦合层面板：只给 `etf_sse50/etf_csi300/etf_csi1000`（**原生观测日**），
      交给 `shared.utils.features.etf_dispersion` 按同一口径算
      （dropna → pct_change → std → 最后才 ffill，顺序不能改）
    - 别的模块的面板（没有 ETF 列）：整列 NaN；整列失效由
      `tests/test_state_features_fresh.py` 兜住，不在这里静默放行、也不炸
    """
    for col in ("ETF_DISPERSION", "etf_dispersion"):
        if col in df.columns:
            return df[col]
    if any(c in df.columns for c in shared_features.ETF_GROUPS):
        return shared_features.etf_dispersion(df)
    return pd.Series(np.nan, index=df.index)


def _derive(df: pd.DataFrame) -> pd.DataFrame:
    """补上状态特征需要的派生列。

    因果层的 `prepare()` 也算了这几列，但那是给边模型用的。
    状态层自带一份**最小派生**，这样它不依赖因果层的面板处理——
    两边公式必须一致，`test_state_features_match_prepare` 守着。
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
    disp = _etf_dispersion(df)          # 先用原始列名（分组份额是小写）
    df = _derive(_upper(df))            # 之后的公式只认大写列名
    out = pd.DataFrame(index=df.index)
    ret = df["CLOSE"].pct_change()
    out["log_return"] = np.log1p(ret)
    out["realized_vol_20"] = ret.rolling(20).std()
    out["volume_z"] = df["VOLUME_Z"]
    # 20 日均线斜率：均线每天上移多少（占价格的比例）
    # 用比例而不是"元/天"，不同价位的资产才可比；上行/下行都能表示
    ma20 = df["CLOSE"].rolling(20, min_periods=10).mean()
    out["ma20_slope"] = ma20.diff() / df["CLOSE"].replace(0, np.nan) * 100.0
    # 旧特征：已不在 FEATURES 里，但现存模型还在用它，保留计算以兼容
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
    out["etf_dispersion"] = disp.reindex(out.index)
    # 返回全部列：用哪几列由 config.FEATURES 决定（老模型据此仍可推理）
    return out
