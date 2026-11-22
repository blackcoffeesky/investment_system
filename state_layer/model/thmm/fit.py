"""Student-t HMM 的 EM 训练。

接口约定（来自 `tests/state_layer/test_thmm.py` 那 4 条测试，**测试是规格**）：

    fit_t_hmm(x, k, nu=5.0)  ← 可以不传 init
    返回 dict，至少含 pi / a / means / covars / nu / true_cov / hist / iters

`hist` 是**每轮的观测对数似然**，`iters` 是实际迭代轮数——"EM 单调性"这个证据
必须留在生产代码里（不能为了移植把它丢掉）。

与 `experiment/hmm_state/hmm_core.py` 的差别只有三处：`init` 可省（默认 KMeans）、
返回 `hist`/`iters`、`_chol` 不再回传 fixed 标记。
"""

from __future__ import annotations

import numpy as np

from state_layer.model.thmm.numerics import (
    _chol, forward_backward, t_logpdf,
)

DEFAULTS = {"nu": 5.0, "max_iter": 400, "rtol": 1e-7}


def default_init(x: np.ndarray, k: int, seed: int = 42) -> dict:
    """KMeans 初始化：均值取簇心，协方差取簇内协方差，转移矩阵取"粘性"对角。

    比"随机挑 k 个点"稳：t-EM 对初值敏感，簇心至少让每个状态一开始就覆盖一段数据。
    """
    from sklearn.cluster import KMeans

    n, d = x.shape
    km = KMeans(n_clusters=k, n_init=10, random_state=seed).fit(x)
    means = np.asarray(km.cluster_centers_, dtype=float)
    covars = np.empty((k, d, d))
    for i in range(k):
        sub = x[km.labels_ == i]
        if len(sub) < 2:
            covars[i] = np.cov(x.T) + np.eye(d) * 1e-6
        else:
            covars[i] = np.cov(sub.T) + np.eye(d) * 1e-6
    pi = np.full(k, 1.0 / k)
    a = np.full((k, k), 0.1 / max(1, k - 1))
    np.fill_diagonal(a, 0.9)
    return {"means": means, "covars": covars, "pi": pi, "a": a}


def fit_t_hmm(x: np.ndarray, k: int, init: dict | None = None,
              nu: float = DEFAULTS["nu"],
              max_iter: int = DEFAULTS["max_iter"],
              rtol: float = DEFAULTS["rtol"]) -> dict:
    """EM 拟合多元 Student-t HMM。

    `covars` 是**尺度矩阵** Σ（不是协方差）；`true_cov = Σ · ν/(ν-2)`。
    """
    n, d = x.shape
    if init is None:
        init = default_init(x, k)

    means = np.copy(init["means"])
    covars = np.copy(init["covars"]) * ((nu - 2.0) / nu if nu > 2 else 1.0)
    pi = np.copy(init["pi"])
    a = np.copy(init["a"])

    hist: list[float] = []
    last_ll = -np.inf
    it = -1
    for it in range(max_iter):
        log_b = np.column_stack(
            [t_logpdf(x, means[i], covars[i], nu) for i in range(k)])
        gamma, xi, ll = forward_backward(log_b, a, pi)
        hist.append(float(ll))

        u = np.empty_like(gamma)
        for i in range(k):
            cf, _ = _chol(covars[i])
            diff = x - means[i]
            from scipy.linalg import cho_solve
            delta = np.einsum("ij,ij->i", diff, cho_solve(cf, diff.T).T)
            u[:, i] = (nu + d) / (nu + delta)

        w = gamma * u
        wsum = w.sum(axis=0)
        means = (w.T @ x) / wsum[:, None]

        gsum = np.clip(gamma.sum(axis=0), 1e-12, None)
        for i in range(k):
            diff = x - means[i]
            covars[i] = ((diff * w[:, i:i + 1]).T @ diff) / gsum[i] + np.eye(d) * 1e-8

        pi = gamma[0] / gamma[0].sum()
        a = xi / np.clip(xi.sum(axis=1, keepdims=True), 1e-300, None)

        if it > 0:
            rel = abs(ll - last_ll) / max(abs(last_ll), 1e-9)
            if rel < rtol:
                break
        last_ll = ll

    true_cov = covars * (nu / (nu - 2.0) if nu > 2 else np.nan)
    return {"pi": pi, "a": a, "means": means, "covars": covars, "nu": nu,
            "true_cov": true_cov, "hist": hist, "iters": it + 1}
