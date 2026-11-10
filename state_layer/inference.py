"""对外推理 + 输出契约。

输出格式（下游唯一该依赖的东西）
--------------------------------
```json
{
  "date": "2026-11-10",              # 状态所属交易日
  "asset": "ashare",
  "state_id": 1,                     # 模型内部标识，**重拟合后含义可能变**
  "label": "trend_up",               # 语义标签，下游用这个
  "label_name": "趋势上行",
  "state_probabilities": [0.87, 0.09, 0.04],
  "confidence": 0.87,
  "model": {
    "kind": "hmm", "k": 3,
    "features": ["log_return", ...],
    "fit_date": "2026-10-23",
    "signature": "8f57add043eb",
    "version": "hmm-k3-8f57add0-20261023"
  },
  "as_of_date": "2026-11-10"         # 数据可见日（月频特征可能晚于 date）
}
```

**下游只用 `label`**，不要用 `state_id`——HMM 的状态编号是任意排列的，
重训一次可能整体换位。`state_id` 留给模型内部与调试。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pandas as pd

from state_layer import config as cfg
from state_layer.estimators import make
from state_layer.postprocessors import extreme

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / "causual" / "data" / "regime"   # P1：产物仍在原处，P3 再搬


def _dir(asset: str) -> Path:
    return STATE_DIR / asset


def load_spec(asset: str) -> dict:
    p = _dir(asset) / "current.json"
    if not p.exists():
        raise FileNotFoundError(f"没有 {asset} 的状态模型：{p}；先跑 cli regime")
    return json.loads(p.read_text(encoding="utf-8"))


def load_last_window(asset: str) -> dict:
    """最后一个窗口的模型参数（P1 仍是 pkl，和 causual 共用）。"""
    import pickle

    p = ROOT / "regime_engine" / "saved_models" / asset / "hmm_last.pkl"
    if not p.exists():
        raise FileNotFoundError(
            f"缺 {p}；先跑 `python -m causual.causual_core.cli regime --asset {asset}`")
    with p.open("rb") as fh:
        return pickle.load(fh)


def load_labels(asset: str) -> pd.Series:  # noqa: D401
    p = _dir(asset) / "labels.csv"
    if not p.exists():
        return pd.Series(dtype=str)
    df = pd.read_csv(p)
    idx = pd.DatetimeIndex(pd.to_datetime(df["date"]))
    vals = [str(x) if x == x else "" for x in df["label"]]
    out = pd.Series(vals, index=idx, name="label", dtype=object)
    return cast(pd.Series, out[out != ""].astype(str))


def model_version(spec: dict) -> str:
    """版本 = 类型 + K + 签名前缀 + 拟合日。哪一项变了都能看出来。"""
    sig = str(spec.get("label_signature", ""))[:8]
    return f"hmm-k{spec.get('k')}-{sig}-{str(spec.get('fit_date', '')).replace('-', '')}"


def _confidence(proba: list[float] | None) -> float | None:
    if not proba:
        return None
    return float(max(proba))


def _payload(asset: str, date, label: str, state_id: int | None,
             proba: list[float] | None, spec: dict) -> dict:
    c = cfg.get(asset)
    return {
        "date": str(pd.Timestamp(date).date()),
        "asset": asset,
        "state_id": state_id,
        "label": label,
        "label_name": c.LABEL_NAME.get(label, label),
        "state_probabilities": proba or [],
        "confidence": _confidence(proba),
        "model": {
            "kind": "hmm",
            "k": spec.get("k"),
            # 老的 current.json 没存 features，用配置兜底
            "features": list(spec.get("features") or c.FEATURES),
            "fit_date": spec.get("fit_date"),
            "signature": spec.get("label_signature"),
            "version": model_version(spec),
        },
        "as_of_date": str(pd.Timestamp(date).date()),
    }


def states(asset: str, start: str | None = None,
           end: str | None = None) -> pd.DataFrame:
    """批量：返回 [date, label, label_name]。给面板构建用。"""
    s = cast(pd.Series, load_labels(asset))
    if s.empty:
        return pd.DataFrame(columns=["date", "label", "label_name"])
    if start:
        s = cast(pd.Series, s[s.index >= pd.Timestamp(start)])
    if end:
        s = cast(pd.Series, s[s.index <= pd.Timestamp(end)])
    c = cfg.get(asset)
    return pd.DataFrame({
        "date": s.index,
        "label": s.values,
        "label_name": [c.LABEL_NAME.get(v, v) for v in s.values],
    })


def current(asset: str, features: pd.DataFrame | None = None) -> dict:
    """单点：今天的状态。给了 features 就现算后验，否则只回标签。"""
    spec = load_spec(asset)
    labels = load_labels(asset)
    if labels.empty:
        return {}
    label = str(labels.iloc[-1])
    date = labels.index[-1]
    sid, proba = None, None

    if features is not None and len(features):
        try:
            est = make("hmm", k=spec.get("k"))
            lw = load_last_window(asset)
            res = est.latest(features, lw)
            if res:
                label = res.get("label") or label
                sid, proba = res.get("state_id"), res.get("proba")
        except Exception:  # noqa: BLE001
            pass          # 现算失败就退回库里的标签，不让页面挂

    return _payload(asset, date, label, sid, proba, spec)


def fit(asset: str, panel: pd.DataFrame, *, estimator: str = "hmm",
        labeler_factory, signature_fn, verbose: bool = True) -> dict:
    """训练并返回 {spec, labels, last, profile, k_table}（不落盘）。"""
    from state_layer.features import build

    c = cfg.get(asset)
    feats = build(asset, panel)
    est = make(estimator, k=c.K)
    res = est.fit(feats, labeler_factory=labeler_factory,
                  name_by_value=c.LABEL_NAME, signature_fn=signature_fn,
                  asset=asset, extreme_params=c.EXTREME_PARAMS, verbose=verbose)
    # 极端规则在这里统一覆盖（estimator 内部已做一次，这里是幂等兜底）
    ext = extreme.classify(feats, c.EXTREME_PARAMS)
    res["labels"] = extreme.apply(res["labels"], ext)
    return res
