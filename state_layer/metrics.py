"""状态评价：**独立于模型**。

为什么不在模型里
----------------
"这个状态划分好不好"是任务层面的判断，同一把尺子要能量 HMM、HSMM、GMM-HMM。
评价函数长在模型内部，横向比较就永远做不成。

约定
----
- 模型只提供 `state_params()`（means / covars / transmat 的纯数组）
- 这里的函数只吃纯数组，不知道模型是什么
- 用哪个指标由 `config.METRICS` 声明
"""

from __future__ import annotations

import numpy as np

METRICS = {}          # 名称 → 函数，供 config 按名字引用


def register(name: str):
    def deco(fn):
        METRICS[name] = fn
        return fn

    return deco


@register("bhattacharyya")
def separation(means, covars, threshold: float = 0.4) -> dict:
    """状态间的 Bhattacharyya 距离（最小成对距离）。

    高斯假设下：
        1/8 (μ1-μ2)ᵀ Σ⁻¹ (μ1-μ2) + 1/2 ln( |Σ| / sqrt(|Σ1||Σ2|) )
    取值越大越可分；低于阈值说明两个状态在观测上分不开。
    """
    means = np.asarray(means, dtype=float)
    covars = np.asarray(covars, dtype=float)
    k = len(means)
    if k < 2:
        return {"method": "bhattacharyya", "min": float("inf"),
                "passed": True, "pairs": []}

    pairs = []
    worst = float("inf")
    for i in range(k):
        for j in range(i + 1, k):
            d = _pairwise(means[i], means[j], covars[i], covars[j])
            pairs.append({"i": i, "j": j, "dist": float(d)})
            worst = min(worst, d)

    return {"method": "bhattacharyya", "min": float(worst),
            "passed": bool(worst >= threshold), "pairs": pairs}


def _pairwise(mu1, mu2, s1, s2) -> float:
    s = (s1 + s2) / 2.0
    try:
        inv = np.linalg.inv(s)
    except np.linalg.LinAlgError:  # pragma: no cover - 退化协方差
        inv = np.linalg.pinv(s)
    dm = mu1 - mu2
    term1 = np.dot(np.dot(dm.T, inv), dm) / 8.0
    sign1, logdet1 = np.linalg.slogdet(s1)
    sign2, logdet2 = np.linalg.slogdet(s2)
    sign, logdet = np.linalg.slogdet(s)
    if min(sign1, sign2, sign) <= 0:      # 非正定，只算均值项
        return float(term1)
    term2 = 0.5 * (logdet - 0.5 * (logdet1 + logdet2))
    return float(term1 + term2)


def score(name: str, state_params: dict, **kwargs) -> dict:
    """按名字调用指标。`state_params` 是模型给的纯数组字典。"""
    if name not in METRICS:
        raise KeyError(f"没有这个评价指标：{name}（可用：{sorted(METRICS)}）")
    if name == "bhattacharyya":
        return METRICS[name](state_params["means"], state_params["covars"],
                             **kwargs)
    return METRICS[name](**state_params, **kwargs)
