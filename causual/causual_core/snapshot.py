"""边模型快照：只存系数，按资产隔离。

    data/snapshots/{asset}/edges_YYYYMMDD.json
    data/snapshots/{asset}/latest.json
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd

from causual.causual_core.edge import Edge, index as edge_index
from causual.causual_core.estimators import EdgeModel

ROOT = Path(__file__).resolve().parents[2]
SNAP_ROOT = ROOT / "causual" / "data" / "snapshots"
# v3：分布滞后改为"当期口径"（只留 path/path_ci，删除 cumulative 系列）
SCHEMA = "edges-v3"


def asset_dir(asset: str) -> Path:
    return SNAP_ROOT / asset


def _encode(m: EdgeModel) -> dict:
    return {
        "source": m.edge.source,
        "target": m.edge.target,
        "n_obs": int(m.n_obs),
        "beta_lead": {str(k): float(v) for k, v in m.beta_lead.items()},
        "beta_ci": {str(k): [float(a), float(b)] for k, (a, b) in m.beta_ci.items()},
        "p_value": float(m.p_value),
        "caar": float(m.caar),
        "t_stat": float(m.t_stat),
        "n_events": int(m.n_events),
        "window_days": int(m.window_days),
        "notes": m.notes,
        "fitted_until": str(m.fitted_until.date()) if m.fitted_until is not None else "",
        "modifier_cols": list(m.modifier_cols),
        "path": {str(k): float(v) for k, v in m.path.items()},
        "path_ci": {str(k): [float(a), float(b)] for k, (a, b) in m.path_ci.items()},
        "path_by_group": {g: {str(k): float(v) for k, v in d.items()}
                          for g, d in m.path_by_group.items()},
        "path_ci_by_group": {g: {str(k): [float(a), float(b)] for k, (a, b) in d.items()}
                             for g, d in m.path_ci_by_group.items()},
        "n_by_group": {g: int(n) for g, n in m.n_by_group.items()},
        "estimator_used": m.estimator_used,
        "freq_used": m.freq_used,
        "split_contemp": bool(m.split_contemp),
    }


def _decode(d: dict, edges: list[Edge]) -> EdgeModel:
    idx = edge_index(edges)
    key = (d["source"], d["target"])
    if key not in idx:
        raise KeyError(f"快照里的边 {key} 已不在该资产的 DAG 中")
    e = idx[key]
    # split_contemp 会改变 α 的口径：快照里存的是估计时的值，
    # 与当前配置不一致就必须重估，否则 path 是旧的、跳过 lag0 的规则是新的。
    stored_sc = d.get("split_contemp")
    if stored_sc is not None and bool(stored_sc) != bool(e.split_contemp):
        raise ValueError(
            f"{e} 的 split_contemp 已改（快照 {stored_sc} vs 配置 {e.split_contemp}），"
            "请重跑 `cli edges` 重估"
        )
    return EdgeModel(
        edge=e,
        split_contemp=bool(stored_sc) if stored_sc is not None else bool(e.split_contemp),
        n_obs=int(d["n_obs"]),
        beta_lead={k: float(v) for k, v in d["beta_lead"].items()},
        beta_ci={k: (float(v[0]), float(v[1])) for k, v in d.get("beta_ci", {}).items()},
        p_value=float(d.get("p_value", "nan")),
        caar=float(d.get("caar", "nan")),
        t_stat=float(d.get("t_stat", "nan")),
        n_events=int(d.get("n_events", 0)),
        window_days=int(d.get("window_days", 0)),
        notes=str(d.get("notes", "")),
        fitted_until=pd.Timestamp(d["fitted_until"]) if d.get("fitted_until") else None,
        modifier_cols=list(d.get("modifier_cols", [])),
        path={int(k): float(v) for k, v in d.get("path", {}).items()},
        path_ci={int(k): (float(v[0]), float(v[1])) for k, v in d.get("path_ci", {}).items()},
        path_by_group={g: {int(k): float(v) for k, v in dd.items()}
                       for g, dd in d.get("path_by_group", {}).items()},
        path_ci_by_group={g: {int(k): (float(v[0]), float(v[1])) for k, v in dd.items()}
                          for g, dd in d.get("path_ci_by_group", {}).items()},
        n_by_group={g: int(n) for g, n in d.get("n_by_group", {}).items()},
        estimator_used=str(d.get("estimator_used", "")),
        freq_used=str(d.get("freq_used", "")),
    )


def save(
    asset: str,
    models: dict[tuple[str, str], EdgeModel],
    label_signature: str,
    fit_date: str | None = None,
) -> Path:
    d = asset_dir(asset)
    d.mkdir(parents=True, exist_ok=True)
    fit_date = fit_date or str(dt.date.today())

    payload = {
        "schema": SCHEMA,
        "asset": asset,
        "fit_date": fit_date,
        "label_signature": label_signature,
        "edges": [_encode(m) for m in models.values()],
    }
    p = d / f"edges_{fit_date.replace('-', '')}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "latest.json").write_text(
        json.dumps({"file": p.name, "fit_date": fit_date,
                    "label_signature": label_signature}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return p


def load(asset: str, edges: list[Edge]) -> tuple[dict[tuple[str, str], EdgeModel], dict]:
    d = asset_dir(asset)
    ptr = d / "latest.json"
    if not ptr.exists():
        raise FileNotFoundError(f"没有 {asset} 的边模型快照；先运行 edges fit --asset {asset}")
    meta = json.loads(ptr.read_text(encoding="utf-8"))
    payload = json.loads((d / meta["file"]).read_text(encoding="utf-8"))

    schema = str(payload.get("schema", ""))
    if schema != SCHEMA:
        raise ValueError(
            f"{asset} 的边快照是 {schema or '未知版本'}，当前需要 {SCHEMA}"
            f"（分布滞后已改为当期口径）。请重跑：python -m causual.causual_core.cli edges --asset {asset}"
        )

    models = {}
    for rec in payload["edges"]:
        m = _decode(rec, edges)
        models[m.key] = m
    return models, {
        "fit_date": payload.get("fit_date", ""),
        "label_signature": payload.get("label_signature", ""),
        "path": str(d / meta["file"]),
        "asset": payload.get("asset", asset),
    }
