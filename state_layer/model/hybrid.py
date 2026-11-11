"""门控组合：统计模型 + 极端规则。

为什么是组合而不是继承
----------------------
"极端日走规则"是这套系统已经验证要长期保留的机制，但它和具体模型无关。
做成装饰器后，换 HSMM / Student-t 只要套同一层门控，规则自动生效。

    model = RuleGated(GaussianHMM(), params=config.EXTREME_PARAMS)
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from state_layer.model import extreme
from state_layer.model.base import Model


class RuleGated(Model):
    """内层模型负责常规行情，极端日由规则覆盖标签。"""

    kind = "rule_gated"

    def __init__(self, inner: Model, params=None):
        self.inner = inner
        self.params = params
        self.labels_: pd.Series | None = None

    # ---------------- 训练：极端日**不参与**内层拟合
    def fit(self, features: pd.DataFrame, **kwargs) -> "RuleGated":
        ext = extreme.classify(features, self.params)
        regular = features.loc[ext == ""]
        self.inner.fit(regular, **kwargs)

        inner_labels = getattr(self.inner, "labels_", None)
        if inner_labels is not None and len(inner_labels):
            # ext 是在**全量**特征上算的，内层标签只有常规日，
            # apply 之前必须对齐索引，否则布尔索引对不上。
            self.labels_ = extreme.apply(
                inner_labels, ext.reindex(inner_labels.index).fillna(""))
        return self

    def infer(self, features: pd.DataFrame) -> dict[str, Any]:
        res = self.inner.infer(features)
        ext = extreme.classify(features, self.params).reindex(res["states"].index)
        res["states"] = extreme.apply(res["states"], ext.fillna(""))
        return res

    def state_params(self) -> dict[str, Any]:
        return self.inner.state_params()

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "inner": self.inner.to_dict(),
                "params": extreme.to_dict(self.params)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuleGated":
        from state_layer.model import MODELS

        inner = MODELS[data["inner"]["kind"]].from_dict(data["inner"])
        return cls(inner, params=extreme.from_dict(data.get("params") or {}))

    def transition(self, features: pd.DataFrame | None = None):
        return self.inner.transition(features)
