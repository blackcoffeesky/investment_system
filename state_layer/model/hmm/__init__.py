"""Gaussian HMM：**模型层**，只认"特征矩阵 + K"，不知道组、不知道资产。

- 发射分布：多元高斯（hmmlearn，`full` 协方差）
- 训练：`walk_forward=True` 时逐日因果解码（每 `refit_every` 天重估）；
  `False` 时整段拟一次再解码（实验口径）
- 推断：逐日滚动窗口内的**最后一点**后验（等价于过滤概率，不掺未来数据）
- 命名：由调用方传 `labeler`；模型只交**编号**

黄金的 0 格差异依赖这里的 `walk_forward` 路径与参数，**改之前先看
`state_assets/gold/estimator.py` 顶部的说明**。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from state_layer.model.base import Model, ModelOutput, to_numpy
from state_layer.model.hmm.fit import RegimeFit, fit_regime
from state_layer.model.hmm.params import rebuild

DEFAULTS: dict[str, Any] = {
    "k": 3,
    "seed": 42,
    "min_train": 500,
    "refit_every": 60,
    "window": 250,
    "bh_threshold": 0.4,
    "walk_forward": True,
    "features": None,          # None = 用传入特征的全部列
}


class GaussianHMM(Model):
    kind = "hmm"

    def __init__(self, params: dict | None = None):
        super().__init__({**DEFAULTS, **(params or {})})
        self._fit: RegimeFit | None = None

    # ---------------- 能力
    def valid_index(self, features: pd.DataFrame) -> pd.Index:
        """**只用于对齐**：按模型实际用到的列判断非空。"""
        cols = list(self.params.get("features") or features.columns)
        cols = [c for c in cols if c in features.columns]
        return features[cols].dropna().index if cols else features.dropna().index

    # ---------------- 训练
    def fit(self, features: pd.DataFrame, *, labeler=None) -> None:
        p = self.params
        k = p.get("k")
        if k is None:
            raise ValueError(
                "K 必须由 config 给定：自动选 K（BIC）已于 2026-11-22 随 select.py 退役")
        sub = features.loc[self.valid_index(features)]
        if len(sub) < 100:
            raise ValueError(f"有效样本只有 {len(sub)}，无法拟合 HMM")

        self._fit = fit_regime(sub, int(k), seed=int(p["seed"]),
                               bh_threshold=float(p["bh_threshold"]))

        if p.get("walk_forward", True) and labeler is not None:
            labels, proba, last = self._fit.walk_forward(
                sub, labeler=labeler,
                min_train=int(p["min_train"]),
                refit_every=int(p["refit_every"]),
                window=int(p["window"]))
            self.labels_ = labels
            self.proba_ = proba
            self.last_window = last
            # proba 在 min_train 之前是 NaN；先丢掉空行再取编号
            labeled = proba.dropna(how="all")
            self.states_ = labeled.idxmax(axis=1).astype(int)
        else:
            self.states_ = self._fit.states.astype(int)
            self.proba_ = self._fit.proba
            self.last_window = self._fit.window_dict()
            if labeler is not None:
                mapped = labeler(self._fit.raw_means())
                self.last_window["state_labels"] = {int(k_): str(v)
                                                    for k_, v in mapped.items()}
                self.labels_ = self.states_.map(self.last_window["state_labels"])

    # ---------------- 推断
    def predict(self, features: pd.DataFrame) -> ModelOutput:
        if self.last_window is None:
            raise RuntimeError("模型还没 fit / 没有 last_window")
        from shared.utils.data_processing import prep_features

        lw = self.last_window
        cols = list(lw["features"])
        sub = features.reindex(columns=cols).loc[self.valid_index(features)].dropna()
        if sub.empty:
            return ModelOutput(pd.Series(dtype="int64"), None)

        hmm = rebuild(lw)
        lo = pd.Series(np.asarray(lw["clip_lo"], dtype=float), index=cols)
        hi = pd.Series(np.asarray(lw["clip_hi"], dtype=float), index=cols)
        scaler = {"mean": np.asarray(lw["scaler_mean"], dtype=float),
                  "scale": np.asarray(lw["scaler_scale"], dtype=float)}
        window = int(self.params["window"])

        states: dict[pd.Timestamp, int] = {}
        rows: dict[pd.Timestamp, np.ndarray] = {}
        for d in sub.index:
            tail = sub.loc[:d].tail(window)
            X = prep_features(tail, scaler, lo, hi)
            post = np.asarray(hmm.predict_proba(X)[-1], dtype=float)
            states[d] = int(np.argmax(post))
            rows[d] = post

        k = len(next(iter(rows.values())))
        return ModelOutput(
            states=pd.Series(states, dtype="int64", name="state"),
            proba=pd.DataFrame.from_dict(rows, orient="index",
                                         columns=list(range(k))))

    # ---------------- 评价 / 序列化
    def state_params(self) -> dict[str, Any] | None:
        return self._fit.state_params() if self._fit is not None else None

    def to_dict(self) -> dict[str, Any]:
        if self._fit is None:
            raise RuntimeError("模型还没 fit")
        data = to_numpy(self._fit.spec())
        data["kind"] = self.kind
        data["params"] = to_numpy(dict(self.params))
        data["last_window"] = to_numpy(self.last_window or {})
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GaussianHMM":
        m = cls(data.get("params") or {"k": data.get("k")})
        m._fit = RegimeFit.from_spec(data)
        m.last_window = data.get("last_window") or m._fit.window_dict()
        return m

    # ---------------- 状态黏性
    def transition(self, features: pd.DataFrame | None = None) -> dict | None:
        if self.last_window is None:
            return None
        lw = self.last_window
        names = {int(k): str(v) for k, v in (lw.get("state_labels") or {}).items()}
        P = np.asarray(lw["transmat"], dtype=float)
        durations = {names[i]: float(1.0 / max(1e-9, 1.0 - P[i, i]))
                     for i in range(len(P)) if i in names}
        out: dict[str, Any] = {"durations": durations, "transmat": P.tolist()}
        if features is not None and len(features):
            res = self.predict(features)
            if res.proba is not None and len(res.proba):
                p_today = res.proba.iloc[-1].to_numpy()
                p_next = p_today @ P
                by_label: dict[str, float] = {}
                for sid, pr in enumerate(p_next):
                    lab = names.get(int(sid))
                    if lab is not None:
                        by_label[lab] = by_label.get(lab, 0.0) + float(pr)
                cur = int(np.argmax(p_today))
                cur_label = names.get(cur, "")
                nxt = sorted(((k_, v) for k_, v in by_label.items() if k_ != cur_label),
                             key=lambda kv: -kv[1])[:2]
                out.update({"label": cur_label,
                            "stay_prob": float(by_label.get(cur_label, 0.0)),
                            "duration": durations.get(cur_label),
                            "p_next": nxt,
                            "posterior": {names.get(i, str(i)): float(p)
                                          for i, p in enumerate(p_today)},
                            "confident": bool(np.max(p_today) >= 0.6)})
        return out
