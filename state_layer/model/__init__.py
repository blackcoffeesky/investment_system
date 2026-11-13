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


def run_infer(models: list[Model], features) -> dict:
    """按链依次推断：上一环交出的日期才给下一环。

    这是"推断"和"拟合"口径一致的保证——fit 时 hmm 只见过非极端日，
    infer 时也必须只喂非极端日，否则 tail(window) 的分布就不是同一回事。
    """
    import pandas as pd

    remaining = features.index
    states: dict = {}
    proba = None
    for m in models:
        res = m.infer(features, claim=remaining)
        st = res.get("states")
        if st is not None and len(st):
            states.update(st.to_dict())
        if res.get("proba") is not None:
            proba = res["proba"]
        remaining = res.get("remaining_index", remaining)

    out = pd.Series(states, dtype=object)
    if len(out):
        out = out.sort_index()
    return {"states": out, "remaining_index": remaining, "proba": proba}


__all__ = ["Model", "GaussianHMM", "ExtremeModel", "MODELS", "make",
           "run_infer"]
