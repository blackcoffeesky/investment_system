"""Student-t HMM 的三条基础单元测试（用户 2026-11-21 要求）。

1. **Gaussian 极限**：ν→∞ 时 t 的对数密度必须趋近多元高斯。
2. **过滤递推**：手工构造 2 状态 2 天的例子，逐日验证过滤概率与预测似然。
3. **EM 更新**：每次迭代的观测对数似然不能下降，尺度矩阵始终正定。

跑法：

    python tests/run_tests.py hmm_state
    python tests/run_tests.py            # 全量
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def test_t_logpdf_converges_to_gaussian() -> None:
    """ν 很大时，t 密度必须等于高斯密度（差 < 1e-4）。"""
    from experiment.hmm_state.stage3_t import gauss_logpdf, t_logpdf

    rng = np.random.default_rng(0)
    x = rng.normal(size=(200, 3)) * 1.5 + 0.3
    mu = np.zeros(3)
    cov = np.array([[1.0, 0.3, 0.0], [0.3, 1.2, -0.2], [0.0, -0.2, 0.8]])

    a = t_logpdf(x, mu, cov, nu=1e8)
    b = gauss_logpdf(x, mu, cov)
    assert np.max(np.abs(a - b)) < 1e-5, (
        f"ν→∞ 应退化成高斯，实际最大差 {np.max(np.abs(a - b)):.2e}")


def test_filtered_recursion_by_hand() -> None:
    """两状态两天的手算例。

    第一天 prior = [0.6, 0.4]，发射似然 ∝ [1, 3]
    → un ∝ [0.6, 1.2]，和 = 1.8 → log p(x1) = log(1.8)，post = [1/3, 2/3]
    第二天 prior = post·A，A=[[0.9,0.1],[0.2,0.8]]
    → [0.3+0.1333, 0.0333+0.5333] = [0.43333, 0.56667]
    发射 ∝ [4, 1] → un ∝ [1.73333, 0.56667]，和 = 2.3
    → log p(x2) = log(2.3)，post = [0.75362, 0.24638]
    """
    from experiment.hmm_state.stage3_t import filtered_predict

    a = np.array([[0.9, 0.1], [0.2, 0.8]])
    log_b = np.log(np.array([[1.0, 3.0], [4.0, 1.0]]))
    post, logpred = filtered_predict(log_b, a, np.array([0.6, 0.4]))

    assert np.allclose(post[0], [1 / 3, 2 / 3]), post[0]
    assert np.isclose(logpred[0], np.log(1.8)), logpred[0]        # 0.6*1 + 0.4*3
    assert np.isclose(np.exp(logpred[1]), 2.3, rtol=1e-9), logpred[1]
    assert np.allclose(post[1], [1.73333 / 2.3, 0.56667 / 2.3], atol=1e-4), post[1]


def test_em_loglik_monotone_and_positive_definite() -> None:
    """EM 的观测似然不得下降；每轮尺度矩阵必须正定。"""
    from experiment.hmm_state.stage3_t import _chol, fit_t_hmm

    rng = np.random.default_rng(7)
    # 两簇 + 厚尾：一个纯净的正态簇 + 一个带离群点的簇
    x = np.vstack([rng.normal(0, 1, size=(300, 2)),
                   rng.normal(4, 1, size=(300, 2))])
    x[:20] *= 6.0    # 人为厚尾

    fit = fit_t_hmm(x, k=2, nu=5.0)
    hist = np.array(fit["hist"])
    assert len(hist) > 2, "至少迭代两轮"
    drops = np.diff(hist)
    assert drops.min() > -1e-6, f"似然出现下降：{drops.min():.3e}"
    for cov in fit["covars"]:
        _chol(cov)                      # 非正定会抛异常
        assert np.all(np.isfinite(cov))
    assert fit["iters"] >= 2
