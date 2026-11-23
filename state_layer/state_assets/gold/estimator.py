"""黄金的状态装配：**一个**分组，行为与 2026-11-22 之前逐位一致。

0 格差异的前置条件（改任何一个 = 换了一套状态体系，不是重构）
--------------------------------------------------------------
`GOLD_WALK_FORWARD` 的 5 个值必须与旧实现逐字一致，`test_gold_parity.py` 断言它们。
"""

from __future__ import annotations

import pandas as pd

from state_layer import state_assets, store
from state_layer.model import GaussianHMM
from state_layer.state_assets import base
from state_layer.state_assets.gold import config as C

# 这 5 个值与旧实现逐字一致（0 格差异验收的前置条件）。
# 改任何一个都会让黄金标签变化 —— 那是另一个决定，不是重构。
GOLD_WALK_FORWARD = dict(min_train=500, refit_every=60, window=250, seed=42, k=4)

GROUP = "raw"


class Estimator(base.Estimator):
    asset = "gold"

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self._models: dict[str, GaussianHMM] = {}

    # ---------------- 装配
    @classmethod
    def load(cls, asset: str = "gold") -> "Estimator":
        inst = cls()
        for name, blob in (store.load_params(asset).get("groups") or {}).items():
            inst._models[name] = GaussianHMM.from_dict(blob["model"])
        return inst

    def _model_params(self) -> dict:
        return {**GOLD_WALK_FORWARD, "features": list(C.FEATURES)}

    # ---------------- 训练 / 推断
    def fit(self, panel: pd.DataFrame) -> dict[str, base.GroupResult]:
        feats = state_assets.load(self.asset).features.build(panel)
        feats = feats[list(C.FEATURES)]
        model = GaussianHMM(self._model_params())
        model.fit(feats, labeler=C.name_states)
        self._models[GROUP] = model
        return {GROUP: self._result(model, feats)}

    def predict(self, panel: pd.DataFrame) -> dict[str, base.GroupResult]:
        feats = state_assets.load(self.asset).features.build(panel)
        feats = feats[list(C.FEATURES)]
        out: dict[str, base.GroupResult] = {}
        for name, model in self._models.items():
            res = model.predict(feats)
            labels = {int(k): str(v) for k, v in
                      (model.last_window or {}).get("state_labels", {}).items()}
            out[name] = base.GroupResult(
                name=name, title=C.GROUP_TITLE.get(name, name), k=GOLD_WALK_FORWARD["k"],
                states=res.states, labels=labels, proba=res.proba,
                features=tuple(C.FEATURES), window=model.last_window)
        return out

    # ---------------- 落盘
    def _result(self, model: GaussianHMM, feats: pd.DataFrame) -> base.GroupResult:
        labels = {int(k): str(v) for k, v in
                  (model.last_window or {}).get("state_labels", {}).items()}
        fit_date = str(pd.Timestamp(model.states_.index[-1]).date())
        return base.GroupResult(
            name=GROUP, title=C.GROUP_TITLE.get(GROUP, GROUP),
            k=GOLD_WALK_FORWARD["k"],
            states=model.states_.astype(int),
            labels=labels,
            label_values=model.labels_,          # walk-forward 的逐日权威标签
            proba=model.proba_,
            features=tuple(C.FEATURES),
            usable=True,
            fit_id=base.make_fit_id(self.asset, GROUP, GOLD_WALK_FORWARD["k"],
                                    C.FEATURES, labels, fit_date),
            window=model.last_window,
        )

    def spec(self) -> dict:
        meta = {}
        for name, model in self._models.items():
            labels = {int(k): str(v) for k, v in
                      (model.last_window or {}).get("state_labels", {}).items()}
            meta[name] = {
                "title": C.GROUP_TITLE.get(name, name),
                "k": GOLD_WALK_FORWARD["k"],
                "features": list(C.FEATURES),
                "labels": labels,
                "usable": True,
                "fit_id": base.make_fit_id(
                    self.asset, name, GOLD_WALK_FORWARD["k"], C.FEATURES, labels,
                    str(pd.Timestamp(model.states_.index[-1]).date())),
                "label_signature": base.label_fingerprint(model.labels_),
                "refit_dates": list((model.last_window or {}).get("refit_dates") or []),
            }
        return {"primary": C.PRIMARY_GROUP, "groups": meta}

    def to_params(self) -> dict:
        return {"groups": {n: {"model": m.to_dict(), "window": m.last_window}
                           for n, m in self._models.items()}}
