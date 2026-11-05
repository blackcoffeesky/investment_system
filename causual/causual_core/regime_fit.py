"""状态模型的装配：极端规则 + 常规 HMM。

两层结构
--------
1. **极端**：规则判定（`regime_engine.extreme`），不进 HMM
2. **常规**：HMM 只在非极端日上训练与解码

两支资产共用这一套装配，避免各自维护一份逻辑。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import regime_engine
from regime_engine import extreme as X
from regime_engine.align import make_labeler
from regime_engine.serialize import make_fit_id, to_numpy


def fit_asset(
    df: pd.DataFrame,
    feat: pd.DataFrame,
    *,
    labeler_factory,
    name_by_value: dict[str, str],
    signature_fn,
    asset: str,
    extreme_params: X.ExtremeParams | None = None,
    k: int | None = None,
    seed: int = 42,
    min_train: int = 500,
    refit_every: int = 60,
    k_candidates: tuple[int, ...] = (2, 3, 4),
    verbose: bool = True,
) -> dict:
    """返回 {spec, labels, fit, last, profile}。"""
    ext = X.classify(feat, extreme_params)
    regular_mask = ext == ""
    reg_feat = feat.loc[regular_mask]

    if len(reg_feat.dropna()) < min_train + 50:
        raise ValueError(f"剔除极端日后只剩 {len(reg_feat.dropna())} 行，不足以拟合")

    k_table = None
    if k is None:
        k, k_table = regime_engine.select_k(reg_feat, candidates=k_candidates, seed=seed)
        if verbose:
            print(k_table.round(4).to_string())
            print(f"-> K = {k}（候选 {k_candidates}）")

    fit = regime_engine.fit_regime(reg_feat, k=k, seed=seed)
    ref_means = fit.raw_means()
    labeler = make_labeler(ref_means, labeler_factory(ref_means))
    reg_labels, proba, last = fit.walk_forward(
        reg_feat, labeler=labeler, min_train=min_train, refit_every=refit_every
    )

    # 合并：极端日由规则给标签，其余用 HMM
    labels = ext.copy()
    common = reg_labels.index
    labels.loc[common] = reg_labels.values
    labels = labels.reindex(feat.index).fillna("").astype(str)

    profile = _profile(labels, feat, ext, name_by_value)
    used = tuple(sorted({r["label"] for r in profile if r["n"] > 0}))

    valid = labels[labels != ""]
    fit_date = str(valid.index[-1].date()) if len(valid) else ""
    fit_id = make_fit_id(int(k), signature_fn(used), fit_date)

    spec = {
        "asset": asset,
        "k": int(k),
        "label_signature": signature_fn(used),
        "used_labels": list(used),
        "min_bhattacharyya": float(fit.separation["min_dist"]),
        "fit_date": fit_date,
        "profile": profile,
        "engine_spec": fit.spec(),
        "fit_id": fit_id,
        "last_proba": [float(x) for x in proba.iloc[-1].to_numpy()] if len(proba) else [],
        "last_proba_date": str(proba.index[-1].date()) if len(proba) else "",
        "extreme": {
            "enabled": bool(getattr(extreme_params, "enabled", True)),
            "params": to_numpy(extreme_params.__dict__) if extreme_params else {},
            "durations": X.duration_stats(ext),
            "n_extreme": int((ext != "").sum()),
        },
        "k_table": k_table.round(6).reset_index().to_dict("records") if k_table is not None else [],
    }
    return {"spec": spec, "labels": labels, "fit": fit, "last": last, "profile": pd.DataFrame(profile)}


def _profile(labels: pd.Series, feat: pd.DataFrame, ext: pd.Series,
             name_by_value: dict[str, str]) -> list[dict]:
    """每个标签的画像。极端标签没有 HMM 状态，用当日特征均值即可。"""
    valid = labels[labels != ""]
    total = max(len(valid), 1)
    run = (valid != valid.shift()).cumsum()
    run_len = valid.groupby(run).size()
    run_first = valid.groupby(run).first()

    ret_col = "log_return"
    vol_col = next((c for c in feat.columns if str(c).startswith("realized_vol")), None)

    rows = []
    for lab in pd.unique(labels[labels != ""]):
        mask = labels == lab
        n = int(mask.sum())
        if n == 0:
            continue
        idx = labels[mask].index
        dur = (float(run_len[run_first == lab].mean())
               if (run_first == lab).any() else float(n))
        rows.append({
            "label": str(lab),
            "name": name_by_value.get(str(lab), str(lab)),
            "n": n,
            "share": n / total,
            "avg_duration": dur,
            "mean_ret": float(feat.loc[idx, ret_col].mean() * 100),
            "vol": float(feat.loc[idx, vol_col].mean() * 100) if vol_col else float("nan"),
            "is_extreme": bool(lab in X.EXTREME_LABELS),
        })
    rows.sort(key=lambda r: (r["is_extreme"], -r["n"]))
    return rows
