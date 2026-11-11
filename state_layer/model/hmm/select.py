"""状态数 K 的选择：BIC + 分离度，加一个 2% 的简约容忍带。"""

from __future__ import annotations

import pandas as pd

from state_layer import metrics
from state_layer.model.hmm import _ops as H


def select_k(
    feat: pd.DataFrame,
    candidates: tuple[int, ...] = (2, 3, 4, 5, 6),
    train_frac: float = 0.7,
    bh_threshold: float = 0.4,
    bic_tol: float = 0.02,
    seed: int = 42,
) -> tuple[int, pd.DataFrame]:
    """返回 (选定 K, 明细表)。

    先用训练窗口拟合每个候选 K，剔除状态分离不达标的，再在 BIC 最优值的
    ``bic_tol`` 容忍带内取**最小** K——避免长样本无限偏好大 K。
    """
    valid = feat.dropna()
    cut = int(len(valid) * train_frac)
    if cut < 200:
        raise ValueError("样本太短，无法选择状态数")

    lo, hi = H.fit_window_bounds(valid.iloc[:cut])
    scaler = H.make_scaler(H.clip_frame(valid.iloc[:cut], lo, hi))
    X = scaler.transform(H.clip_frame(valid.iloc[:cut], lo, hi))

    rows = []
    for k in candidates:
        model = H.fit(X, k, seed=seed)
        sep = metrics.score("bhattacharyya",
                        {"means": model.means_, "covars": model.covars_},
                        threshold=bh_threshold)
        rows.append({
            "k": k,
            "bic": float(model.bic(X)),
            "loglik": float(model.score(X)),
            "min_bhattacharyya": sep["min"],
            "separated": sep["passed"],
        })

    table = pd.DataFrame(rows).set_index("k")
    passed = table[table["separated"]]
    if len(passed):
        best = float(passed["bic"].min())
        band = passed[passed["bic"] <= best + abs(best) * bic_tol]
        return int(band.index.min()), table
    return int(table["min_bhattacharyya"].idxmax()), table
