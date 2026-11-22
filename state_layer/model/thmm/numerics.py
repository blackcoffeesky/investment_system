"""Student-t 分布的数值原语：对数密度、前向过滤、前向-后向。

来源：`experiment/hmm_state/hmm_core.py`（2026-11-22 随 t-HMM 进生产搬过来）。
**只处理数值，不知道任何资产语义**，也不碰盘。

为什么只输出**过滤**概率
------------------------
HMM 只用 `P(S_t | X_1:t)`。平滑概率（前向-后向）会用到 t 之后的数据，
在回测里就是未来函数；`forward_backward` 只用于**训练**（EM 的 E 步），
对外推断一律走 `filtered_predict`。
"""

from __future__ import annotations

import numpy as np
from scipy.linalg import cho_factor, cho_solve
from scipy.special import gammaln


def _chol(cov: np.ndarray):
    """Cholesky 分解，非正定时加 jitter 重试（最多 6 次）。"""
    jitter = 0.0
    for _ in range(6):
        try:
            cf = cho_factor(cov + np.eye(cov.shape[0]) * jitter, lower=True)
            logdet = 2.0 * np.sum(np.log(np.diag(cf[0])))
            return cf, logdet
        except np.linalg.LinAlgError:
            jitter = max(jitter * 10, 1e-8)
    raise np.linalg.LinAlgError("协方差非正定修复失败")


def gauss_logpdf(x: np.ndarray, mu: np.ndarray, cov: np.ndarray) -> np.ndarray:
    """多元高斯对数密度（x: n×d）。"""
    d = x.shape[1]
    c, logdet = _chol(cov)
    diff = x - mu
    sol = cho_solve(c, diff.T).T
    delta = np.einsum("ij,ij->i", diff, sol)
    return -0.5 * (d * np.log(2 * np.pi) + logdet + delta)


def t_logpdf(x: np.ndarray, mu: np.ndarray, cov: np.ndarray,
             nu: float) -> np.ndarray:
    """多元 Student-t 对数密度（`cov` 是**尺度矩阵** Σ，不是协方差）。"""
    d = x.shape[1]
    c, logdet = _chol(cov)
    diff = x - mu
    sol = cho_solve(c, diff.T).T
    delta = np.einsum("ij,ij->i", diff, sol)
    return (gammaln((nu + d) / 2) - gammaln(nu / 2)
            - 0.5 * d * np.log(nu * np.pi) - 0.5 * logdet
            - 0.5 * (nu + d) * np.log1p(delta / nu))


def log_sum_exp(a, axis=None):
    """稳定的 log-sum-exp。"""
    a = np.asarray(a, dtype=float)
    m = np.max(a, axis=axis, keepdims=True)
    m = np.where(np.isfinite(m), m, 0.0)
    out = np.log(np.sum(np.exp(a - m), axis=axis, keepdims=True)) + m
    if axis is None:
        return float(np.ravel(out)[0])
    return np.squeeze(out, axis=axis)


def filtered_predict(log_b: np.ndarray, transmat: np.ndarray,
                     prior0: np.ndarray):
    """前向**过滤**：只用到当日为止的观测。

    参数
    ----
    log_b : (n, k) 观测对数似然
    transmat : (k, k) 转移矩阵
    prior0 : (k,) 第一笔观测**之前**的状态分布

    返回
    ----
    post : (n, k) `P(S_t | X_1:t)`
    logpred : (n,) 每步的一步预测对数似然 `log P(X_t | X_1:t-1)`
    """
    n, k = log_b.shape
    post = np.empty((n, k))
    logpred = np.empty(n)
    prior = prior0 / max(prior0.sum(), 1e-300)
    for t in range(n):
        shift = float(np.max(log_b[t]))
        un = prior * np.exp(log_b[t] - shift)
        s = un.sum()
        logpred[t] = np.log(max(s, 1e-300)) + shift
        post[t] = un / s
        prior = post[t] @ transmat
    return post, logpred


def forward_backward(log_b: np.ndarray, a: np.ndarray, pi: np.ndarray):
    """**训练**用前向-后向（EM 的 E 步）。返回 gamma、xi_sum、loglik。

    ⚠️ 只给训练用：`gamma` 用到了整段观测，不能拿去当在线状态。
    """
    n, k = log_b.shape
    la = np.log(np.clip(a, 1e-300, None))
    lpi = np.log(np.clip(pi, 1e-300, None))

    la_f = np.empty((n, k))
    la_f[0] = lpi + log_b[0]
    c = log_sum_exp(la_f[0])
    la_f[0] -= c
    loglik = c
    for t in range(1, n):
        cur = log_sum_exp(la_f[t - 1][:, None] + la, axis=0) + log_b[t]
        c = log_sum_exp(cur)
        la_f[t] = cur - c
        loglik += c

    la_b = np.zeros((n, k))
    for t in range(n - 2, -1, -1):
        cur = log_sum_exp(la + log_b[t + 1] + la_b[t + 1][None, :], axis=1)
        la_b[t] = cur - log_sum_exp(cur)

    gamma = la_f + la_b
    gamma -= log_sum_exp(gamma, axis=1)[:, None]
    gamma = np.exp(gamma)

    xi = np.zeros((k, k))
    for t in range(n - 1):
        m = la_f[t][:, None] + la + (log_b[t + 1] + la_b[t + 1])[None, :]
        m -= log_sum_exp(m.reshape(-1))
        xi += np.exp(m)
    return gamma, xi, loglik
