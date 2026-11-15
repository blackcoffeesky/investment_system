"""黄金状态面板：只声明状态识别需要的数据。

**基础面板来自耦合层**（`shared.adapter.get_data`）；本面板没有派生列。
"""

from __future__ import annotations

import pandas as pd

from shared.adapter import Panel, column_of, get_data

REQUIRED_TABLES = (
    "gold_cny",     # OHLCV 主价格
    "usdcny",       # → usdcny_vol_5d
    "vix",          # → vix_level
)

COLS = {
    "OPEN": ("gold_cny", "open"),
    "HIGH": ("gold_cny", "high"),
    "LOW": ("gold_cny", "low"),
    "CLOSE": ("gold_cny", "close"),
    "VOLUME": ("gold_cny", "volume"),
    "USDCNY": "usdcny",
    "VIX": "vix",
}


def panel(series=None, meta=None) -> pd.DataFrame:
    """基础面板来自耦合层；`series` / `meta` 保留只为兼容现有调用点（过渡期）。"""
    base = get_data(Panel.STATE_GOLD)
    return pd.DataFrame({out: base[column_of(spec)] for out, spec in COLS.items()},
                        index=base.index)
