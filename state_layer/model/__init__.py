"""模型注册表：按名字取模型类。

加新模型 = 在 `model/<name>/` 实现 `Model` 协议，然后在这里登记一行。
链里用哪些、什么次序，由 `state_assets/<asset>/config.MODEL_CHAIN` 决定。
"""

from __future__ import annotations

from state_layer.model.base import Model
from state_layer.model.extreme import ExtremeModel
from state_layer.model.hmm import GaussianHMM

MODELS: dict[str, type[Model]] = {
    "hmm": GaussianHMM,
    "extreme": ExtremeModel,
}


def make(name: str, params: dict | None = None) -> Model:
    if name not in MODELS:
        raise KeyError(f"没有这个模型：{name}（可用：{sorted(MODELS)}）")
    return MODELS[name](params)


__all__ = ["Model", "GaussianHMM", "ExtremeModel", "MODELS", "make"]
