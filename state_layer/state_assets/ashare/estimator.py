"""A 股的状态装配。

步 2 只出**一个**分组（`short_price`）——让"装配点取第一个分组"从第一天就成立，
causual 的卡片不会断档。步 3 再补 `short_vol` / `long_price`。

walk-forward 参数只在 `config.WALK_FORWARD`（代码里不写死）。
"""

from __future__ import annotations

import pandas as pd

from state_layer import align, state_assets, store
from state_layer.model import StudentTHMM
from state_layer.state_assets import base
from state_layer.state_assets.ashare import config as C

GROUP = "short_price"


class Estimator(base.Estimator):
    asset = "ashare"

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self._models: dict[str, StudentTHMM] = {}

    # ---------------- 装配
    @classmethod
    def load(cls, asset: str = "ashare") -> "Estimator":
        inst = cls()
        for name, blob in (store.load_params(asset).get("groups") or {}).items():
            inst._models[name] = StudentTHMM.from_dict(blob["model"])
        return inst

    @property
    def warmup_bars(self) -> int:
        return max(int(v) for v in C.WARMUP_BARS.values())

    def _model_params(self, name: str) -> dict:
        return {**C.WALK_FORWARD, "k": C.K[name],
                "features": list(C.GROUP_FEATURES[name]), "walk_forward": True}

    # ---------------- 训练 / 推断
    def fit(self, panel: pd.DataFrame) -> dict[str, base.GroupResult]:
        groups = state_assets.load(self.asset).features.build_groups(panel)
        return {GROUP: self._fit_one(groups[GROUP], GROUP)}

    def _fit_one(self, feats: pd.DataFrame, name: str) -> base.GroupResult:
        cols = list(C.GROUP_FEATURES[name])
        feats = feats[cols]
        model = StudentTHMM(self._model_params(name))
        model.fit(feats, labeler=align.order_labeler(C.ORDER_COL[name],
                                                     C.GROUP_LABELS[name]))
        self._models[name] = model
        labels = {int(k): str(v) for k, v in
                  (model.last_window or {}).get("state_labels", {}).items()}
        fit_date = str(pd.Timestamp(model.states_.index[-1]).date())
        return base.GroupResult(
            name=name, title=C.GROUP_TITLE.get(name, name), k=C.K[name],
            states=model.states_.astype(int), labels=labels,
            label_values=model.labels_, proba=model.proba_,
            features=tuple(cols), usable=True,
            fit_id=base.make_fit_id(self.asset, name, C.K[name], cols, labels, fit_date),
            window=model.last_window,
            warmup_bars=int(C.WARMUP_BARS.get(name, 0)))

    def predict(self, panel: pd.DataFrame) -> dict[str, base.GroupResult]:
        groups = state_assets.load(self.asset).features.build_groups(panel)
        out: dict[str, base.GroupResult] = {}
        for name, model in self._models.items():
            cols = list(C.GROUP_FEATURES[name])
            res = model.predict(groups[name][cols])
            labels = {int(k): str(v) for k, v in
                      (model.last_window or {}).get("state_labels", {}).items()}
            out[name] = base.GroupResult(
                name=name, title=C.GROUP_TITLE.get(name, name), k=C.K[name],
                states=res.states, labels=labels, proba=res.proba,
                features=tuple(cols), window=model.last_window)
        return out

    # ---------------- 落盘
    def spec(self) -> dict:
        meta = {}
        for name, model in self._models.items():
            cols = list(C.GROUP_FEATURES[name])
            labels = {int(k): str(v) for k, v in
                      (model.last_window or {}).get("state_labels", {}).items()}
            meta[name] = {
                "title": C.GROUP_TITLE.get(name, name), "k": C.K[name],
                "features": cols, "labels": labels, "usable": True,
                "fit_id": base.make_fit_id(
                    self.asset, name, C.K[name], cols, labels,
                    str(pd.Timestamp(model.states_.index[-1]).date())),
                "label_signature": base.label_fingerprint(model.labels_),
            }
        return {"primary": C.PRIMARY_GROUP, "groups": meta}

    def to_params(self) -> dict:
        return {"groups": {n: {"model": m.to_dict(), "window": m.last_window}
                           for n, m in self._models.items()}}
