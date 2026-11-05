"""黄金专属的状态标签。

**枚举定义在这个资产里**，不进核心库——A 股用的是另一套语义
（RANGE/TREND_UP/TREND_DOWN/REBOUND/PANIC/DISTRIBUTION）。
"""

from __future__ import annotations

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
NAME_BY_VALUE: dict[str, str] = {l.value: NAME[l] for l in ORDER}


def name_states(means: pd.DataFrame, allowed: tuple[str, ...] | list[str] | None = None) -> dict[int, str]:
    """按「收益排序切三段 + 组内按波动排序」给状态贴标签。

    阈值不再是硬编码的 ±2e-4，而是按状态收益排序切三段
    （`regime_engine.align.three_way_groups`）：K≥3 时三组都非空，
    K=2 只分低/高两组。波动列自动识别，不硬编码列名。

    ``allowed`` 限定可用标签集合：先由全样本模型定词表，再逐窗命名。
    """
    from regime_engine.align import name_states as _name

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
