"""在线状态后验：用**最后一个 walk-forward 窗口**的模型算今天的后验。

为什么不存 posterior.csv
-----------------------
多一个文件就多一处口径漂移（哪个窗口的？和 hmm.pkl 对得上吗？）。
改成：`walk_forward` 返回 `(labels, daily_proba, last_window)`，
把 `last_window`（参数 + 状态标签映射）塞进 spec，用的时候现算。

这样后验永远和"当日标签所用模型"是同一套参数——因果，且不会对不上。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import regime_engine.serialize as S


def _prep(features: pd.DataFrame, lw: dict, window: int = 250) -> np.ndarray | None:
    cols = list(lw["features"])
    # 必须和 walk_forward 走同一个 prep_features（先裁剪后标准化）
    sub = features.reindex(columns=cols).dropna()
    if sub.empty:
        return None
    sub = sub.tail(window)
    from regime_engine.prep import prep_features

    scaler = {"mean": lw["scaler_mean"], "scale": lw["scaler_scale"]}
    lo = pd.Series(lw["clip_lo"], index=cols)
    hi = pd.Series(lw["clip_hi"], index=cols)
    return prep_features(sub, scaler, lo, hi)


def current(features: pd.DataFrame, last_window: dict | None, window: int = 250):
    """返回 (状态号, 后验向量)。

    状态号取**后验 argmax**，与 `walk_forward` 写进 labels.csv 的口径一致。
    """
    if not last_window:
        return None, None
    X = _prep(features, last_window, window)
    if X is None:
        return None, None
    hmm = S.rebuild({"k": len(last_window["means"]), **last_window})
    proba = np.asarray(hmm.predict_proba(X)[-1], dtype=float)
    return int(np.argmax(proba)), proba


def posterior(features: pd.DataFrame, last_window: dict | None, window: int = 250) -> np.ndarray | None:
    """今天的状态后验（长度 K）。"""
    return current(features, last_window, window)[1]


def predict_labels(features: pd.DataFrame, lw: dict, dates, window: int = 250) -> pd.Series:
    """把标签外推到断档期：逐日用"最后一个窗口的模型"预测。

    这是 daily 能在不重跑 regime 的情况下拿到新一天标签的原因。
    极端日走规则；常规日走后验 argmax（与 walk_forward 同口径）。
    """
    labels = {int(k): str(v) for k, v in lw.get("state_labels", {}).items()}
    out = {}
    for d in dates:
        st, p = current(features.loc[:d], lw, window)
        if p is None:
            continue
        out[d] = labels.get(int(np.argmax(p)), "")
    return pd.Series(out, dtype=object)


def transition_view(last_window: dict, p_today: np.ndarray, cur_state: int | None = None) -> dict:
    """今日后验 × 转移矩阵 = 明日状态分布；再折成标签。"""
    labels = {int(k): str(v) for k, v in last_window["state_labels"].items()}
    P = np.asarray(last_window["transmat"], dtype=float)
    p_next = p_today @ P

    by_label: dict[str, float] = {}
    for sid, p in enumerate(p_next):
        lab = labels.get(int(sid))
        if lab is None:
            continue
        by_label[lab] = by_label.get(lab, 0.0) + float(p)

    # "留在原状态"的概率 = 明日分布里、与**当前 Viterbi 状态**同名的那个概率
    cur_sid = int(cur_state) if cur_state is not None else int(np.argmax(p_today))
    cur_label = labels.get(cur_sid, str(cur_sid))
    post_label = labels.get(int(np.argmax(p_today)), str(int(np.argmax(p_today))))
    stay_prob = by_label.get(cur_label, 0.0)

    durations = {labels[int(i)]: (1.0 / max(1e-9, 1.0 - P[i, i])) for i in range(len(P)) if i in labels}
    return {
        "p_today": {labels.get(i, str(i)): float(p) for i, p in enumerate(p_today)},
        "p_next": dict(sorted(by_label.items(), key=lambda kv: -kv[1])),
        "current_label": cur_label,
        "stay_label": cur_label,
        "stay_prob": stay_prob,
        "posterior_label": post_label,
        "posterior_prob": float(np.max(p_today)),
        "ambiguous": post_label != cur_label,
        "durations": durations,
    }
