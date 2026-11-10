"""状态估计器协议。

换模型只换这里的实现，不改上层：
- `hmm.py`    常规高斯 HMM（第一个实现）
- 将来可以加 `gmm_hmm.py` / `student_t.py` / `hsmm.py`

三个方法就够：
    fit(features)        → 训练
    labels(features)     → 逐日标签（批量）
    latest(features)     → 当前后验（单点）
"""

from __future__ import annotations

from typing import Any, Protocol

import pandas as pd


class StateEstimator(Protocol):
    name: str

    def fit(self, features: pd.DataFrame) -> Any: ...

    def labels(self, features: pd.DataFrame) -> pd.Series: ...

    def latest(self, features: pd.DataFrame) -> dict: ...
