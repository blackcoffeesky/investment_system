"""Gaussian HMM 模型：普通行情用 HMM，极端行情由规则覆盖。

组合方式
--------
`RuleGated(inner=GaussianHMM, rules=extreme)` —— 极端规则是**装饰**，
不是 HMM 的一部分。将来换 HSMM 时套同一层门控即可复用。

契约见 `state_layer.model.base.Model`：fit / infer / state_params / to_dict。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from state_layer.model.base import Model
from state_layer.model.hmm import serialize
from state_layer.model.hmm.fit import RegimeFit, fit_regime


class GaussianHMM(Model):
    """参数 + 标准化/裁剪边界，能被 JSON 化地存下来。"""

    kind = "hmm"

    def __init__(self, fit: RegimeFit | None = None):
        self._fit = fit
        self.labels_: pd.Series | None = None
        self.last_window: dict | None = None

    # ---------------- 训练
    def fit(self, features: pd.DataFrame, *, k: int = 3, seed: int = 42,
            labeler=None, min_train: int = 500, refit_every: int = 60,
            window: int = 250, bh_threshold: float = 0.4) -> "GaussianHMM":
        self._fit = fit_regime(features, k=k, seed=seed, bh_threshold=bh_threshold)
        # 先用全样本参数兜底，walk_forward 会给出更精确的"最后窗口"
        self.last_window = self._fit.window_dict()

        if labeler is not None:
            labels, _proba, last = self._fit.walk_forward(
                features, labeler=labeler, min_train=min_train,
                refit_every=refit_every, window=window)
            self.labels_ = labels
            if last:
                self.last_window = last
        return self

    # ---------------- 推断
    def infer(self, features: pd.DataFrame) -> dict[str, Any]:
        """用最后一个窗口的模型，逐日给出样本外状态与后验。"""
        if self.last_window is None:
            raise RuntimeError("模型还没 fit / 没有 last_window")
        from state_layer.features.common import prep_features
        from state_layer.model.hmm import _ops as H

        cols = list(self.last_window["features"])
        sub = features.reindex(columns=cols).dropna()
        if sub.empty:
            return {"states": pd.Series(dtype=object), "proba": pd.DataFrame()}

        hmm = serialize.rebuild(self.last_window)
        lo = pd.Series(self.last_window["clip_lo"], index=cols)
        hi = pd.Series(self.last_window["clip_hi"], index=cols)
        scaler = {"mean": self.last_window["scaler_mean"],
                  "scale": self.last_window["scaler_scale"]}

        states = {}
        proba = {}
        for d in sub.index:
            tail = sub.loc[:d].tail(250)
            X = prep_features(tail, scaler, lo, hi)
            post = np.asarray(hmm.predict_proba(X)[-1], dtype=float)
            states[d] = int(np.argmax(post))
            proba[d] = post

        labels = {int(k): str(v)
                  for k, v in (self.last_window.get("state_labels") or {}).items()}
        st = pd.Series({d: labels.get(v, v) for d, v in states.items()}, dtype=object)
        n_state = len(next(iter(proba.values()))) if proba else len(labels)
        pb = pd.DataFrame.from_dict(proba, orient="index",
                                    columns=[f"p{i}" for i in range(n_state)])
        return {"states": st, "proba": pb}

    def state_params(self) -> dict[str, Any]:
        f = self._fit
        if f is None:
            raise RuntimeError("模型还没 fit")
        return {"means": f.means, "covars": f.covars,
                "transmat": f.transmat, "startprob": f.startprob}

    # ---------------- 序列化
    def to_dict(self) -> dict[str, Any]:
        if self._fit is None:
            raise RuntimeError("模型还没 fit")
        data = serialize.to_numpy(self._fit.spec())
        data["last_window"] = serialize.to_numpy(self.last_window or {})
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GaussianHMM":
        """重建模型：参数 + 最后窗口都在 dict 里，重建后可直接推理。"""
        fit = RegimeFit.from_spec(data)
        m = cls(fit)
        m.last_window = data.get("last_window") or fit.window_dict()
        return m

    def transition(self, features: pd.DataFrame | None = None) -> dict | None:
        """状态黏性：留在原状态的概率、平均停留、最可能转向。"""
        if self.last_window is None:
            return None
        lw = self.last_window
        labels = {int(k): str(v) for k, v in (lw.get("state_labels") or {}).items()}
        P = np.asarray(lw["transmat"], dtype=float)
        durations = {labels[i]: float(1.0 / max(1e-9, 1.0 - P[i, i]))
                     for i in range(len(P)) if i in labels}
        out: dict[str, Any] = {"durations": durations, "transmat": P.tolist()}
        if features is not None:
            res = self.infer(features)
            proba = res.get("proba")
            if proba is not None and len(proba):
                p_today = proba.iloc[-1].to_numpy()
                p_next = p_today @ P
                by_label: dict[str, float] = {}
                for sid, pr in enumerate(p_next):
                    lab = labels.get(int(sid))
                    if lab is not None:
                        by_label[lab] = by_label.get(lab, 0.0) + float(pr)
                cur = int(np.argmax(p_today))
                cur_label = labels.get(cur, "")
                nxt = sorted(((k, v) for k, v in by_label.items() if k != cur_label),
                             key=lambda kv: -kv[1])[:2]
                out.update({
                    "label": cur_label,
                    "stay_prob": float(by_label.get(cur_label, 0.0)),
                    "duration": durations.get(cur_label),
                    "p_next": nxt,
                    "posterior": {labels.get(i, str(i)): float(p)
                                  for i, p in enumerate(p_today)},
                    "confident": bool(np.max(p_today) >= 0.6),
                })
        return out
