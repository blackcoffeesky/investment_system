"""Gaussian HMM：认领常规行情，认领不设上限（链里通常放在最后）。

K 由**模型内部**决定：`params["k"]` 给定就用它；给 None 才自己按 BIC + 分离度选。
选模型是模型自己的事——对外"这套状态好不好"才由 `metrics.py` 评价。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from state_layer.model.base import Model, resolve_claim
from state_layer.model.hmm import serialize
from state_layer.model.hmm.fit import RegimeFit, fit_regime

DEFAULTS = {
    "k": 3,
    "seed": 42,
    "min_train": 500,
    "refit_every": 60,
    "window": 250,
    "bh_threshold": 0.4,
}


class GaussianHMM(Model):
    kind = "hmm"

    def __init__(self, params: dict | None = None):
        merged = {**DEFAULTS, **(params or {})}
        super().__init__(merged)
        self._fit: RegimeFit | None = None
        self.last_window: dict | None = None
        self.k_table: pd.DataFrame | None = None
        self.k_used: int | None = None

    # ---------------- 能力：模型自己记录的列
    def valid_index(self, features: pd.DataFrame) -> pd.Index:
        cols = list(self.params.get("features") or features.columns)
        cols = [c for c in cols if c in features.columns]
        return features[cols].dropna().index if cols else features.dropna().index

    # ---------------- 训练
    def fit(self, features: pd.DataFrame, *, claim: pd.Index | None = None,
            labeler=None, **kw) -> pd.Index:
        p = self.params
        allowed = resolve_claim(self, features, claim)
        sub = features.loc[allowed]

        k = p.get("k")
        self.k_table = None
        if k is None:
            from state_layer.model.hmm import select as S

            k, self.k_table = S.select_k(sub, seed=int(p["seed"]),
                                         bh_threshold=float(p["bh_threshold"]))
        self.k_used = int(k)

        self._fit = fit_regime(sub, k=int(k), seed=int(p["seed"]),
                               bh_threshold=float(p["bh_threshold"]))
        self.last_window = self._fit.window_dict()

        if labeler is not None:
            labels, _proba, last = self._fit.walk_forward(
                sub, labeler=labeler, min_train=int(p["min_train"]),
                refit_every=int(p["refit_every"]), window=int(p["window"]))
            self.labels_ = labels
            if last:
                self.last_window = last
        else:
            # 没有标签器：至少把状态编号留下，别让下游拿到一片空
            dec = self._fit.states.reindex(allowed)
            self.labels_ = dec.map(lambda v: f"s{int(v)}" if pd.notna(v) else "")

        return allowed.difference(allowed)      # 全部认领，下游拿到空

    # ---------------- 推断
    def infer(self, features: pd.DataFrame, *,
              claim: pd.Index | None = None) -> dict:
        if self.last_window is None:
            raise RuntimeError("模型还没 fit / 没有 last_window")
        allowed = resolve_claim(self, features, claim)

        from shared.data_processing import prep_features

        cols = list(self.last_window["features"])
        sub = features.reindex(columns=cols).loc[allowed].dropna()
        if sub.empty:
            return {"states": pd.Series(dtype=object),
                    "remaining_index": allowed, "proba": None}

        hmm = serialize.rebuild(self.last_window)
        lo = pd.Series(self.last_window["clip_lo"], index=cols)
        hi = pd.Series(self.last_window["clip_hi"], index=cols)
        scaler = {"mean": self.last_window["scaler_mean"],
                  "scale": self.last_window["scaler_scale"]}

        names = {int(k): str(v)
                 for k, v in (self.last_window.get("state_labels") or {}).items()}
        window = int(self.params["window"])

        states: dict = {}
        proba: dict = {}
        for d in sub.index:
            tail = sub.loc[:d].tail(window)
            X = prep_features(tail, scaler, lo, hi)
            post = np.asarray(hmm.predict_proba(X)[-1], dtype=float)
            sid = int(np.argmax(post))
            states[d] = names.get(sid, sid)
            proba[d] = post

        n_state = len(next(iter(proba.values()))) if proba else 0
        return {
            "states": pd.Series(states, dtype=object),
            "remaining_index": allowed.difference(sub.index),
            "proba": pd.DataFrame.from_dict(
                proba, orient="index", columns=[f"p{i}" for i in range(n_state)]),
        }

    def state_params(self) -> dict[str, Any] | None:
        if self._fit is None:
            return None
        f = self._fit
        return {"means": f.means, "covars": f.covars,
                "transmat": f.transmat, "startprob": f.startprob}

    # ---------------- 序列化
    def to_dict(self) -> dict[str, Any]:
        if self._fit is None:
            raise RuntimeError("模型还没 fit")
        data = serialize.to_numpy(self._fit.spec())
        data["kind"] = self.kind
        data["params"] = dict(self.params)
        data["last_window"] = serialize.to_numpy(self.last_window or {})
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GaussianHMM":
        m = cls(data.get("params") or {"k": data.get("k")})
        m._fit = RegimeFit.from_spec(data)
        m.last_window = data.get("last_window") or m._fit.window_dict()
        m.k_used = int(data.get("k") or m.params.get("k") or m._fit.k)
        return m

    def transition(self, features: pd.DataFrame | None = None) -> dict | None:
        if self.last_window is None:
            return None
        lw = self.last_window
        names = {int(k): str(v) for k, v in (lw.get("state_labels") or {}).items()}
        P = np.asarray(lw["transmat"], dtype=float)
        durations = {names[i]: float(1.0 / max(1e-9, 1.0 - P[i, i]))
                     for i in range(len(P)) if i in names}
        out: dict[str, Any] = {"durations": durations, "transmat": P.tolist()}
        if features is not None:
            res = self.infer(features)
            proba = res.get("proba")
            if proba is not None and len(proba):
                p_today = proba.iloc[-1].to_numpy()
                p_next = p_today @ P
                by_label: dict[str, float] = {}
                for sid, pr in enumerate(p_next):
                    lab = names.get(int(sid))
                    if lab is not None:
                        by_label[lab] = by_label.get(lab, 0.0) + float(pr)
                cur = int(np.argmax(p_today))
                cur_label = names.get(cur, "")
                nxt = sorted(((k, v) for k, v in by_label.items() if k != cur_label),
                             key=lambda kv: -kv[1])[:2]
                out.update({
                    "label": cur_label,
                    "stay_prob": float(by_label.get(cur_label, 0.0)),
                    "duration": durations.get(cur_label),
                    "p_next": nxt,
                    "posterior": {names.get(i, str(i)): float(p)
                                  for i, p in enumerate(p_today)},
                    "confident": bool(np.max(p_today) >= 0.6),
                })
        return out
