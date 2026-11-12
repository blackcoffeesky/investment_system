"""黄金状态层配置（P1 为 causual 侧常量的副本，P3 收归唯一来源）。"""

from __future__ import annotations

from state_layer.model.extreme import ExtremeParams

ASSET = "gold"
TITLE = "国内黄金"

# 现存黄金模型是 K=4（2026-10-19 拟合）。配置必须与线上一致，
# 否则 `state_layer.cli regime` 会用 K=3 悄悄换掉整个状态体系。
K = 4

# 评价用哪个指标、及格线多少。模型不评价自己，由 state_layer.metrics 算。
METRICS = {"separation": "bhattacharyya", "min_separation": 0.40}

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

# 面板构造函数（点号路径，运行时动态导入）。
# 状态层只声明"我需要一个面板"，具体怎么把原始表拼成面板是资产包的事。
PANEL_PROVIDER = "causual.causual_assets.gold.pipeline:load_raw"


# ======================================================================
# 状态标签词汇（原 labels/{asset}.py 并入）
# ======================================================================


import hashlib
from enum import Enum

import pandas as pd

RET_COL = "log_return"
VOL_COL = "realized_vol_5d"


class RegimeLabel(str, Enum):
    RANGE = "range"
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    DISTRIBUTION = "distribution"
    # 已移出常规 HMM（改由极端规则判定），保留枚举兼容旧配置
    RECOVERY = "recovery"
    STRESS = "stress"


ORDER: tuple[RegimeLabel, ...] = (
    RegimeLabel.RANGE, RegimeLabel.TREND_UP, RegimeLabel.TREND_DOWN,
    RegimeLabel.DISTRIBUTION,
)

REGULAR: tuple[str, ...] = tuple(l.value for l in ORDER)

NAME = {
    RegimeLabel.RANGE: "震荡",
    RegimeLabel.TREND_UP: "趋势上行",
    RegimeLabel.TREND_DOWN: "趋势下行",
    RegimeLabel.DISTRIBUTION: "高波派发",
    RegimeLabel.RECOVERY: "高波修复（已移出常规）",
    RegimeLabel.STRESS: "高波恐慌（已移出常规）",
}

EXTREME_NAMES: dict[str, str] = {
    "extreme_up": "极端上涨",
    "extreme_down": "极端下跌",
    "extreme_vol": "波动爆表",
}

VALUES: tuple[str, ...] = tuple(l.value for l in ORDER)
# 别名：因果层的 `labels_order` 用这个名字
ALL = VALUES
NAME_BY_VALUE: dict[str, str] = {l.value: NAME[l] for l in ORDER}


def name_states(means: pd.DataFrame, allowed: tuple[str, ...] | list[str] | None = None) -> dict[int, str]:
    """按「收益排序切三段 + 组内按波动排序」给状态贴标签。

    阈值不再是硬编码的 ±2e-4，而是按状态收益排序切三段
    （`state_layer.align.three_way_groups`）：K≥3 时三组都非空，
    K=2 只分低/高两组。波动列自动识别，不硬编码列名。

    ``allowed`` 限定可用标签集合：先由全样本模型定词表，再逐窗命名。
    """
    from state_layer.align import name_states as _name

    ok = set(allowed) if allowed else set(VALUES)

    def keep(pool):
        picked = [l for l in pool if l.value in ok]
        return picked or [l for l in ORDER if l.value in ok]

    pools = (
        keep([RegimeLabel.TREND_DOWN]),
        keep([RegimeLabel.TREND_UP, RegimeLabel.RANGE]),
        keep([RegimeLabel.RANGE, RegimeLabel.DISTRIBUTION]),
    )
    proto = {
        RegimeLabel.RANGE: 0.0, RegimeLabel.TREND_UP: -1.0, RegimeLabel.TREND_DOWN: -1.0,
        RegimeLabel.RECOVERY: 1.0, RegimeLabel.STRESS: 1.0, RegimeLabel.DISTRIBUTION: 1.0,
    }
    mapping, vcol = _name(
        means, ret_col=RET_COL, vol_col=VOL_COL, pools=pools,
        proto=proto, default=RegimeLabel.RANGE,
    )
    if vcol is None:
        import warnings

        warnings.warn(
            f"状态均值里找不到波动列（期望 {VOL_COL} 或 realized_vol*）",
            RuntimeWarning,
        )
    return mapping


def signature(used: tuple[str, ...] | list[str]) -> str:
    """只哈希"用到了哪些标签"。下游 pin 的就是这个。"""
    return hashlib.sha1(",".join(sorted(used)).encode("utf-8")).hexdigest()[:12]
