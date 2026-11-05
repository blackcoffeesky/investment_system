"""状态标签对齐：让同一个状态在时间上始终叫同一个名字。

问题
----
逐窗重估时，每个窗口的状态编号会重排（S0 在 2019 年可能是"反弹"，2024 年变成"S0=上行"）。
如果每个窗口各自命名，同一个特征在不同年份会被叫成不同名字。

做法
----
用**全样本模型**的状态均值作为参照，把每个窗口的状态按均值距离做匈牙利匹配，
再套用参照映射的标签。这样"状态长什么样 → 叫什么"在时间上完全一致。

注意
----
参照映射来自全样本，属于**命名**使用未来信息；β 的估计仍然是逐窗、无前视的。
命名泄漏只影响标签文字，不影响系数。若要绝对无前视，可以把参照换成"截至当日"
的模型——代价是标签仍会随时间漂移，回到原点。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment


def reference_mapping(ref_means: pd.DataFrame, ref_labels: dict[int, str]) -> dict[int, str]:
    return {int(k): str(v) for k, v in ref_labels.items()}


def make_labeler(ref_means: pd.DataFrame, ref_labels: dict[int, str]):
    """返回一个 labeler：把当前窗口的状态映射到参照状态，再套参照标签。"""
    cols = list(ref_means.columns)
    ref = ref_means[cols].to_numpy(dtype=float)

    # 用参照状态的离散度做标准化，避免 log_return(1e-4) 被 vol(1e-2) 压死
    scale = ref.std(axis=0)
    scale[scale < 1e-12] = 1.0
    ref_z = ref / scale

    def _labeler(means: pd.DataFrame) -> dict[int, str]:
        cur = means[cols].to_numpy(dtype=float) / scale
        cost = ((cur[:, None, :] - ref_z[None, :, :]) ** 2).sum(axis=2)
        rows, cols_idx = linear_sum_assignment(cost)
        return {int(i): ref_labels[int(j)] for i, j in zip(rows, cols_idx)}

    return _labeler


def resolve_vol_col(columns, preferred: str = "realized_vol_5d") -> str | None:
    """在状态均值表里找"波动"列。

    各资产波动口径不同（黄金 5 日、A 股 20 日），**不能让 label 硬编码**：
    找不到必须显式报出来，否则 v 会静默变成全 0，
    `panic/rebound` 这类靠波动区分的标签就退化成按状态编号随便分配。
    """
    cols = list(columns)
    if preferred in cols:
        return preferred
    cand = sorted(c for c in cols if str(c).startswith("realized_vol"))
    return cand[0] if cand else None


def three_way_groups(returns) -> tuple[list[int], list[int], list[int]]:
    """按收益**排序切三段**，保证 bear / bull 非空。

    K≥3 时三组都非空；K=2 只分低/高两组（不硬造 neut）；K=1 全部归 middle。
    """
    r = np.asarray(returns, dtype=float)
    k = len(r)
    if k <= 1:
        return [], list(range(k)), []
    if k == 2:
        order = np.argsort(r, kind="stable")
        return [int(order[0])], [int(order[1])], []

    nb = max(1, int(round(k / 3)))
    nu = max(1, int(round(k / 3)))
    while nb + nu > k - 1:          # 给 neut 至少留一个
        if nb >= nu and nb > 1:
            nb -= 1
        elif nu > 1:
            nu -= 1
        else:
            break

    order = np.argsort(r, kind="stable")
    return (
        [int(i) for i in order[:nb]],
        [int(i) for i in order[-nu:]],
        [int(i) for i in order[nb:k - nu]],
    )


def name_states(
    means: pd.DataFrame,
    *,
    ret_col: str,
    vol_col: str,
    pools: tuple[list, list, list],
    proto: dict,
    default,
) -> tuple[dict[int, str], str | None]:
    """通用的状态命名：按收益排序切三段，组内按波动从高到低配标签。

    ``pools`` 依次是 (bear, bull, neut) 的候选标签；``proto`` 给出组内排序键
    （值越大越"高波"）。返回 (映射, 实际使用的波动列)。
    """
    vcol = resolve_vol_col(means.columns, vol_col)
    r = means[ret_col].to_numpy(dtype=float)
    v = means[vcol].to_numpy(dtype=float) if vcol else np.zeros(len(r))

    bear, bull, neut = three_way_groups(r)
    out: dict[int, str] = {}
    # bear/bull 内按波动**从高到低**配标签（状态越极端越先拿）；
    # neutral 内按波动**从低到高**——低波是"震荡"，高波才是"派发"。
    for idxs, pool, asc in ((bear, pools[0], False), (bull, pools[1], False),
                            (neut, pools[2], True)):
        if not idxs or not pool:
            continue
        labs = list(pool)
        ordered = sorted(idxs, key=lambda i: float(v[i]) * (1 if asc else -1))
        for pos, i in enumerate(ordered):
            lab = labs[min(pos, len(labs) - 1)]
            out[int(i)] = lab.value if hasattr(lab, "value") else str(lab)

    for i in range(len(r)):
        out.setdefault(i, default.value if hasattr(default, "value") else default)
    return out, vcol


def label_churn(labels: pd.Series) -> pd.DataFrame:
    """标签稳定性的粗略诊断：每个标签出现在多少个不连续的区段里。

    区段越多，说明同一个名字在不同时期反复出现/消失，语义越不稳。
    """
    s = labels[labels != ""]
    if s.empty:
        return pd.DataFrame(columns=["label", "segments", "days"])
    run = (s != s.shift()).cumsum()
    seg = pd.DataFrame({"label": s.to_numpy(), "run": run.to_numpy()}).groupby(
        ["label", "run"], sort=False).size().rename("n").reset_index()
    out = seg.groupby("label")["n"].agg(segments="size", days="sum")
    return out.sort_values("segments", ascending=False)
