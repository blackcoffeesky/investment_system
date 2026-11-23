"""黄金的命名：按「收益排序切三段 + 组内按波动排序」给 K=4 起名。

**代码是原样从 `align.py` 搬过来的**（2026-11-23），行为一字未改。
为什么单独一份：这套命名只服务黄金的 K=4（A 股改用序位命名了）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


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


