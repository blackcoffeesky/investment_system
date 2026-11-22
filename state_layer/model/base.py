"""模型协议：**统计模型**的契约（Gaussian HMM / Student-t HMM / 未来的 …）。

判据（2026-11-22 定）：**模型层不该知道有组，也不该知道资产。**
模型只认"特征矩阵 + K"：训练、逐日推断、交出纯数组供 metrics 评价、可序列化。

契约
----
1. **不得前视**：某日的结果只能用该日及之前的数据（`predict` 只用传入的行）。
2. `to_dict()` 必须可 JSON 序列化；`from_dict(to_dict(m))` 后 `predict` 逐位相同。
3. **模型不评价自己**：`state_params()` 只交纯数组，评价由 `metrics.py` 做。
4. `valid_index()` **只用于对齐**：声明"我能吃哪些行"，调用方（estimator）用它取各组日期交集。

链已经不存在（2026-11-22）：模型不再"认领日期 / 返回剩余"，`claim` / `remaining_index` 已删。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class ModelOutput:
    """一次推断的结果。`states` 是**组内编号**（int），标签由资产层映射。"""

    states: pd.Series
    proba: pd.DataFrame | None = None


class Model(ABC):
    kind: str = "base"

    def __init__(self, params: dict | None = None):
        self.params: dict[str, Any] = dict(params or {})
        self.states_: pd.Series | None = None      # fit 产出的逐日编号（int）
        self.labels_: pd.Series | None = None      # fit 产出的逐日标签（str，来自 labeler）
        self.proba_: pd.DataFrame | None = None
        self.last_window: dict | None = None       # 在线推断用（纯数组）

    # ---------------- 必选
    @abstractmethod
    def fit(self, features: pd.DataFrame, *, labeler=None) -> None:
        """训练。结果放 `states_` / `labels_` / `proba_` / `last_window`。

        `labeler` 是**资产注入**的命名回调：`raw_means(DataFrame) -> {编号: 标签}`。
        模型不自己决定"状态叫什么"——那是资产语义（按收益/波动排序）。
        """

    @abstractmethod
    def predict(self, features: pd.DataFrame) -> ModelOutput:
        """逐日推断（只用传入的行，不重训）。"""

    @abstractmethod
    def state_params(self) -> dict[str, Any] | None:
        """纯数组（means / covars / transmat / startprob），供 `metrics` 用。"""

    # ---------------- 序列化
    @abstractmethod
    def to_dict(self) -> dict[str, Any]:
        """JSON 可序列化。"""

    @classmethod
    @abstractmethod
    def from_dict(cls, data: dict[str, Any]) -> "Model":
        """从 `to_dict` 的产物重建。"""

    # ---------------- 能力声明
    def valid_index(self, features: pd.DataFrame) -> pd.Index:
        """**只用于对齐**：模型能处理哪些行（按自己用到的列判断非空）。

        与"交给下一个模型"无关——链已经不存在。
        """
        cols = [c for c in (self.params.get("features") or []) if c in features.columns]
        return features[cols].dropna().index if cols else features.dropna().index

    # ---------------- 可选钩子
    def transition(self, features: pd.DataFrame | None = None) -> dict | None:
        """状态黏性。只有离散马尔可夫类模型有意义。"""
        return None

    def summary(self) -> str:  # pragma: no cover
        return f"<{self.kind}>"


def to_numpy(obj):
    """把 numpy / pandas 递归转成**可 JSON / 可 pickle 的纯 Python 结构**。

    模型参数要落盘（`store`）又要能进 `spec.json`，所以序列化前统一过这一层：
    ndarray → list、numpy 标量 → 内置标量、Series → list。
    """
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    if isinstance(obj, pd.Series):
        return obj.tolist()
    if isinstance(obj, dict):
        return {k: to_numpy(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_numpy(x) for x in obj]
    return obj
