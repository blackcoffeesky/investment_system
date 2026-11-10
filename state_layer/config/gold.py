"""黄金状态层配置（P1 为 causual 侧常量的副本，P3 收归唯一来源）。"""

from __future__ import annotations

from regime_engine.extreme import ExtremeParams

ASSET = "gold"
TITLE = "国内黄金"

K = 3

# 黄金与 A 股特征完全不同，各自实现
FEATURES = [
    "log_return",
    "realized_vol_5d",
    "range_pct",
    "vol_z",
    "usdcny_vol_5d",
    "vix_level",
    "mom_20",
    "rv_ratio",
    "gap",
]

EXTREME_PARAMS = ExtremeParams(
    ret_col="log_return",
    vol_col="realized_vol_5d",
    sigma=3.0,
    pct=0.99,
    exit_days=1,
)

LABEL_NAME = {
    "trend_up": "趋势上行",
    "range": "震荡",
    "trend_down": "趋势下行",
    "extreme_up": "极端上行",
    "extreme_down": "极端下行",
    "extreme_vol": "极端波动",
}
