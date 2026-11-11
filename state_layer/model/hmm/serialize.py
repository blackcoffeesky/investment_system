"""模型序列化：**只存参数，不存对象**。

为什么不用 pickle 直接倒模型对象
--------------------------------
hmmlearn / sklearn 的类结构在小版本之间也会变，pickle 反序列化经常直接报错或
静默改变行为。这里把 HMM 的全部参数（means / covars / transmat / startprob）
连同标准化与 winsorize 参数一起存成 dict，加载时重建一个等价的 GaussianHMM。

文件名仍用 `.pkl`（用户约定），但内容是 dict，可以用 pickle 打开检查。
"""

from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]      # state_layer/
MODEL_DIR = ROOT / "models"


def model_path(asset: str) -> Path:
    return MODEL_DIR / asset / "hmm.pkl"


def named_path(asset: str, name: str) -> Path:
    return MODEL_DIR / asset / name


def save_named(spec: dict, asset: str, name: str) -> Path:
    p = named_path(asset, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("wb") as f:
        pickle.dump(spec, f, protocol=4)
    return p


def load_named(asset: str, name: str) -> dict:
    p = named_path(asset, name)
    if not p.exists():
        raise FileNotFoundError(f"没有 {asset} 的模型文件：{p}")
    with p.open("rb") as f:
        return pickle.load(f)


def make_fit_id(k: int, label_signature: str, fit_date: str) -> str:
    """一次拟合的指纹：模型文件与 spec 都用它，对不上就说明漂移了。"""
    import hashlib

    raw = f"{k}|{label_signature}|{fit_date}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def save(spec: dict, asset: str) -> Path:
    p = model_path(asset)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("wb") as f:
        pickle.dump(spec, f, protocol=4)
    return p


def load(asset: str) -> dict:
    p = model_path(asset)
    if not p.exists():
        raise FileNotFoundError(f"没有 {asset} 的 HMM 模型：{p}")
    with p.open("rb") as f:
        return pickle.load(f)


def rebuild(spec: dict):
    """从参数字典重建 GaussianHMM 对象（用于在线算后验）。"""
    from hmmlearn.hmm import GaussianHMM

    # 最后一个窗口的文件里没有 k，从 means 推
    k = int(spec.get("k") or len(np.asarray(spec["means"])))
    hmm = GaussianHMM(n_components=k, covariance_type="full")
    hmm.means_ = np.asarray(spec["means"], dtype=float)
    hmm.covars_ = np.asarray(spec["covars"], dtype=float)
    hmm.transmat_ = np.asarray(spec["transmat"], dtype=float)
    hmm.startprob_ = np.asarray(spec["startprob"], dtype=float)
    hmm.n_features = hmm.means_.shape[1]
    return hmm


def to_numpy(obj):
    """把 numpy/pandas 递归转成可 pickle 的纯 Python 结构。"""
    if isinstance(obj, (np.ndarray,)):
        return obj.tolist()
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    if isinstance(obj, pd.Series):
        return obj.tolist()
    if isinstance(obj, dict):
        return {k: to_numpy(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_numpy(x) for x in obj]
    return obj
