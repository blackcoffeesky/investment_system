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

    p = ROOT / "state_layer" / "engine" / "saved_models" / asset / "hmm_last.pkl"
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


def extend(asset: str, features: pd.DataFrame,
           labels: pd.Series | None = None) -> pd.Series:
    """把标签外推到断档期（上次 fit 之后到最新一天）。

    这是让卡片能在不重跑 regime 的情况下拿到当天状态的原因。
    极端日走规则，常规日用最后一个窗口的模型逐日后验 argmax。

    `features` 由调用方传入（它来自面板，而面板是消费方的事）——
    状态层只负责"拿特征 → 出标签"。
    """
    lab = cast(pd.Series, load_labels(asset) if labels is None else labels)
    if lab.empty or features is None or features.empty:
        return lab

    c = cfg.get(asset)
    spec = load_spec(asset)
    valid = features.dropna().index
    if not len(valid):
        return lab
    # 注意：调用方可能传入"已 reindex 到最新日期、空格子仍是 ''"的标签序列，
    # 所以断档起点要按**最后一个非空标签**算，不能取 index.max()。
    filled = cast(pd.Series, lab[lab.astype(str) != ""])
    if filled.empty:
        return lab
    last_lab = filled.index.max()
    gap = valid[valid > last_lab]
    if not len(gap):
        return lab

    try:
        lw = load_last_window(asset)
    except FileNotFoundError:
        print(f"[state] 缺 hmm_last.pkl，{asset} 的标签不外推")
        return lab
    if lw.get("fit_id") != spec.get("fit_id"):
        print(f"[state] hmm_last.pkl 与 {asset} 的 spec fit_id 不一致，不外推")
        return lab

    est = make("hmm", k=spec.get("k"))
    add = est.labels(features, lw)
    add = add[add.index.isin(gap)]
    add = cast(pd.Series, add[add != ""])
    if add.empty:
        return lab

    ext = extreme.classify(features, c.EXTREME_PARAMS).reindex(add.index)
    add = extreme.apply(add, ext)
    span = pd.DatetimeIndex(add.index)
    print(f"[state] 外推 {len(add)} 天标签"
          f"（{span[0].date()} ~ {span[-1].date()}）")
    return pd.concat([filled, add]).sort_index()


def transition(asset: str, features: pd.DataFrame | None = None) -> dict | None:
    """今日后验 × 转移矩阵：留在原状态的概率、平均停留、最可能转向。

    从 `regime_infer.transition_view` 迁来（P3 删掉那边）。
    没有特征或模型文件时返回 None，调用方自己降级。
    """
    import numpy as np

    try:
        lw = load_last_window(asset)
    except FileNotFoundError:
        return None
    if not lw:
        return None

    labels = {int(k): str(v) for k, v in lw.get("state_labels", {}).items()}
    proba = None
    if features is not None and len(features):
        try:
            est = make("hmm")
            res = est.latest(features, lw)
            proba = res.get("proba") if res else None
        except Exception:  # noqa: BLE001
            proba = None
    if not proba:
        return None

    p_today = np.asarray(proba, dtype=float)
    P = np.asarray(lw["transmat"], dtype=float)
    p_next = p_today @ P

    by_label: dict[str, float] = {}
    for sid, pr in enumerate(p_next):
        lab = labels.get(int(sid))
        if lab is not None:
            by_label[lab] = by_label.get(lab, 0.0) + float(pr)

    cur_sid = int(np.argmax(p_today))
    cur_label = labels.get(cur_sid, "")
    durations = {labels[i]: float(1.0 / max(1e-9, 1.0 - P[i, i]))
                 for i in range(len(P)) if i in labels}
    nxt = sorted(((k, v) for k, v in by_label.items() if k != cur_label),
                 key=lambda kv: -kv[1])
    return {
        "label": cur_label,
        "stay_prob": float(by_label.get(cur_label, 0.0)),
        "duration": durations.get(cur_label),
        "p_next": nxt[:2],
        "posterior": {labels.get(i, str(i)): float(p) for i, p in enumerate(p_today)},
        "confident": bool(np.max(p_today) >= 0.6),
    }


def fit(asset: str, panel: pd.DataFrame, *, estimator: str = "hmm",
        labeler_factory=None, signature_fn=None,
        label_module=None, verbose: bool = True) -> dict:
    """训练。`labeler_factory` / `signature_fn` 由调用方给（标签词汇表是资产的）。

    `label_module` 可以传一个带 `name_states` / `signature` / `NAME_BY_VALUE`
    的模块，省得调用方拆开传。
    """
    from state_layer.features import build

    c = cfg.get(asset)
    if label_module is not None:
        labeler_factory = getattr(label_module, "name_states", labeler_factory)
        signature_fn = getattr(label_module, "signature", signature_fn)

    feats = build(asset, panel)
    est = make(estimator, k=c.K)
    res = est.fit(feats, labeler_factory=labeler_factory,
                  name_by_value=c.LABEL_NAME, signature_fn=signature_fn,
                  asset=asset, extreme_params=c.EXTREME_PARAMS, verbose=verbose)
    # 极端规则在这里统一覆盖（estimator 内部已做一次，这里是幂等兜底）
    ext = extreme.classify(feats, c.EXTREME_PARAMS)
    res["labels"] = extreme.apply(res["labels"], ext)
    return res


def save(asset: str, result: dict) -> Path:
    """把拟合结果落盘（P3 起由状态层写）。产物路径保持不变。"""
    import pandas as pd
    from state_layer.engine.serialize import save_named, to_numpy

    d = _dir(asset)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "current.json"
    p.write_text(json.dumps(result["spec"], ensure_ascii=False, indent=2),
                 encoding="utf-8")
    labels = result["labels"]
    pd.DataFrame({"date": labels.index, "label": labels.values}).to_csv(
        d / "labels.csv", index=False, encoding="utf-8-sig")
    if result.get("fit") is not None:
        result["fit"].save(asset)
    if result.get("last"):
        from typing import cast

        payload = dict(cast(dict, to_numpy(result["last"])))
        payload["fit_id"] = result["spec"]["fit_id"]
        save_named(payload, asset, "hmm_last.pkl")
    return p
