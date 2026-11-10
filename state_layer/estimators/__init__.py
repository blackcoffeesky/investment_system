"""状态估计器注册表。"""

from __future__ import annotations

from state_layer.estimators.hmm import HMMEstimator

ESTIMATORS = {"hmm": HMMEstimator}


def make(name: str = "hmm", **kwargs):
    if name not in ESTIMATORS:
        raise KeyError(f"没有这个估计器：{name}（可用：{sorted(ESTIMATORS)}）")
    return ESTIMATORS[name](**kwargs)
