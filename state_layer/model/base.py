"""模型基类：状态模型的统一契约。

三个方法 + 两个序列化钩子
------------------------
    fit(features)      → 训练，返回自身
    infer(features)    → {states, proba}，out-of-sample
    state_params()     → 纯数组字典，供 `state_layer.metrics` 评价
    to_dict() / from_dict()  → 可 JSON 序列化的参数

契约（必须遵守，否则下游会静默出错）
-----------------------------------
1. **fit/infer 不得前视**：`infer` 在历史某日给出的状态，只能用到那日及之前的数据。
   HMM 的做法是 walk-forward 逐窗重估；换模型时也必须保持。
2. **to_dict 必须可 JSON 序列化**（numpy → list）。不要存 pickle 对象，
   类名一变旧模型就加载不了。
3. **from_dict(to_dict(m)) 的 infer 结果必须逐位相同**——有 roundtrip 测试守着。
4. 模型**不评价自己**：好坏由 `metrics.py` 用 `state_params()` 算。

模型不知道文件、不知道资产、不知道标签文字。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd


class Model(ABC):
    kind: str = "base"

    # ---------------- 必选
    @abstractmethod
    def fit(self, features: pd.DataFrame, **kwargs) -> "Model":
        """在特征上训练。特征必须已按时间升序、无空行。"""

    @abstractmethod
    def infer(self, features: pd.DataFrame) -> dict[str, Any]:
        """返回 {"states": Series, "proba": DataFrame}，逐日为样本外结果。"""

    @abstractmethod
    def state_params(self) -> dict[str, Any]:
        """纯数组字典（means / covars / transmat …），供评价函数消费。"""

    # ---------------- 序列化
    @abstractmethod
    def to_dict(self) -> dict[str, Any]:
        """JSON 可序列化的参数。"""

    @classmethod
    @abstractmethod
    def from_dict(cls, data: dict[str, Any]) -> "Model":
        """从 to_dict 的产物重建等价模型。"""

    # ---------------- 可选钩子
    def transition(self, features: pd.DataFrame) -> dict | None:
        """状态黏性：留在原状态的概率、平均停留、最可能转向。

        只有离散马尔可夫类模型有这个概念；其余返回 None。
        """
        return None

    def summary(self) -> str:  # pragma: no cover - 仅调试用
        return f"<{self.kind}>"
