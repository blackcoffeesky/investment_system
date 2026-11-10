"""A 股状态层配置。

P1 阶段：这里是 `causual/causual_assets/ashare/config.py` 与 `pipeline.py`
相关常量的**副本**，目的是让状态层先独立跑起来。P3 会删掉因果层那份，
届时本文件成为唯一来源。

`tests/test_state_layer.py` 有一条测试守着两边一致，防止 P1~P3 期间漂移。
"""

from __future__ import annotations

from regime_engine.extreme import ExtremeParams

ASSET = "ashare"
TITLE = "中证全指"

# 状态数：None 表示按 BIC 自动选（现有实现选出来是 3）
K = 3

# 状态识别用的特征（顺序即 HMM 输入顺序）
FEATURES = [
    "log_return",
    "realized_vol_20",
    "volume_z",
    "up_day_ratio",
    "size_mom",
    "margin_chg_z",
    "rv_ratio",
    "shibor_chg_z",
    "etf_dispersion",
]

# 极端行情判定（资产级覆盖）
EXTREME_PARAMS = ExtremeParams(
    ret_col="log_return",
    vol_col="realized_vol_20",
    sigma=3.0,
    pct=0.99,
    exit_days=1,
)

# 标签 → 展示名
LABEL_NAME = {
    "trend_up": "趋势上行",
    "range": "震荡",
    "trend_down": "趋势下行",
    "extreme_up": "极端上行",
    "extreme_down": "极端下行",
    "extreme_vol": "极端波动",
}
