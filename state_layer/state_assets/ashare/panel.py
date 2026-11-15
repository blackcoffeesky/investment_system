"""A 股状态面板：**只声明状态识别需要的数据**。

和 `causual_assets/ashare/panel.py` 是两份独立的声明——因果层要的宏观表更多，
状态层只取识别状态用得到的几列。

**数据全部来自耦合层** `shared.adapter.get_data`；派生列走
`shared.utils.features`（纯函数）。数据层内部怎么变（表名、宽表拆解、ETF 代码
格式）都不用改这里。
"""

from __future__ import annotations

import pandas as pd

from shared.adapter import Panel, column_of, get_data
# 转出口：外部脚本 / 测试还在 `from ...panel import bare_code`（归一逻辑已上移到 adapter）
from shared.adapter import bare_code  # noqa: F401
from shared.utils import features

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


def panel(series=None, meta=None) -> pd.DataFrame:
    """基础面板来自耦合层；`series` / `meta` 只为兼容现有调用点保留（已不需要）。"""
    base = get_data(Panel.STATE_ASHARE)
    df = pd.DataFrame({out: base[column_of(spec)] for out, spec in COLS.items()},
                      index=base.index)
    df["ETF_DISPERSION"] = features.etf_dispersion(base)
    return df
