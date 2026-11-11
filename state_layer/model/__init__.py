"""模型注册表：按名字取模型类。

加新模型（HSMM / Student-t HMM / GMM-HMM）= 在 `model/<family>/` 里实现
`Model` 协议，然后在这里登记一行。上层只认名字。
"""

from __future__ import annotations

from state_layer.model.base import Model
from state_layer.model.hmm import GaussianHMM

MODELS: dict[str, type[Model]] = {"hmm": GaussianHMM}


def make(name: str = "hmm", **kwargs) -> Model:
    if name not in MODELS:
        raise KeyError(f"没有这个模型：{name}（可用：{sorted(MODELS)}）")
    return MODELS[name](**kwargs)


__all__ = ["Model", "GaussianHMM", "MODELS", "make"]
