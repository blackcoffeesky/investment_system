"""state_layer 对外的能力（其他模块只能通过这里访问状态层）。

这一层做三件事，都不涉及模型内部：
- 读：状态标签、spec、单点状态
- 算：标签外推（用最后窗口的模型）、状态黏性
- 装配：spec → 对外契约（date / label / model / metrics / as_of_date）

模型参数怎么存、怎么重建，都由 `store` 与 `model.hmm` 负责。
"""

from __future__ import annotations

from typing import cast

import numpy as np
import pandas as pd

from state_layer import state_assets
from state_layer import store
from state_layer.analysis import observations, stats
from state_layer.model import MODELS
from state_layer.model.extreme import rules as extreme_rules
from state_layer.model.hmm import GaussianHMM

REQUIRED_TABLES = (
    "csi_all", "csi_all_amount", "csi_all_turnover",
    "breadth_adv", "breadth_dec", "breadth_limitup",
    "csi1000", "csi300",
)


def _payload(asset: str, date, label: str, sid: int | None,
             proba: list[float] | None, spec: dict) -> dict:
    c = state_assets.load(asset).config
    m = spec.get("model", {})
    d = spec.get("data", {})
    return {
        "date": str(pd.Timestamp(date).date()),
        "asset": asset,
        "state_id": sid,
        "label": label,
        "label_name": c.LABEL_NAME.get(label, label),
        "state_probabilities": proba or [],
        "confidence": float(max(proba)) if proba else None,
        "model": {
            "kind": m.get("kind"),
            "k": m.get("k"),
            "version": m.get("version"),
            "features": list(d.get("features") or c.FEATURES),
            "fit_date": d.get("fit_date"),
            "signature": d.get("label_signature"),
        },
        "metrics": spec.get("metrics", {}),
        "as_of_date": str(pd.Timestamp(date).date()),
    }


def _extreme_params(asset: str):
    """链里 extreme 那一环的参数，转成 ExtremeParams。"""
    pkg = state_assets.load(asset)
    return extreme_rules.from_dict(pkg.config.MODEL_PARAMS.get("extreme") or {})


class StatePort:
    # ---------------- 装配数据
    def required_tables(self, asset: str = "ashare") -> tuple[str, ...]:
        """状态层要的数据 = 页面观测用的 + 该资产特征用的。"""
        pkg = state_assets.load(asset)
        return tuple(sorted(set(REQUIRED_TABLES) | set(pkg.REQUIRED_TABLES)))

    # ---------------- 读
    def labels(self, asset: str = "ashare") -> pd.Series:
        """状态标签（date → label）。因果层要的就是这个。"""
        return store.load_labels(asset)

    def spec(self, asset: str = "ashare") -> dict:
        return store.load_spec(asset)

    def states(self, asset: str = "ashare", start: str | None = None,
               end: str | None = None) -> pd.DataFrame:
        s = cast(pd.Series, store.load_labels(asset))
        if s.empty:
            return pd.DataFrame(columns=["date", "label", "label_name"])
        if start:
            s = cast(pd.Series, s[s.index >= pd.Timestamp(start)])
        if end:
            s = cast(pd.Series, s[s.index <= pd.Timestamp(end)])
        c = state_assets.load(asset).config
        return pd.DataFrame({
            "date": s.index,
            "label": s.values,
            "label_name": [c.LABEL_NAME.get(v, v) for v in s.values],
        })

    # ---------------- 现算
    def _model(self, asset: str):
        """链里的统计模型（extreme 不参与概率计算）。"""
        try:
            chain = store.load_chain(asset)
        except FileNotFoundError:
            return None
        for data in chain:
            if data.get("kind", "hmm") != "extreme":
                return MODELS[data.get("kind", "hmm")].from_dict(data)
        return None

    def current_state(self, asset: str = "ashare", features=None) -> dict:
        spec = store.load_spec(asset)
        labels = store.load_labels(asset)
        if labels.empty:
            return {}
        label = str(labels.iloc[-1])
        date = labels.index[-1]
        sid, proba = None, None

        if features is not None and len(features):
            try:
                model = self._model(asset)
                if model is not None:
                    lw = store.load_model(asset, "hmm_last.pkl")
                    model.last_window = lw
                    res = model.infer(features)
                    st = res.get("states")
                    pb = res.get("proba")
                    if st is not None and len(st):
                        label = str(st.iloc[-1])
                    if pb is not None and len(pb):
                        proba = [float(x) for x in pb.iloc[-1].to_numpy()]
                        sid = int(np.argmax(proba))
            except Exception as e:  # noqa: BLE001
                print(f"[state] 现算后验失败：{type(e).__name__}: {e}")

        return _payload(asset, date, label, sid, proba, spec)

    def extend(self, asset: str, features: pd.DataFrame,
               labels: pd.Series | None = None) -> pd.Series:
        """把标签外推到断档期：最后窗口的模型 + 极端规则覆盖。"""
        lab = cast(pd.Series, store.load_labels(asset) if labels is None else labels)
        if lab.empty or features is None or features.empty:
            return lab

        c = state_assets.load(asset).config
        spec = store.load_spec(asset)
        filled = cast(pd.Series, lab[lab.astype(str) != ""])
        if filled.empty:
            return lab
        valid = features.dropna().index
        gap = valid[valid > filled.index.max()]
        if not len(gap):
            return lab

        try:
            lw = store.load_model(asset, "hmm_last.pkl")
        except FileNotFoundError:
            print(f"[state] 缺 hmm_last.pkl，{asset} 的标签不外推")
            return lab
        if lw.get("fit_id") != spec.get("data", {}).get("fit_id"):
            print(f"[state] hmm_last.pkl 与 spec 的 fit_id 不一致，不外推")
            return lab

        model = self._model(asset)
        if model is None:
            return lab
        model.last_window = lw
        add = model.infer(features.loc[gap])["states"]
        add = cast(pd.Series, add[add != ""])
        if add.empty:
            return lab

        ext = extreme_rules.classify(features, _extreme_params(asset)).reindex(add.index)
        add = extreme_rules.apply(add, ext)
        span = pd.DatetimeIndex(add.index)
        print(f"[state] 外推 {len(add)} 天标签"
              f"（{span[0].date()} ~ {span[-1].date()}）")
        return pd.concat([filled, add]).sort_index()

    def transition(self, asset: str, features=None) -> dict | None:
        """状态黏性：留在原状态的概率、平均停留、最可能转向。"""
        model = self._model(asset)
        if model is None:
            return None
        try:
            model.last_window = store.load_model(asset, "hmm_last.pkl")
        except FileNotFoundError:
            return None
        return model.transition(features)

    # ---------------- 给因果层的视图
    def causual_view(self, asset: str, series: dict, meta=None) -> dict:
        """因果层需要的状态数据，一次给全。

        因果层不知道状态层的存在，只消费这个 dict：
        `{labels, label_name, labels_order, version, line}`。
        """
        pkg = state_assets.load(asset)
        panel = pkg.panel(series, meta)
        feats = pkg.features.build(panel)

        labels = self.extend(asset, feats)
        c = pkg.config
        cur = self.current_state(asset, feats)
        tr = self.transition(asset, feats) or {}

        bits = [str(cur.get("label_name") or cur.get("label") or "—")]
        conf = cur.get("confidence")
        if conf:
            bits.append(f"后验 {float(conf):.0%}")
        if tr.get("duration"):
            bits.append(f"平均停留 {float(tr['duration']):.0f} 天")

        spec = store.load_spec(asset)

        return {
            "labels": labels,
            "label_name": dict(c.LABEL_NAME),
            "labels_order": tuple(c.VALUES),
            "version": spec.get("model", {}).get("version"),
            "spec": spec,
            "line": " · ".join(bits) + "（详见 /state）",
        }

    # ---------------- 展示
    def panel(self, series: dict) -> pd.DataFrame:
        return observations.panel(series)

    def availability(self, series: dict) -> list[dict]:
        return observations.availability(series)

    def profile(self, asset: str, series: dict) -> pd.DataFrame:
        return stats.profile(asset, observations.panel(series))

    def timeline(self, asset: str, series: dict, days: int = 250) -> list[dict]:
        return stats.timeline(asset, observations.panel(series), days)


_INSTANCE: StatePort | None = None


def get_port() -> StatePort:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = StatePort()
    return _INSTANCE
