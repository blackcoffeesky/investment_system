"""拟合编排：把「配置 + 特征 + 模型 + 评价」串成一次 fit。

这一层是状态层的"训练入口"，负责：
1. 按 config 构造特征与模型
2. 调模型的 fit（walk-forward，产出样本外标签）
3. 用 metrics 评价（模型不评价自己）
4. 组装 spec（model / data / metrics 三段）并落盘

模型、评价、存储都不在这层里面——这里只做编排。
"""

from __future__ import annotations

import hashlib
from typing import Any

import pandas as pd

from state_layer import state_assets, store
from state_layer.model import MODELS, extreme
from state_layer.model.hybrid import RuleGated
from state_layer.metrics import score as score_metric


def _signature(labels: pd.Series) -> str:
    """标签序列的指纹：边快照靠它判断"状态体系有没有变"。"""
    s = "".join(str(x) for x in labels.values)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


def _fit_id(asset: str, k: int, features: list[str], fit_date: str) -> str:
    raw = f"{asset}|{k}|{','.join(features)}|{fit_date}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def fit(
    asset: str,
    panel: pd.DataFrame,
    *,
    model_name: str = "hmm",
    labeler=None,
    k: int | None = None,
    seed: int = 42,
    verbose: bool = True,
) -> dict[str, Any]:
    """在面板上训练状态模型，返回 {spec, labels, model, metrics}（不落盘）。"""
    pkg = state_assets.load(asset)
    c = pkg.config
    feats = pkg.features.build(panel)
    k_use = c.K if k is None else k

    inner = MODELS[model_name]()
    model = RuleGated(inner, params=c.EXTREME_PARAMS)

    ext = extreme.classify(feats, c.EXTREME_PARAMS)
    feats_regular = feats.loc[ext == ""]

    # K 的默认**来自 config**：拟合是可复现操作，不该每次悄悄换状态数。
    # 想让数据自己选，显式传 k="auto"。
    picked_k, k_table = k_use, None
    if k == "auto":
        from state_layer.model.hmm import select as S

        picked_k, k_table = S.select_k(feats_regular, seed=seed)
        if verbose:
            print(k_table.round(4).to_string())
            print(f"-> K = {picked_k}")

    model.fit(feats_regular, k=picked_k, seed=seed, labeler=labeler)
    labels = model.labels_ if model.labels_ is not None else pd.Series(dtype=object)
    labels = extreme.apply(labels, ext).reindex(feats.index).fillna("")

    metrics = score_metric(c.METRICS["separation"], model.inner.state_params(),
                           threshold=c.METRICS.get("min_separation", 0.4))

    fit_date = str(feats.dropna().index[-1].date())
    spec = {
        "asset": asset,
        "model": {
            "kind": model_name,
            "k": int(picked_k),
            "version": f"{model_name}-k{picked_k}-{_fit_id(asset, picked_k, list(feats.columns), fit_date)}",
        },
        "data": {
            "features": list(feats.columns),
            "fit_date": fit_date,
            "label_signature": _signature(labels),
            "used_labels": sorted({v for v in labels if v}),
            "fit_id": _fit_id(asset, picked_k, list(feats.columns), fit_date),
        },
        "metrics": metrics,
    }
    return {"spec": spec, "labels": labels, "model": model, "k_table": k_table}


def save(asset: str, result: dict[str, Any]) -> Path:
    """落盘：spec + labels + 模型参数 + 最后窗口。"""
    model = result["model"]
    store.save_spec(asset, result["spec"])
    store.save_labels(asset, result["labels"])
    store.save_model(asset, "hmm.pkl", model.to_dict())
    inner = getattr(model, "inner", None)
    lw = getattr(inner, "last_window", None)
    if lw:
        store.save_model(asset, "hmm_last.pkl", lw)
    return store.asset_dir(asset) / "current.json"


# `Path` 只用于类型标注，放最后避免与上面的函数名冲突
from pathlib import Path  # noqa: E402
