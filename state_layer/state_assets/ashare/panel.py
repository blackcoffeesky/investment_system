"""A 股状态面板：**只声明状态识别需要的数据**。

和 `causual_assets/ashare/panel.py` 是两份独立的声明——因果层要的宏观表更多，
状态层只取识别状态用得到的几列。两边的拼装逻辑都走 `datahub.panel.build`，
所以对齐口径（lag_days、ffill）只有一份实现。

数据来源：调用方传 `series`（{表名: Series}），状态层不自己去读库。
"""

from __future__ import annotations

import pandas as pd

from datahub import panel as P

# 需要 datahub 提供的原始表（CLI / service 按这个清单装数据）
REQUIRED_TABLES = (
    "csi_all",          # OHLCV 主价格
    "csi300",
    "csi1000",
    "margin_balance",   # 两融余额 → MARGIN_CHG
    "shibor3m",         # → SHIBOR_CHG_BP
    "etf_shares",       # → ETF_DISPERSION
)

COLS = {
    "OPEN": ("csi_all", "open"),
    "HIGH": ("csi_all", "high"),
    "LOW": ("csi_all", "low"),
    "CLOSE": ("csi_all", "close"),
    "VOLUME": ("csi_all", "volume"),
    "CSI300": "csi300",
    "CSI1000": "csi1000",
    "SHIBOR3M": "shibor3m",
    "MARGIN_BALANCE": "margin_balance",
}

# ETF 分组：宽基规模由小到大
GROUPS = {
    "SSE50": ["510050", "510710", "510100", "510190"],
    "CSI300": ["510300", "510310", "510330"],
    "CSI1000": ["512100", "560010", "510580", "510510"],
}


def _etf_dispersion(idx: pd.DatetimeIndex, series) -> pd.Series:
    """宽基 ETF 申赎的分歧度：各组份额变化的截面标准差。

    数据层不存派生量，这里现算。缺失就返回空序列，特征里自然为 NaN。
    """
    keys = [k for k in series if k.startswith("etf_shares:")]
    if len(keys) < 3:
        return pd.Series(index=idx, dtype=float)
    w = pd.DataFrame({k.split(":", 1)[1]: series[k] for k in keys})
    w = w.rename(columns=lambda c: str(c)[1:] if str(c).startswith("c") else str(c))

    parts = []
    for g in ("SSE50", "CSI300", "CSI1000"):
        cols = [c for c in GROUPS.get(g, []) if c in w.columns]
        if cols:
            parts.append(w[cols].sum(axis=1).pct_change())
    if len(parts) < 2:
        return pd.Series(index=idx, dtype=float)
    return pd.concat(parts, axis=1).std(axis=1).reindex(idx, method="ffill")


def panel(series, meta=None) -> pd.DataFrame:
    df = P.build(COLS, index_table="csi_all", series=series, meta=meta)
    df["ETF_DISPERSION"] = _etf_dispersion(df.index, series)
    return df
