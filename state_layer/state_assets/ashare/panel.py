"""A 股状态面板：**只声明状态识别需要的数据**。

和 `causual_assets/ashare/panel.py` 是两份独立的声明——因果层要的宏观表更多，
状态层只取识别状态用得到的几列。

**数据全部来自耦合层** `shared.adapter.get_data`：数据层内部怎么变（表名、
宽表拆解、ETF 代码格式）都不用改这里。派生列 `ETF_DISPERSION` 用的是耦合层
给的**分组份额**（`etf_sse50/etf_csi300/etf_csi1000`），所以连 `series` 都不需要了。
"""

from __future__ import annotations

import sys

import pandas as pd

from shared.adapter import Panel, column_of, get_data
# 转出口：外部脚本 / 测试还在 `from ...panel import bare_code`（归一逻辑已上移到 adapter）
from shared.adapter import bare_code  # noqa: F401

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

# 耦合层给的分组份额列（组名见 `shared/adapter` 的 _ETF_GROUPS）
ETF_GROUP_COLS = ("etf_sse50", "etf_csi300", "etf_csi1000")


def _etf_dispersion(base: pd.DataFrame) -> pd.Series:
    """宽基 ETF 申赎的分歧度：各组份额变化的截面标准差。

    输入是**分组份额**（原生观测日、不 ffill），所以先在 `.dropna()` 上算
    `pct_change`，再把结果 ffill 回交易日——和派生口径一致。
    组数不足就返回全 NaN **并告警**：整列静默变 NaN 会让 HMM 一行都不认领。
    """
    parts = [base[c].dropna().pct_change() for c in ETF_GROUP_COLS
             if c in base.columns and base[c].notna().any()]
    if len(parts) < 2:
        print(f"[面板] ETF 分组份额只有 {len(parts)} 组可用，ETF_DISPERSION 整列 NaN",
              file=sys.stderr)
        return pd.Series(index=base.index, dtype=float)
    return pd.concat(parts, axis=1).std(axis=1).reindex(base.index, method="ffill")


def panel(series=None, meta=None) -> pd.DataFrame:
    """基础面板来自耦合层；`series` / `meta` 只为兼容现有调用点保留（已不需要）。"""
    base = get_data(Panel.STATE_ASHARE)
    df = pd.DataFrame({out: base[column_of(spec)] for out, spec in COLS.items()},
                      index=base.index)
    df["ETF_DISPERSION"] = _etf_dispersion(base)
    return df
