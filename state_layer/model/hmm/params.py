"""HMM 参数 → `hmmlearn` 对象的重建（**只重建，不读写文件**）。

为什么不用 pickle 直接倒模型对象：hmmlearn / sklearn 的类结构在小版本之间也会变，
pickle 反序列化经常直接报错或静默改变行为。所以产物里只存纯数组，加载时重建一个等价对象。
（文件 I/O 在 `state_layer/store.py`，这里不碰盘。）
"""

from __future__ import annotations

import numpy as np


def rebuild(spec: dict):
    """从参数字典重建 `GaussianHMM`（用于在线算后验）。

    `spec` 的字段与 `RegimeFit.spec()` / `window_dict()` 一致：
    `means` / `covars` / `transmat` / `startprob`（+ 可选 `k`）。
    """
    from hmmlearn.hmm import GaussianHMM

    # 老的"最后窗口"文件里没有 k，从 means 的行数推
    k = int(spec.get("k") or len(np.asarray(spec["means"])))
    hmm = GaussianHMM(n_components=k, covariance_type="full")
    hmm.means_ = np.asarray(spec["means"], dtype=float)
    hmm.covars_ = np.asarray(spec["covars"], dtype=float)
    hmm.transmat_ = np.asarray(spec["transmat"], dtype=float)
    hmm.startprob_ = np.asarray(spec["startprob"], dtype=float)
    hmm.n_features = hmm.means_.shape[1]
    return hmm
