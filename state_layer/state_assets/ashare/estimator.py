"""A 股的状态装配：三个分组（短期价格 / 短期波动 / 中长期价格）。

模型分工（见 `config.GROUP_MODEL`）：
- 价格用 Student-t HMM（厚尾）
- 波动用 Gaussian HMM
- 每组**自己**定 K / 特征 / 标签词表 / 预热天数，都来自 config

**primary 永远是 `short_price`**（causual 只取它）。
加组**不得改变** `label_signature`——签名只哈希 primary 组的标签序列
（`base.label_fingerprint`，见 DESIGN §13）。
"""

from __future__ import annotations

import pandas as pd

from state_layer import state_assets, store
from state_layer.state_assets.labels import order_labeler
from state_layer.model import GaussianHMM, StudentTHMM
from state_layer.state_assets import base
from state_layer.state_assets.ashare import config as C


class Estimator(base.Estimator):
    asset = "ashare"

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self._models: dict[str, object] = {}

    # ---------------- 装配
    @classmethod
    def load(cls, asset: str = "ashare") -> "Estimator":
        inst = cls()
        for name, blob in (store.load_params(asset).get("groups") or {}).items():
            kind = (blob.get("model") or {}).get("kind")
            model_cls = StudentTHMM if kind == "thmm" else GaussianHMM
            inst._models[name] = model_cls.from_dict(blob["model"])
        return inst

    @property
    def warmup_bars(self) -> int:
        return max(int(C.WARMUP_BARS[n]) for n in C.GROUPS_ENABLED)

    def _model_params(self, name: str) -> dict:
        params = {"k": C.K[name], "features": list(C.GROUP_FEATURES[name]),
                  "walk_forward": True, **C.WALK_FORWARD}
        if C.GROUP_MODEL[name] == "thmm":
            params["nu"] = 5.0
        return params

    # ---------------- 训练 / 推断
    def fit(self, panel: pd.DataFrame) -> dict[str, base.GroupResult]:
        frames = state_assets.load(self.asset).features.build_groups(panel)
        out: dict[str, base.GroupResult] = {}
        for name in C.GROUPS_ENABLED:
            out[name] = self._fit_one(frames[name], name)
        return out

    def _fit_one(self, feats: pd.DataFrame, name: str) -> base.GroupResult:
        cols = list(C.GROUP_FEATURES[name])
        feats = feats[cols]
        cls = StudentTHMM if C.GROUP_MODEL[name] == "thmm" else GaussianHMM
        model = cls(self._model_params(name))
        model.fit(feats, labeler=order_labeler(C.ORDER_COL[name],
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
        frames = state_assets.load(self.asset).features.build_groups(panel)
        out: dict[str, base.GroupResult] = {}
        for name, model in self._models.items():
            cols = list(C.GROUP_FEATURES[name])
            res = model.predict(frames[name][cols])
            labels = {int(k): str(v) for k, v in
                      (model.last_window or {}).get("state_labels", {}).items()}
            out[name] = base.GroupResult(
                name=name, title=C.GROUP_TITLE.get(name, name), k=C.K[name],
                states=res.states, labels=labels, proba=res.proba,
                features=tuple(cols), window=model.last_window,
                warmup_bars=int(C.WARMUP_BARS.get(name, 0)))
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
                "model": C.GROUP_MODEL[name], "features": cols,
                "labels": labels, "usable": True,
                "fit_id": base.make_fit_id(
                    self.asset, name, C.K[name], cols, labels,
                    str(pd.Timestamp(model.states_.index[-1]).date())),
                # **只由本组标签决定**；primary 那份就是 causual 读的签名
                "label_signature": base.label_fingerprint(model.labels_),
                "refit_dates": list((model.last_window or {}).get("refit_dates") or []),
            }
        return {"primary": C.PRIMARY_GROUP, "groups": meta}

    def to_params(self) -> dict:
        return {"groups": {n: {"model": m.to_dict(), "window": m.last_window}
                           for n, m in self._models.items()}}
