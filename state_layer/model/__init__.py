"""模型包：**统计模型**的协议与实现。

- `base.py`：`Model` 协议 + `ModelOutput`（模型层不知道"组"、不知道资产）
- `hmm/`：Gaussian HMM（短波动用；黄金仍用它）
- `thmm/`：Student-t HMM（A 股价格维度用）

链已经退役（2026-11-22）：没有 `MODELS` 注册表、没有 `run_infer`。
谁用哪个模型、怎么组合，由 `state_assets/<asset>/estimator.py` 决定。
"""

from __future__ import annotations

from state_layer.model.base import Model, ModelOutput, to_numpy
from state_layer.model.hmm import GaussianHMM
from state_layer.model.thmm import StudentTHMM

__all__ = ["Model", "ModelOutput", "to_numpy", "GaussianHMM", "StudentTHMM"]
