"""产物读写：模型、spec、标签。

为什么单独一层
--------------
模型只管数组，不认文件；`port` 只管对外接口，不认路径。
把「存哪儿、什么格式」收在这里，将来换存储（比如进数据库）只改这一个文件。

产物目录：`state_layer/saved_models/{asset}/`
    hmm.pkl        模型参数（dict，pickle 壳；内容是可 JSON 的纯数据）
    hmm_last.pkl   最后一个窗口（在线算后验用）
    current.json   spec（model / data / metrics 三段）
    labels.csv     逐日标签
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "state_layer" / "saved_models"


def asset_dir(asset: str) -> Path:
    return MODEL_DIR / asset


# ---------------------------------------------------------------- spec
def save_spec(asset: str, spec: dict) -> Path:
    d = asset_dir(asset)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "current.json"
    p.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_spec(asset: str) -> dict:
    p = asset_dir(asset) / "current.json"
    if not p.exists():
        raise FileNotFoundError(f"没有 {asset} 的状态模型：{p}；先跑 state_layer.cli regime")
    spec = json.loads(p.read_text(encoding="utf-8"))
    return _migrate(spec, asset)


def _migrate(spec: dict, asset: str) -> dict:
    """旧的扁平 spec（k / features 在顶层）→ 新的三段式（model / data / metrics）。

    老文件是历史产物，不能要求用户重跑一遍拟合才能看图。
    """
    if "model" in spec and "data" in spec:
        return spec
    k = spec.get("k")
    features = list(spec.get("features") or [])
    fit_date = spec.get("fit_date")
    sig = spec.get("label_signature")
    return {
        "asset": asset,
        "model": {
            "kind": "hmm",
            "k": k,
            "version": f"hmm-k{k}-{sig}-{fit_date}",
        },
        "data": {
            "features": features,
            "fit_date": fit_date,
            "label_signature": sig,
            "used_labels": spec.get("used_labels", []),
            "fit_id": spec.get("fit_id"),
        },
        "metrics": {
            "method": "bhattacharyya",
            "min": float(spec.get("min_bhattacharyya", 0.0)),
            "passed": float(spec.get("min_bhattacharyya", 0.0)) >= 0.4,
        },
    }


# ---------------------------------------------------------------- 标签
def save_labels(asset: str, labels: pd.Series) -> Path:
    d = asset_dir(asset)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "labels.csv"
    pd.DataFrame({"date": labels.index, "label": labels.values}).to_csv(
        p, index=False, encoding="utf-8-sig")
    return p


def load_labels(asset: str) -> pd.Series:
    p = asset_dir(asset) / "labels.csv"
    if not p.exists():
        return pd.Series(dtype=str)
    df = pd.read_csv(p)
    idx = pd.DatetimeIndex(pd.to_datetime(df["date"]))
    vals = [str(x) if x == x else "" for x in df["label"]]
    out = pd.Series(vals, index=idx, name="label", dtype=object)
    return out[out != ""].astype(str)


# ---------------------------------------------------------------- 模型参数
def save_model(asset: str, name: str, data: dict) -> Path:
    d = asset_dir(asset)
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    with p.open("wb") as f:
        pickle.dump(data, f, protocol=4)
    return p


def load_model(asset: str, name: str) -> dict:
    p = asset_dir(asset) / name
    if not p.exists():
        raise FileNotFoundError(f"没有 {asset} 的模型文件：{p}")
    with p.open("rb") as f:
        return pickle.load(f)
