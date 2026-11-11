"""HMM 估计器：P1 阶段先**包一层** `regime_engine`，不搬内部实现。

为什么先包一层
--------------
`regime_engine` 里有几块东西和状态层职责重合（prep 的前处理、extreme 规则、
align 的命名），但 causual 的边估计还在用它做分组。P1 只做"迁移接口"，
把搬迁留到 P3——那时因果层不再依赖 regime 分组，才好动。

实现来源
--------
拟合与推断逻辑复制自 `causual/causual_core/regime_fit.py` 与 `regime_infer.py`。
P3 删掉那两份之后，这里就是唯一实现。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

import regime_engine
from regime_engine import extreme as X
from regime_engine.align import make_labeler
from regime_engine.serialize import make_fit_id, to_numpy


class HMMEstimator:
    """高斯 HMM + 分布滞后无关的 walk-forward 解码。"""

    name = "hmm"

    def __init__(self, *, k: int | None = None, seed: int = 42,
                 min_train: int = 500, refit_every: int = 60,
                 k_candidates: tuple[int, ...] = (2, 3, 4)):
        self.k = k
        self.seed = seed
        self.min_train = min_train
        self.refit_every = refit_every
        self.k_candidates = k_candidates

    # ---------------- 训练
    def fit(self, features: pd.DataFrame, *, labeler_factory,
            name_by_value: dict[str, str], signature_fn,
            asset: str, extreme_params: X.ExtremeParams | None = None,
            verbose: bool = True) -> dict:
        ext = X.classify(features, extreme_params)
        regular_mask = ext == ""
        reg_feat = features.loc[regular_mask]

        if len(reg_feat.dropna()) < self.min_train + 50:
            raise ValueError(f"剔除极端日后只剩 {len(reg_feat.dropna())} 行，不足以拟合")

        k = self.k
        k_table = None
        if k is None:
            k, k_table = regime_engine.select_k(reg_feat, candidates=self.k_candidates,
                                                seed=self.seed)
            if verbose:
                print(k_table.round(4).to_string())
                print(f"-> K = {k}（候选 {self.k_candidates}）")

        fit = regime_engine.fit_regime(reg_feat, k=k, seed=self.seed)
        ref_means = fit.raw_means()
        labeler = make_labeler(ref_means, labeler_factory(ref_means))
        reg_labels, proba, last = fit.walk_forward(
            reg_feat, labeler=labeler, min_train=self.min_train,
            refit_every=self.refit_every)

        labels = reg_labels.reindex(features.index).fillna("")
        labels.loc[ext != ""] = ext[ext != ""]

        spec = {
            "asset": asset,
            "k": int(k),
            "features": list(features.columns),
            "fit_date": str(features.dropna().index[-1].date()),
            "label_signature": signature_fn(labels),
            "used_labels": sorted({v for v in labels if v}),
            "min_bhattacharyya": float(getattr(fit, "min_bhattacharyya", 0.0) or 0.0),
            "fit_id": make_fit_id(asset, features, k),
        }
        return {"spec": spec, "labels": labels, "fit": fit, "last": last,
                "profile": self._profile(labels, name_by_value),
                "k_table": k_table}

    # ---------------- 推断
    def labels(self, features: pd.DataFrame, last_window: dict,
               window: int = 250) -> pd.Series:
        """把标签外推到断档期：逐日用"最后一个窗口的模型"预测。"""
        labels = {int(k): str(v) for k, v in last_window.get("state_labels", {}).items()}
        out = {}
        for d in features.index:
            st, p = self._current(features.loc[:d], last_window, window)
            if p is None:
                continue
            out[d] = labels.get(int(np.argmax(p)), "")
        return pd.Series(out, dtype=object)

    def latest(self, features: pd.DataFrame, last_window: dict,
               window: int = 250) -> dict:
        sid, proba = self._current(features, last_window, window)
        if proba is None:
            return {}
        labels = {int(k): str(v) for k, v in last_window.get("state_labels", {}).items()}
        return {"state_id": sid, "proba": [float(x) for x in proba],
                "label": labels.get(int(sid), ""),
                "label_proba": {labels.get(i, str(i)): float(p)
                                for i, p in enumerate(proba)}}

    # ---------------- 内部
    def _prep(self, features: pd.DataFrame, lw: dict,
              window: int = 250) -> np.ndarray | None:
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

    def _current(self, features: pd.DataFrame, lw: dict,
                 window: int = 250) -> tuple[int | None, np.ndarray | None]:
        if not lw:
            return None, None
        X_in = self._prep(features, lw, window)
        if X_in is None:
            return None, None
        hmm = regime_engine.serialize.rebuild({"k": len(lw["means"]), **lw})
        proba = np.asarray(hmm.predict_proba(X_in)[-1], dtype=float)
        return int(np.argmax(proba)), proba

    @staticmethod
    def _profile(labels: pd.Series, name_by_value: dict[str, str]) -> list[dict]:
        s = labels[labels != ""]
        if s.empty:
            return []
        out = []
        for lab, n in s.value_counts().items():
            out.append({"label": lab, "name": name_by_value.get(lab, lab),
                        "n": int(n), "share": float(n / len(s))})
        return out
