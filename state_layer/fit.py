"""拟合编排：按 `MODEL_CHAIN` 依次跑模型，谁认领什么由模型自己决定。

这一层只做三件事：
1. 从 config 读链与参数，按次序构造模型
2. 串起来：上一个模型返回的"没认领日期"喂给下一个
3. 把最后那个统计模型的参数交给 `metrics` 评价，组装 spec 落盘

模型、评价、存储都不在这里面。
"""

from __future__ import annotations

import hashlib
from typing import Any

import pandas as pd

from state_layer import state_assets, store
from state_layer.metrics import score as score_metric
from state_layer.model import MODELS


def _signature(labels: pd.Series) -> str:
    """标签序列的指纹：边快照靠它判断"状态体系有没有变"。"""
    s = "".join(str(x) for x in labels.values)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


def _fit_id(asset: str, chain: tuple[str, ...], fit_date: str) -> str:
    raw = f"{asset}|{','.join(chain)}|{fit_date}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _profile(labels: pd.Series, feats: pd.DataFrame) -> list[dict]:
    """每个标签的画像：占比、平均持续、日均收益、日波动。卡片直接渲染这张表。"""
    valid = labels[labels != ""]
    if not len(valid):
        return []
    total = len(valid)
    run = (valid != valid.shift()).cumsum()
    run_len = valid.groupby(run).size()
    run_first = valid.groupby(run).first()
    ret = feats["log_return"] if "log_return" in feats else None

    rows = []
    for lab in sorted(set(valid)):
        mask = run_first == lab
        n = int((valid == lab).sum())
        sub = ret.reindex(valid.index)[valid == lab] if ret is not None else None
        rows.append({
            "label": lab,
            "n": n,
            "share": n / total,
            "avg_duration": float(run_len[mask].mean()) if mask.any() else 0.0,
            "mean_ret": float(sub.mean() * 100) if sub is not None and len(sub) else None,
            "vol": float(sub.std() * 100) if sub is not None and len(sub) else None,
        })
    return rows


def fit_pipeline(
    asset: str,
    panel: pd.DataFrame,
    *,
    labeler=None,
    verbose: bool = True,
) -> dict[str, Any]:
    """在面板上依次跑模型链，返回 {spec, labels, models, chain}（不落盘）。"""
    pkg = state_assets.load(asset)
    c = pkg.config
    feats = pkg.features.build(panel)

    chain = tuple(getattr(c, "MODEL_CHAIN", ("hmm",)))
    params = dict(getattr(c, "MODEL_PARAMS", {}))

    labels = pd.Series("", index=feats.index, dtype=object)
    # 起点是**有特征的行**：NaN 行谁都认领不了，不该算进"已认领"
    remaining = feats.dropna().index
    models: list[Any] = []

    for name in chain:
        model = MODELS[name](params.get(name))
        unclaimed = model.fit(feats, claim=remaining, labeler=labeler)
        claimed = remaining.difference(unclaimed)
        if model.labels_ is not None and len(model.labels_):
            labels.loc[claimed] = model.labels_.reindex(claimed).astype(str)
        models.append(model)
        remaining = unclaimed
        if verbose:
            print(f"  [{name}] 认领 {len(claimed)} 天，剩 {len(remaining)} 天")
        if not len(remaining):
            break

    if len(remaining):
        # 链没覆盖全部日期：不抛错，下游拿不到标签自然会发现
        print(f"[warn] {asset} 有 {len(remaining)} 天没有被任何模型认领")

    labels = labels.fillna("")
    stat = next((m for m in reversed(models) if m.state_params() is not None), None)
    metrics = {"method": None, "min": None, "passed": None}
    if stat is not None:
        method = c.METRICS["separation"]
        metrics = score_metric(method, stat.state_params(),
                               threshold=c.METRICS.get("min_separation", 0.4))

    k_used = getattr(stat, "k_used", None)
    fit_date = str(feats.dropna().index[-1].date())
    version = f"{'+'.join(chain)}" + (f"-k{k_used}" if k_used else "") + f"-{_fit_id(asset, chain, fit_date)}"

    spec = {
        "asset": asset,
        "chain": list(chain),
        "model": {"kind": "+".join(chain), "k": k_used, "version": version},
        "data": {
            "features": list(feats.columns),
            "fit_date": fit_date,
            "label_signature": _signature(labels),
            "used_labels": sorted({v for v in labels if v}),
            "profile": _profile(labels, feats),
            "fit_id": _fit_id(asset, chain, fit_date),
        },
        "metrics": metrics,
    }
    return {"spec": spec, "labels": labels, "models": models, "chain": chain}


def save(asset: str, result: dict[str, Any]) -> Path:
    """落盘：spec + labels + 整条链。"""
    store.save_spec(asset, result["spec"])
    store.save_labels(asset, result["labels"])
    store.save_chain(asset, [m.to_dict() for m in result["models"]])
    return store.asset_dir(asset) / "current.json"


from pathlib import Path  # noqa: E402  （只用于类型标注，放最后避免和上面的名字冲突）
