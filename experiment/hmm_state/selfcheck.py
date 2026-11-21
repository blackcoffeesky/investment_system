"""HSMM 实现自检：真实持续时间是几何分布时，HSMM 必须退化成 HMM。

    python -m experiment.hmm_state.selfcheck

## 为什么需要它

2026-11-21 那版 HSMM 有两个会**改变结论但不报错**的错误：

1. 风险率分母用了 `P(D>d)` 而不是 `P(D≥d)`，还带一格索引偏移；
2. 离开状态时直接用了完整转移矩阵 `A`（对角线会把状态送回自身）。

结果：HSMM 的状态莫名其妙变持久（中位 11 → 15 天），看起来"显式持续时间建模有用"。
实际是 bug。

这条自检是**唯一能证明实现正确的东西**：
生成一条已知几何持续时间的马尔可夫链，用真实路径估持续时间，
如果实现正确，HSMM 与 HMM 的预测得分必须几乎相同（差异应 < 1%）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def run(n: int = 6000, p_stay: float = 0.9, seed: int = 0) -> dict:
    from experiment.hmm_state.stage2 import (duration_pmf, embedded_transmat,
                                             hmm_forward, hazard, hsmm_forward)

    rng = np.random.default_rng(seed)
    k = 2
    a = np.array([[p_stay, 1 - p_stay], [1 - p_stay, p_stay]])
    pi = np.array([0.5, 0.5])

    s = np.zeros(n, dtype=int)
    for t in range(1, n):
        s[t] = s[t - 1] if rng.random() < p_stay else 1 - s[t - 1]

    pmf, info = duration_pmf(pd.Series(s), 80, fallback_stay=np.diag(a))
    hz = hazard(pmf)
    a_emb = embedded_transmat(a)

    log_b = np.log(np.array([[0.6, 0.4], [0.3, 0.7]]))[s]
    _, ll_hmm = hmm_forward(log_b, a, pi)
    joint0 = np.zeros((k, hz.shape[1]))
    joint0[:, 0] = pi
    _, ll_hsmm = hsmm_forward(log_b, a_emb, hz, joint0)

    sc_hmm = float(np.mean(-ll_hmm))
    sc_hsmm = float(np.mean(-ll_hsmm))
    return {"hmm": sc_hmm, "hsmm": sc_hsmm,
            "rel_diff": abs(sc_hsmm - sc_hmm) / sc_hmm, "info": info}


def main() -> int:
    r = run()
    print(f"HMM  得分 {r['hmm']:.5f}")
    print(f"HSMM 得分 {r['hsmm']:.5f}")
    print(f"相对差 {r['rel_diff']:.4%}（几何持续时间下应接近 0）")
    print("持续时间估计:", r["info"])
    ok = r["rel_diff"] < 0.01
    print("\n" + ("✅ 通过" if ok else "❌ 失败：HSMM 没有退化成 HMM，实现有问题"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
