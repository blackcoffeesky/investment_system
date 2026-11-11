"""A 股专属的状态标签。

与黄金**不共用枚举**：A 股的"高波派发"更接近"放量滞涨"，"反弹"也比黄金的"修复"
更依赖政策面，语义不同就不该复用同一个类。
"""

from __future__ import annotations

import hashlib
from enum import Enum

import pandas as pd

RET_COL = "log_return"
VOL_COL = "realized_vol_20"


class RegimeLabel(str, Enum):
    RANGE = "range"
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    DISTRIBUTION = "distribution"
    # 下面两个已移出常规 HMM（改由极端规则判定），保留枚举是为了兼容旧配置
    REBOUND = "rebound"
    PANIC = "panic"


ORDER: tuple[RegimeLabel, ...] = (
    RegimeLabel.RANGE, RegimeLabel.TREND_UP, RegimeLabel.TREND_DOWN,
    RegimeLabel.DISTRIBUTION,
)

# 常规 HMM 只产出这四个；极端日由 state_layer.engine.extreme 的规则给出 EXTREME_*
REGULAR: tuple[str, ...] = tuple(l.value for l in ORDER)

NAME = {
    RegimeLabel.RANGE: "窄幅震荡",
    RegimeLabel.TREND_UP: "趋势上行",
    RegimeLabel.TREND_DOWN: "趋势下行",
    RegimeLabel.DISTRIBUTION: "放量滞涨",
    RegimeLabel.REBOUND: "超跌反弹（已移出常规）",
    RegimeLabel.PANIC: "恐慌杀跌（已移出常规）",
}

# 极端标签的中文名（展示用；它们不由 HMM 产生）
EXTREME_NAMES: dict[str, str] = {
    "extreme_up": "极端上涨",
    "extreme_down": "极端下跌",
    "extreme_vol": "波动爆表",
}

VALUES: tuple[str, ...] = tuple(l.value for l in ORDER)
NAME_BY_VALUE: dict[str, str] = {l.value: NAME[l] for l in ORDER}

# 原型：(收益符号, 相对波动高低)。用于组内排序。
PROTO = {
    RegimeLabel.RANGE: 0.0,
    RegimeLabel.TREND_UP: -1.0,
    RegimeLabel.TREND_DOWN: -1.0,
    RegimeLabel.REBOUND: 1.0,
    RegimeLabel.PANIC: 1.0,
    RegimeLabel.DISTRIBUTION: 1.0,
}


def name_states(means: pd.DataFrame, allowed: tuple[str, ...] | list[str] | None = None) -> dict[int, str]:
    """按「收益排序切三段 + 组内按波动排序」命名。

    阈值不再是硬编码的 ±2e-4，而是**按状态收益排序切三段**（见
    `state_layer.engine.align.three_way_groups`）；波动列也自动识别（A 股是 20 日）。
    """
    from state_layer.engine.align import name_states as _name

    ok = set(allowed) if allowed else set(VALUES)

    def keep(pool):
        picked = [l for l in pool if l.value in ok]
        return picked or [l for l in ORDER if l.value in ok]

    pools = (
        keep([RegimeLabel.TREND_DOWN]),                            # 最弱的一段
        keep([RegimeLabel.TREND_UP, RegimeLabel.RANGE]),           # 最强的一段
        keep([RegimeLabel.RANGE, RegimeLabel.DISTRIBUTION]),       # 中间（低波→震荡，高波→派发）
    )
    mapping, vcol = _name(
        means, ret_col=RET_COL, vol_col=VOL_COL, pools=pools,
        proto=PROTO, default=RegimeLabel.RANGE,
    )
    if vcol is None:
        import warnings

        warnings.warn(
            f"状态均值里找不到波动列（期望 {VOL_COL} 或 realized_vol*）："
            "trend/distribution 会退化为按状态编号分配",
            RuntimeWarning,
        )
    return mapping


def signature(used: tuple[str, ...] | list[str]) -> str:
    return hashlib.sha1(",".join(sorted(used)).encode("utf-8")).hexdigest()[:12]
