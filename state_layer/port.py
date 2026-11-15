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

from shared.adapter import get_data

from state_layer import state_assets
from state_layer import store
from state_layer.analysis import observations, stats
from state_layer.model import MODELS, run_infer

# 页面观测用到的表。市场宽度（breadth_*）已从数据层移除、改由
# `datahub.fin.derive.breadth()` 现算，状态层不再依赖它们。
REQUIRED_TABLES = (
    "csi_all",
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
    def _models(self, asset: str) -> list:
        """重建整条模型链。

        **结构以 config.MODEL_CHAIN 为准**，参数优先用落盘的那份；
        落盘里没有的环节（比如旧版本只存了 hmm.pkl）按 config 现场构造，
        这样"旧模型 + 新链结构"也能正确跑，不用先重拟合。

        最后窗口以 **chain.pkl 为唯一真相**：它由 `fit.save` 和 spec 一起落盘，
        每个模型自带 `last_window`（含 `state_labels` / `transmat`）。
        链里没带窗口就**不可用**，不回退到任何遗留文件（2026-11-15 起只认链）。
        """
        pkg = state_assets.load(asset)
        chain_names = list(getattr(pkg.config, "MODEL_CHAIN", ("hmm",)))
        params = dict(getattr(pkg.config, "MODEL_PARAMS", {}))

        try:
            saved = {d.get("kind", "hmm"): d for d in store.load_chain(asset)}
        except FileNotFoundError:
            saved = {}

        out = []
        for name in chain_names:
            data = saved.get(name)
            m = (MODELS[name].from_dict(data) if data
                 else MODELS[name](params.get(name)))
            if m.kind != "extreme" and m.last_window is None:
                # 链里没带最后窗口（旧格式链 / 压根没有 chain.pkl）。
                # 不回退：留 None，让在线后验与外推明确报缺，而不是拿一份过期窗口凑。
                print(f"[state] {asset} 的 {name} 没有最后窗口（last_window），"
                      f"离线后验与外推不可用")
            out.append(m)
        return out

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
                models = self._models(asset)
                if models:
                    res = run_infer(models, features)
                    st = res.get("states")
                    pb = res.get("proba")
                    if st is not None and len(st):
                        label = str(st.iloc[-1])
                        date = st.index[-1]
                    if pb is not None and len(pb):
                        proba = [float(x) for x in pb.iloc[-1].to_numpy()]
                        sid = int(np.argmax(proba))
            except Exception as e:  # noqa: BLE001
                print(f"[state] 现算后验失败：{type(e).__name__}: {e}")

        return _payload(asset, date, label, sid, proba, spec)

    def extend(self, asset: str, features: pd.DataFrame,
               labels: pd.Series | None = None) -> pd.Series:
        """把标签外推到断档期：最后窗口的模型 + 极端规则覆盖。

        最后窗口只来自**模型链**（`chain.pkl`，与 spec 同一次 `fit.save` 落盘）；
        链里没有窗口就不外推，不回退到任何遗留文件。
        同源产物不需要比对 `fit_id`：以前那个遗留窗口文件是**独立老化**的、可能不是
        这次拟合的，才需要核对；它已于 2026-11-15 删除。
        """
        lab = cast(pd.Series, store.load_labels(asset) if labels is None else labels)
        if lab.empty or features is None or features.empty:
            return lab

        filled = cast(pd.Series, lab[lab.astype(str) != ""])
        if filled.empty:
            return lab
        gap = features.index[features.index > filled.index.max()]
        if not len(gap):
            return lab

        models = self._models(asset)
        if not models:
            return lab
        stat = next((m for m in models if m.kind != "extreme"), None)
        lw = stat.last_window if stat is not None else None
        if not lw:
            print(f"[state] {asset} 的模型链里没有最后窗口，标签不外推")
            return lab

        sub = features.loc[features.index <= gap.max()]
        raw = cast(pd.Series, run_infer(models, sub)["states"])
        keep = raw.astype(str) != ""
        add = cast(pd.Series, raw.loc[keep])
        add = cast(pd.Series, add.loc[add.index > filled.index.max()])
        if add.empty:
            print(f"[state] {asset} 外推没产出标签（模型一天都没认领）；"
                  f"检查传入的 features 是否覆盖了训练时的列 {list(lw.get('features') or [])}")
            return lab
        span = pd.DatetimeIndex(add.index)
        print(f"[state] 外推 {len(add)} 天标签"
              f"（{span[0].date()} ~ {span[-1].date()}）")
        return pd.concat([filled, add.astype(str)]).sort_index()

    def transition(self, asset: str, features=None) -> dict | None:
        """状态黏性：留在原状态的概率、平均停留、最可能转向。"""
        models = self._models(asset)
        if not models:
            return None
        stat = next((m for m in models if m.kind != "extreme"), None)
        if stat is None or stat.last_window is None:
            return None
        if features is None or not len(features):
            return stat.transition(None)
        # 只有非极端日才有后验，和 fit 时喂给 HMM 的是同一批日子
        res = run_infer(models, features)
        non_extreme = res["states"].index.intersection(features.index)
        hmm_part = features.loc[non_extreme]
        return stat.transition(hmm_part if len(hmm_part) else None)

    # ---------------- 状态的视图
    def state_view(self, asset: str, series: dict, meta=None) -> dict:
        """需要的状态数据，一次给全。

        消费者不知道状态层的存在，只消费这个 dict：
        `{labels, label_name, labels_order, version, line}`。

        `series` / `meta` 保留只为**兼容既有调用点**（装配点仍会顺手传进来）：
        面板现在直接走耦合层 `get_data`，这两个参数已经不用了。删参数要同步
        service 2 处 + causual cli + 1 条测试，改动面远大于留一个无用参数。
        """
        pkg = state_assets.load(asset)
        panel = get_data(state_assets.panel_of(asset))
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
