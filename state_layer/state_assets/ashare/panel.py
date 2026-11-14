"""A 股状态面板：**只声明状态识别需要的数据**。

和 `causual_assets/ashare/panel.py` 是两份独立的声明——因果层要的宏观表更多，
状态层只取识别状态用得到的几列。两边的拼装逻辑都走 `datahub.fin.read`，
所以对齐口径（lag_days、ffill）只有一份实现。

数据来源：调用方传 `series`（{表名: Series}），状态层不自己去读库。
"""

from __future__ import annotations

import pandas as pd

from shared import panel_align as P

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

# ETF 分组：宽基规模由小到大（裸 6 位码；比对时双方都会归一化）
GROUPS = {
    "SSE50": ["510050", "510710", "510100", "510190"],
    "CSI300": ["510300", "510310", "510330"],
    "CSI1000": ["512100", "560010", "510580", "510510"],
}


def bare_code(name: object) -> str:
    """把各种写法的 ETF 代码归一成裸 6 位。

    数据层 2026-11 起给代码加了市场前缀（`etf_shares:sh510300`），
    而这之前是 `c510300` / `510300` 之类的写法。统一取**最后 6 位数字**：
    `sh510300` / `sz159919` / `c510300` / `510300` → `510300`。

    不认得的形状原样返回（长度不足 6 位数字）——匹配不上就自然跳过，
    但下面的「整列 NaN」会在 `tests/test_state_features_fresh.py` 里被抓住。
    """
    digits = "".join(ch for ch in str(name) if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else str(name)


def _etf_dispersion(idx: pd.DatetimeIndex, series) -> pd.Series:
    """宽基 ETF 申赎的分歧度：各组份额变化的截面标准差。

    数据层不存派生量，这里现算。缺失就返回空序列，特征里自然为 NaN——
    注意**这是静默失效**：整列 NaN 会让 HMM 一行都不认领，所以
    `tests/test_state_features_fresh.py` 专门盯着每列的最后有效日期。
    """
    keys = [k for k in series if k.startswith("etf_shares:")]
    if len(keys) < 3:
        return pd.Series(index=idx, dtype=float)
    # 列名按裸 6 位归一，GROUPS 也按同样口径比——代码前缀怎么变都不怕
    w = pd.DataFrame({bare_code(k.split(":", 1)[1]): series[k] for k in keys})

    parts = []
    for g in ("SSE50", "CSI300", "CSI1000"):
        want = [bare_code(code) for code in GROUPS.get(g, [])]
        cols = [c for c in want if c in w.columns]
        if cols:
            parts.append(w[cols].sum(axis=1).pct_change())
    if len(parts) < 2:
        return pd.Series(index=idx, dtype=float)
    return pd.concat(parts, axis=1).std(axis=1).reindex(idx, method="ffill")


def panel(series, meta=None) -> pd.DataFrame:
    df = P.build(COLS, index_table="csi_all", series=series, meta=meta)
    df["ETF_DISPERSION"] = _etf_dispersion(df.index, series)
    return df
