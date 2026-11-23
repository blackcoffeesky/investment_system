"""Student-t HMM：**模型层**，只认"特征矩阵 + K"，不知道组、不知道资产。

- 发射分布：多元 Student-t（尺度矩阵 Σ，ν 固定）
- 训练：自写 EM（`fit_t_hmm`）；默认 KMeans 初值
- 推断：**只用过滤概率** `P(S_t | X_1:t)`（禁止平滑——平滑会用未来数据）
- 命名：由调用方传 `labeler`（资产语义），模型只交编号

两种拟合方式（参数来自 config，代码里不写死）：
- `walk_forward=True`：逐日因果解码（每 `refit_every` 天重估一次），生产用
- `walk_forward=False`：整段拟一次再过滤（实验口径，快）
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from state_layer.model.base import Model, ModelOutput, to_numpy
from state_layer.model.thmm.fit import DEFAULTS as FIT_DEFAULTS
from state_layer.model.thmm.fit import fit_t_hmm
from state_layer.model.thmm.numerics import filtered_predict, t_logpdf

DEFAULTS: dict[str, Any] = {
    "k": 3,
    "nu": FIT_DEFAULTS["nu"],
    "max_iter": FIT_DEFAULTS["max_iter"],
    "rtol": FIT_DEFAULTS["rtol"],
    "features": None,          # None = 用传入特征的全部列
    "walk_forward": True,
    "min_train": 500,
    "refit_every": 60,
    "window": 250,
}


def _standardize(hist: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """按给定窗口的 mean/std(ddof=0) 标准化（**只用这段窗口的数据**）。"""
    mean = hist.mean().to_numpy(dtype=float)
    scale = hist.std(ddof=0).to_numpy(dtype=float)
    scale = np.where(scale > 1e-12, scale, 1.0)
    return ((hist.to_numpy(dtype=float) - mean) / scale), mean, scale


class StudentTHMM(Model):
    kind = "thmm"

    def __init__(self, params: dict | None = None):
        super().__init__({**DEFAULTS, **(params or {})})

    # ---------------- 能力
    def _cols(self, features: pd.DataFrame) -> list[str]:
        cols = list(self.params.get("features") or features.columns)
        return [c for c in cols if c in features.columns]

    def valid_index(self, features: pd.DataFrame) -> pd.Index:
        cols = self._cols(features)
        return features[cols].dropna().index if cols else features.dropna().index

    # ---------------- 训练
    def fit(self, features: pd.DataFrame, *, labeler=None) -> None:
        cols = self._cols(features)
        sub = features.loc[self.valid_index(features), cols].dropna()
        if len(sub) < 50:
            raise ValueError(f"有效样本只有 {len(sub)}，无法拟合 t-HMM")
        k = int(self.params["k"])
        if self.params.get("walk_forward", True) and labeler is not None:
            self._fit_walk_forward(sub, k, labeler)
        else:
            self._fit_all(sub, k, labeler)

    def _fit_all(self, sub: pd.DataFrame, k: int, labeler) -> None:
        X, mean, scale = _standardize(sub)
        f = fit_t_hmm(X, k, nu=float(self.params["nu"]),
                      max_iter=int(self.params["max_iter"]),
                      rtol=float(self.params["rtol"]))
        log_b = np.column_stack(
            [t_logpdf(X, f["means"][i], f["covars"][i], f["nu"]) for i in range(k)])
        post, _ = filtered_predict(log_b, f["a"], f["pi"])
        self.proba_ = pd.DataFrame(post, index=sub.index,
                                   columns=list(range(k)))
        self.states_ = pd.Series(post.argmax(axis=1), index=sub.index, name="state")
        self.last_window = self._window(f, mean, scale, sub.columns,
                                        labeler(f["means"] * scale + mean) if labeler else {})
        if labeler is not None:
            self.labels_ = self.states_.map(self.last_window["state_labels"])

    def _fit_walk_forward(self, sub: pd.DataFrame, k: int,
                          labeler) -> None:
        """逐日因果解码：每天只用当日及之前的数据。与 `RegimeFit.walk_forward` 同构。

        ⚠️ 这段循环与 Gaussian 的实现是**同构**的（不是同一份代码）。步 5 收尾时抽公共
        helper——现在抽会动到黄金 0 格差异的路径，风险不值得。
        """
        p = self.params
        min_train = int(p["min_train"])
        refit_every = int(p["refit_every"])
        window = int(p["window"])
        if len(sub) <= min_train:
            raise ValueError(f"有效样本 {len(sub)} ≤ min_train={min_train}")

        cols = list(sub.columns)
        states = pd.Series(np.nan, index=sub.index, name="state")
        labels = pd.Series(np.nan, index=sub.index, dtype=object, name="label")
        proba = pd.DataFrame(np.nan, index=sub.index, columns=list(range(k)))
        f = None
        mean = scale = None
        label_map: dict[int, str] = {}
        refit_dates: list[str] = []

        for i in range(min_train, len(sub)):
            if f is None or (i - min_train) % refit_every == 0:
                hist = sub.iloc[: i + 1]
                X, mean, scale = _standardize(hist)
                f = fit_t_hmm(X, k, nu=float(p["nu"]),
                              max_iter=int(p["max_iter"]), rtol=float(p["rtol"]))
                raw = f["means"] * scale + mean
                label_map = labeler(pd.DataFrame(raw, columns=cols))
                refit_dates.append(str(pd.Timestamp(sub.index[i]).date()))

            tail = sub.iloc[: i + 1].tail(window)
            Xt = (tail.to_numpy(dtype=float) - mean) / scale
            log_b = np.column_stack(
                [t_logpdf(Xt, f["means"][j], f["covars"][j], f["nu"])
                 for j in range(k)])
            post, _ = filtered_predict(log_b, f["a"], f["pi"])
            proba.iloc[i] = post[-1]
            sid = int(np.argmax(post[-1]))
            states.iloc[i] = sid
            labels.iloc[i] = label_map.get(sid)

        self.proba_ = proba.dropna(how="all").loc[:, :]
        self.states_ = states.dropna().astype(int)
        self.last_window = self._window(f, mean, scale, cols, label_map)
        self.last_window["refit_dates"] = refit_dates
        # ⚠️ 必须用**逐日**标签：拿最后一次 refit 的映射去重写整段历史，
        # 会让追加数据改变所有历史标签（2026-11-23 的 A 股 prefix_diff 事故）。
        self.labels_ = labels.dropna()

    @staticmethod
    def _window(f: dict, mean: np.ndarray, scale: np.ndarray,
                cols, label_map: dict) -> dict:
        mapped = {int(k): str(v) for k, v in label_map.items()}
        return to_numpy({
            "means": f["means"], "covars": f["covars"],
            "transmat": f["a"], "startprob": f["pi"], "nu": f["nu"],
            "scaler_mean": mean, "scaler_scale": scale,
            "features": list(cols), "state_labels": mapped,
        })

    # ---------------- 推断
    def predict(self, features: pd.DataFrame) -> ModelOutput:
        if self.last_window is None:
            raise RuntimeError("模型还没 fit / 没有 last_window")
        lw = self.last_window
        cols = list(lw["features"])
        valid = features.reindex(columns=cols).dropna()
        if valid.empty:
            return ModelOutput(pd.Series(dtype="int64"), None)

        mean = np.asarray(lw["scaler_mean"], dtype=float)
        scale = np.asarray(lw["scaler_scale"], dtype=float)
        X = (valid.to_numpy(dtype=float) - mean) / scale
        k = len(lw["means"])
        log_b = np.column_stack(
            [t_logpdf(X, np.asarray(lw["means"][i], dtype=float),
                      np.asarray(lw["covars"][i], dtype=float), float(lw["nu"]))
             for i in range(k)])
        post, _ = filtered_predict(
            log_b, np.asarray(lw["transmat"], dtype=float),
            np.asarray(lw["startprob"], dtype=float))
        return ModelOutput(
            states=pd.Series(post.argmax(axis=1), index=valid.index, name="state"),
            proba=pd.DataFrame(post, index=valid.index, columns=list(range(k))))

    # ---------------- 评价 / 序列化
    def state_params(self) -> dict[str, Any] | None:
        if self.last_window is None:
            return None
        lw = self.last_window
        return {"means": np.asarray(lw["means"], dtype=float),
                "covars": np.asarray(lw["covars"], dtype=float),
                "transmat": np.asarray(lw["transmat"], dtype=float),
                "startprob": np.asarray(lw["startprob"], dtype=float)}

    def to_dict(self) -> dict[str, Any]:
        if self.last_window is None:
            raise RuntimeError("模型还没 fit")
        params = {k: v for k, v in self.params.items() if k != "features"}
        return {"kind": self.kind, "params": to_numpy(params),
                "features": to_numpy(list(self.last_window["features"])),
                **to_numpy({k: self.last_window[k] for k in
                            ("means", "covars", "transmat", "startprob", "nu",
                             "scaler_mean", "scaler_scale", "state_labels")})}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StudentTHMM":
        params = dict(data.get("params") or {})
        params["features"] = list(data.get("features") or [])
        m = cls(params)
        m.last_window = {
            "means": data["means"], "covars": data["covars"],
            "transmat": data["transmat"], "startprob": data["startprob"],
            "nu": float(data["nu"]),
            "scaler_mean": data["scaler_mean"], "scaler_scale": data["scaler_scale"],
            "features": list(data.get("features") or []),
            "state_labels": {int(k): str(v)
                             for k, v in (data.get("state_labels") or {}).items()},
        }
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
                nxt = sorted(((k, v) for k, v in by_label.items() if k != cur_label),
                             key=lambda kv: -kv[1])[:2]
                out.update({"label": cur_label,
                            "stay_prob": float(by_label.get(cur_label, 0.0)),
                            "duration": durations.get(cur_label),
                            "p_next": nxt,
                            "posterior": {names.get(i, str(i)): float(p)
                                          for i, p in enumerate(p_today)},
                            "confident": bool(np.max(p_today) >= 0.6)})
        return out
