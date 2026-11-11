"""两个高斯分布之间的 Bhattacharyya 距离，用于验证 HMM 状态是否真的可分。

距离越大越可分；工程上用 0.4 作为下限（见 PROBLEM / CHANGES 里的取值理由）。
"""

from __future__ import annotations

from itertools import combinations

import numpy as np
from scipy.linalg import sqrtm  # noqa: F401  (保留给需要矩阵平方根的场景)


def bhattacharyya_distance(mu1, sigma1, mu2, sigma2) -> float:
    """闭式解：D = 1/8 (μ1−μ2)ᵀ Σ⁻¹ (μ1−μ2) + 1/2 ln(detΣ / sqrt(detΣ1 detΣ2))。"""
    mu1 = np.asarray(mu1, dtype=float).ravel()
    mu2 = np.asarray(mu2, dtype=float).ravel()
    s1 = np.atleast_2d(np.asarray(sigma1, dtype=float))
    s2 = np.atleast_2d(np.asarray(sigma2, dtype=float))

    s_avg = (s1 + s2) / 2.0
    diff = mu1 - mu2
    term1 = (1.0 / 8.0) * diff @ np.linalg.inv(s_avg) @ diff

    eps = 1e-12
    d_avg = max(float(np.linalg.det(s_avg)), eps)
    d1 = max(float(np.linalg.det(s1)), eps)
    d2 = max(float(np.linalg.det(s2)), eps)
    term2 = 0.5 * np.log(d_avg / np.sqrt(d1 * d2))
    return float(term1 + term2)


def pairwise(means, covars, threshold: float = 0.4) -> dict:
    """两两距离矩阵 + 最小非对角距离 + 是否全部通过阈值。"""
    means = np.asarray(means, dtype=float)
    covars = np.asarray(covars, dtype=float)
    k = len(means)

    dist = np.zeros((k, k))
    for i, j in combinations(range(k), 2):
        d = bhattacharyya_distance(means[i], covars[i], means[j], covars[j])
        dist[i, j] = dist[j, i] = d

    off = dist + np.eye(k) * 1e9
    idx = np.unravel_index(np.argmin(off), off.shape)
    min_dist = float(dist[idx])

    failed = [
        (i, j, float(dist[i, j]))
        for i, j in combinations(range(k), 2)
        if dist[i, j] < threshold
    ]
    return {
        "dist_matrix": dist,
        "min_dist": min_dist,
        "min_pair": (int(idx[0]), int(idx[1])),
        "threshold": threshold,
        "passed": len(failed) == 0,
        "failed_pairs": failed,
    }
