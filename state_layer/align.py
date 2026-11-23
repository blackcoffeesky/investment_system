"""标签稳定性度量：同一个标签名在时间上被切成多少段。

留给 `label_churn` 一个安身之处（重估诊断要用）。
命名相关的东西已经搬走：通用序位命名 → `state_assets/labels.py`，
黄金 K=4 命名 → `state_assets/gold/labels.py`。
"""

from __future__ import annotations

import pandas as pd


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
