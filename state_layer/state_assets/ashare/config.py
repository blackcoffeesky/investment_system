"""A 股状态层配置。

P1 阶段：这里是 `causual/causual_assets/ashare/config.py` 与 `pipeline.py`
相关常量的**副本**，目的是让状态层先独立跑起来。P3 会删掉因果层那份，
届时本文件成为唯一来源。

`tests/test_state_layer.py` 有一条测试守着两边一致，防止 P1~P3 期间漂移。
"""

from __future__ import annotations

ASSET = "ashare"
TITLE = "中证全指"

# ---------------- 分组（2026-11-22 重构）
# 组名是稳定 ASCII（落盘/接口用）；`PRIMARY_GROUP` 给下游（causual 只取一组）。
PRIMARY_GROUP = "short_price"
# 多分组能力已具备，**当前只启用一组**（用户 2026-11-22：A 股状态数就是 3）。
# 将来加 short_vol / long_price：往下面的 dict 加 key，再把名字放进这里。
GROUPS_ENABLED = ("short_price",)

GROUP_TITLE = {"short_price": "短期价格", "short_vol": "短期波动",
               "long_price": "长期价格"}

# 每组的状态数（实验定下来的：3 / 3 / 2）
K = {"short_price": 3, "short_vol": 3, "long_price": 2}

# 每组用哪个模型：价格用 Student-t（厚尾），波动用 Gaussian
GROUP_MODEL = {"short_price": "thmm", "short_vol": "hmm", "long_price": "thmm"}

# 每组状态排序依据（越大 = 越强 / 风险越高）
ORDER_COL = {"short_price": "ret_10", "short_vol": "rv_5",
             "long_price": "ret_120"}

# 每组的特征列（顺序即模型输入顺序；与 features.build_groups 一一对应）
GROUP_FEATURES = {
    "short_price": ("ret_5", "ret_10", "er_10", "snr_10"),
    "short_vol": ("rv_5", "atr_5", "gk_10", "abs_ret_5"),
    "long_price": ("ret_60", "ret_120", "er_60", "er_120", "snr_60"),
}

# 每组的标签词表（按序位从小到大）。primary 组沿用旧四词表里的三个。
GROUP_LABELS = {
    "short_price": ("trend_down", "range", "trend_up"),
    "short_vol": ("low", "normal", "high"),
    "long_price": ("weak", "strong"),
}

# 各组特征的预热交易日数（由 features 里的 min_periods 决定，不是拍脑袋）：
#   短价格 ret_10/er_10=10、snr_10 min_periods=5 → 10
#   短波动 rv_5/abs_ret_5=3、atr_5=3+1shift、gk_10 min_periods=5 → 5
#   长价格 ret_120/er_120=120、snr_60 min_periods=30 → 120
WARMUP_BARS = {"short_price": 10, "short_vol": 5, "long_price": 120}

# walk-forward 参数（**只在这里**；代码里不写死）。用户 2026-11-22 定的默认值。
WALK_FORWARD = {"min_train": 250, "refit_every": 60, "window": 250}

# 评价用哪个指标、及格线多少。模型不评价自己，由 state_layer.metrics 算。
# 注意：分离度只作参考，**不作门槛**（硬约束是占比/持续/ES5 三件套）。
METRICS = {"separation": "bhattacharyya", "min_separation": 0.40}

# 状态识别用的特征（顺序即 HMM 输入顺序）
# 旧特征（log_return / vol20 / volume_z / ma20_slope / size_mom / margin_chg_z /
# rv_ratio / shibor_chg_z / etf_dispersion）已于 2026-11-22 退役。
# 特征现在按**分组**定义（GROUP_FEATURES）；`FEATURES` 只是"启用组"的扁平视图，
# 给按列名遍历的地方（observations / 新鲜度测试）用。
FEATURES = ["ret_5", "ret_10", "er_10", "snr_10"]

# 标签 → 展示名
LABEL_NAME = {
    # code 不变（进签名 / 因果层枚举），只改展示名：下跌 / 震荡 / 上涨
    "trend_down": "下跌",
    "range": "震荡",
    "trend_up": "上涨",
}

# 本资产要哪些表（**不**描述面板怎么拼——面板由 `shared.adapter` 统一提供）。
# `get_data` 自己按 adapter 的声明取数；这份是给 `port.required_tables()` 用的装配
# 清单，`tests/state_layer/test_state_features_fresh.py` 钉住它 == `tables_of(Panel.STATE_ASHARE)`。
REQUIRED_TABLES = (
    "csi_all",          # OHLCV 主价格（特征 + 成交额/换手展示）
    "csi300",
    "csi1000",
    # 下面三张表**当前面板仍在取**（`Panel.STATE_ASHARE` 还是 `ashare_base`）。
    # 面板瘦身（只留 OHLC + 成交/换手）需要 adapter 里一个独立的 builder 函数，
    # 尚未做——**声明必须与实现一致**，所以先留着；瘦身时一起删。
    "margin_balance",
    "shibor3m",
    "etf_shares",
)


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


# A 股只有 3 个状态（用户 2026-11-22：去掉极端后就是 3 个）。
# `distribution` 只属于黄金的 K=4，已从本资产词表移除。
ORDER: tuple[RegimeLabel, ...] = (
    RegimeLabel.RANGE, RegimeLabel.TREND_UP, RegimeLabel.TREND_DOWN,
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

# 极端标签已于 2026-11-22 随 extreme 模型一起移除（极端行情由 t 分布厚尾
# 与短波动的 high 档表达），`EXTREME_NAMES` 不再存在。

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


def signature(used: tuple[str, ...] | list[str]) -> str:
    return hashlib.sha1(",".join(sorted(used)).encode("utf-8")).hexdigest()[:12]
