"""极端行情后处理：规则判定，覆盖统计模型给出的标签。

为什么放在 `postprocessors/` 而不是 `inference.py`
------------------------------------------------
inference 是标准流程（特征 → 估计 → 输出），对任何模型都一样；
极端规则是**可选的覆盖层**，和 HMM 没有理论关系。放在这里，
将来换 HSMM / Student-t 也能直接复用，不用改 inference。

现在只是包一层 `regime_engine.extreme`；P3 可以把实现搬进来。
"""

from __future__ import annotations

import pandas as pd

from regime_engine import extreme as X


def classify(features: pd.DataFrame, params: X.ExtremeParams | None) -> pd.Series:
    """返回逐日极端标签（非极端日为 ""）。"""
    return X.classify(features, params)


def apply(labels: pd.Series, extreme: pd.Series) -> pd.Series:
    """用极端标签覆盖统计标签。"""
    out = labels.copy()
    mask = extreme != ""
    out.loc[mask] = extreme[mask]
    return out
