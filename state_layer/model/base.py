"""模型协议：一条**模型链**里的每一环都要满足这个契约。

链的语义
--------
`MODEL_CHAIN = ("extreme", "hmm")` 表示处理次序。每个模型只认领一部分日期：

    features ──► extreme: 认领极端日，返回剩下没认领的
                        │
                        ▼
                 hmm: 认领其余全部，返回空

`fit` / `infer` 都返回**没认领的日期**，外部循环把它喂给下一个模型。
`claim` 参数限定"这一环只准在这些日子上下判断"——传全量特征是为了滚动窗口
能正常算，不是为了让模型越界认领。

契约（必须遵守）
---------------
1. **不得前视**：某日的结果只能用该日及之前的数据。
2. `to_dict()` 必须可 JSON 序列化。
3. `from_dict(to_dict(m))` 后 `infer` 结果逐位相同。
4. 模型**不评价自己**：`state_params()` 只交出纯数组，评价由 `metrics.py` 做。
   规则类模型（如 extreme）没有概率分布，返回 None。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd


class Model(ABC):
    kind: str = "base"

    def __init__(self, params: dict | None = None):
        self.params: dict[str, Any] = dict(params or {})
        self.labels_: pd.Series | None = None
        # 统计模型的"最后窗口"（在线算后验用）；规则模型为 None
        self.last_window: dict | None = None

    # ---------------- 必选
    @abstractmethod
    def fit(self, features: pd.DataFrame, *, claim: pd.Index | None = None,
            **kw) -> pd.Index:
        """训练。返回**没认领**的日期（交给下游）。认领结果放 `self.labels_`。"""

    @abstractmethod
    def infer(self, features: pd.DataFrame, *,
              claim: pd.Index | None = None) -> dict:
        """返回 {states: Series(只含认领日期), remaining_index: Index, proba: DataFrame|None}。"""

    @abstractmethod
    def state_params(self) -> dict[str, Any] | None:
        """纯数组字典（means / covars / transmat），规则类模型返回 None。"""

    # ---------------- 序列化
    @abstractmethod
    def to_dict(self) -> dict[str, Any]:
        """JSON 可序列化。"""

    @classmethod
    @abstractmethod
    def from_dict(cls, data: dict[str, Any]) -> "Model":
        """从 to_dict 的产物重建。"""

    # ---------------- 可选钩子
    def transition(self, features: pd.DataFrame | None = None) -> dict | None:
        """状态黏性。只有离散马尔可夫类模型有意义。"""
        return None

    def summary(self) -> str:  # pragma: no cover
        return f"<{self.kind}>"


def resolve_claim(features: pd.DataFrame, claim: pd.Index | None) -> pd.Index:
    """这一环允许认领的日期（默认全部有效行）。"""
    valid = features.dropna().index
    if claim is None:
        return valid
    return valid.intersection(claim)
