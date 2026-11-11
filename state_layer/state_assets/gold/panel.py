"""黄金状态面板：只声明状态识别需要的数据。"""

from __future__ import annotations

import pandas as pd

from datahub import panel as P

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


def panel(series, meta=None) -> pd.DataFrame:
    return P.build(COLS, index_table="gold_cny", series=series, meta=meta)
