"""A 股状态层配置。

P1 阶段：这里是 `causual/causual_assets/ashare/config.py` 与 `pipeline.py`
相关常量的**副本**，目的是让状态层先独立跑起来。P3 会删掉因果层那份，
届时本文件成为唯一来源。

`tests/test_state_layer.py` 有一条测试守着两边一致，防止 P1~P3 期间漂移。
"""

from __future__ import annotations

from state_layer.model.extreme import ExtremeParams

ASSET = "ashare"
TITLE = "中证全指"

# 状态数：None 表示按 BIC 自动选（现有实现选出来是 3）

# ---------------- 模型链（顺序即处理次序）
# 每个模型只认领一部分日期，没认领的交给下一个；
# 最后一个模型通常认领"剩下的全部"。
MODEL_CHAIN = ("extreme", "hmm")
MODEL_PARAMS = {
    "extreme": dict(
        ret_col="log_return", vol_col="realized_vol_20", sigma=3.0,
        pct=0.99, abs_cap=0.05,
    ),
    "hmm": {"k": 3},          # k=None 表示让模型自己按 BIC+分离度选
}

# 评价用哪个指标、及格线多少。模型不评价自己，由 state_layer.metrics 算。
METRICS = {"separation": "bhattacharyya", "min_separation": 0.40}

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

# 面板构造函数（点号路径，运行时动态导入）。
# 状态层只声明"我需要一个面板"，具体怎么把原始表拼成面板是资产包的事。
PANEL_PROVIDER = "causual.causual_assets.ashare.pipeline:load_raw"


# ======================================================================
# 状态标签词汇（原 labels/{asset}.py 并入）
# ======================================================================


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

# 常规 HMM 只产出这四个；极端日由 state_layer.model.extreme 的规则给出 EXTREME_*
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
# 别名：因果层的 `labels_order` 用这个名字
ALL = VALUES
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
    `state_layer.align.three_way_groups`）；波动列也自动识别（A 股是 20 日）。
    """
    from state_layer.align import name_states as _name

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
