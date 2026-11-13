"""极端行情模型：用规则认领"不寻常"的日子，剩下的交给统计模型。

它和 HMM 无关——换成 HSMM 或 Student-t，这一环原样复用。

为什么极端不用统计模型
--------------------
极端样本通常 <1%，任何统计模型都会把它并进相邻状态；即使分出来，
均值也会被少量样本拖得不稳。极端行情的定义本身有很强的业务直觉
（暴跌、暴涨、波动爆表），规则更直接、更可解释。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from state_layer.model.base import Model, resolve_claim
from state_layer.model.extreme import rules
from state_layer.model.extreme.params import ExtremeParams


def _as_params(raw: dict | ExtremeParams | None) -> ExtremeParams:
    if isinstance(raw, ExtremeParams):
        return raw
    if not raw:
        return ExtremeParams()
    allowed = set(ExtremeParams.__dataclass_fields__)
    return ExtremeParams(**{k: v for k, v in raw.items() if k in allowed})


class ExtremeModel(Model):
    kind = "extreme"

    def __init__(self, params: dict | ExtremeParams | None = None):
        p = _as_params(params)
        super().__init__(rules.to_dict(p))
        self.params_obj = p

    # ---------------- 能力：只关心 ret_col / vol_col
    def valid_index(self, features: pd.DataFrame) -> pd.Index:
        cols = [c for c in (self.params_obj.ret_col, self.params_obj.vol_col)
                if c in features.columns]
        return features[cols].dropna().index if cols else features.index

    # ---------------- 训练：只认领极端日
    def fit(self, features: pd.DataFrame, *, claim: pd.Index | None = None,
            **kw) -> pd.Index:
        allowed = resolve_claim(self, features, claim)
        ext = rules.classify(features, self.params_obj)
        claimed = allowed.intersection(ext[ext != ""].index)
        self.labels_ = ext.reindex(claimed).astype(str)
        return allowed.difference(claimed)

    # ---------------- 推断
    def infer(self, features: pd.DataFrame, *,
              claim: pd.Index | None = None) -> dict:
        allowed = resolve_claim(self, features, claim)
        ext = rules.classify(features, self.params_obj)
        claimed = allowed.intersection(ext[ext != ""].index)
        states = ext.reindex(claimed).astype(str)
        return {
            "states": states,
            "remaining_index": allowed.difference(claimed),
            "proba": None,
        }

    def state_params(self) -> dict[str, Any] | None:
        return None          # 没有概率分布，评价函数跳过它

    # ---------------- 序列化
    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "params": dict(self.params)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExtremeModel":
        return cls(data.get("params") or {})
