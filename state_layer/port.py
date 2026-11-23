"""state_layer 对外的能力（其他模块只能通过这里访问状态层）。

这一层做三件事，都不涉及模型内部：
- 读：标签、spec、单点状态
- 算：标签外推（用 estimator 现算）、状态黏性
- 装配：spec → 对外契约（date / label / model / metrics / as_of_date）

**多分组**：A 股有 3 组、黄金 1 组。对外默认只给 `primary`（`config.PRIMARY_GROUP`），
分组全貌留给 `/state` 页面（步 4）。`state_view` 的返回形状保持不变，
这样 service / causual 不用跟着改。
"""

from __future__ import annotations

from typing import cast

import numpy as np
import pandas as pd

from state_layer import state_assets, profile as core_profile, store

# 页面观测用到的表。
REQUIRED_TABLES = (
    "csi_all",
    "csi1000", "csi300",
)


def _is_v2(meta: dict) -> bool:
    return meta.get("schema") == store.SCHEMA_V2


def _primary_meta(meta: dict) -> tuple[str, dict]:
    """(primary 组名, 该组元信息)。legacy spec 没有分组，合成一个。"""
    if _is_v2(meta):
        name = str(meta.get("primary"))
        return name, dict((meta.get("groups") or {}).get(name) or {})
    d = meta.get("data") or {}
    m = meta.get("model") or {}
    return "raw", {"k": m.get("k"), "features": list(d.get("features") or []),
                   "labels": {}, "usable": True, "fit_id": d.get("fit_id"),
                   "label_signature": d.get("label_signature")}


def _payload(asset: str, date, label: str, sid: int | None,
             proba: list[float] | None, meta: dict) -> dict:
    c = state_assets.load(asset).config
    name, g = _primary_meta(meta)
    k = g.get("k")
    kind = g.get("kind") or ("thmm" if asset == "ashare" else "hmm")
    version = (f"estimator-v2:{asset}@{name}-{kind}-k{k}-{g.get('fit_id')}"
               if _is_v2(meta) else (meta.get("model") or {}).get("version"))
    return {
        "date": str(pd.Timestamp(date).date()),
        "asset": asset,
        "group": name,
        "state_id": sid,
        "label": label,
        "label_name": c.LABEL_NAME.get(label, label),
        "state_probabilities": proba or [],
        "confidence": float(max(proba)) if proba else None,
        "model": {
            "kind": kind,
            "k": k,
            "version": version,
            "features": list(g.get("features") or []),
            "fit_date": g.get("fit_date"),
            "signature": g.get("label_signature"),
        },
        "metrics": meta.get("metrics", {}),
        "as_of_date": str(pd.Timestamp(date).date()),
    }


class StatePort:
    # ---------------- 装配数据
    def required_tables(self, asset: str = "ashare") -> tuple[str, ...]:
        pkg = state_assets.load(asset)
        return tuple(sorted(set(REQUIRED_TABLES) | set(pkg.REQUIRED_TABLES)))

    # ---------------- 读
    def labels(self, asset: str = "ashare", group: str | None = None) -> pd.Series:
        """状态标签（date → label）。默认给 `primary` 组；legacy 产物回退读旧文件。"""
        return store.load_group_labels(asset, group)

    def spec(self, asset: str = "ashare") -> dict:
        return store.load_meta(asset)

    def states(self, asset: str = "ashare", start: str | None = None,
               end: str | None = None) -> pd.DataFrame:
        s = cast(pd.Series, self.labels(asset))
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
    def _estimate(self, asset: str):
        """重建 estimator（v2 产物）。没有 v2 就抛——不拿旧链凑。"""
        return state_assets.load(asset).estimator.load(asset)

    def _primary_group(self, asset: str, panel: pd.DataFrame):
        meta = self.spec(asset)
        name, _ = _primary_meta(meta)
        groups = self._estimate(asset).predict(panel)
        if name not in groups:
            raise KeyError(f"{asset} 的 estimator 没产出 primary 组 {name!r}；"
                           f"实际有 {sorted(groups)}")
        return groups[name], meta

    def current_state(self, asset: str = "ashare", panel: pd.DataFrame | None = None) -> dict:
        meta = self.spec(asset)
        labels = self.labels(asset)
        if labels.empty and panel is None:
            return {}
        label, date = str(labels.iloc[-1]), labels.index[-1]
        sid, proba = None, None

        if panel is not None and len(panel):
            try:
                g, _ = self._primary_group(asset, panel)
                if len(g.states):
                    sid = int(g.states.iloc[-1])
                    date = g.states.index[-1]
                    label = str(g.labels.get(sid, sid))
                    if g.proba is not None and len(g.proba):
                        proba = [float(x) for x in g.proba.iloc[-1].to_numpy()]
            except Exception as e:  # noqa: BLE001
                print(f"[state] 现算后验失败：{type(e).__name__}: {e}")

        return _payload(asset, date, label, sid, proba, meta)

    def extend(self, asset: str, panel: pd.DataFrame,
               labels: pd.Series | None = None) -> pd.Series:
        """把标签外推到断档期（用 estimator 现算，只用当日及之前的行）。"""
        lab = cast(pd.Series, self.labels(asset) if labels is None else labels)
        if lab.empty or panel is None or panel.empty:
            return lab
        filled = cast(pd.Series, lab[lab.astype(str) != ""])
        if filled.empty:
            return lab
        gap = panel.index[panel.index > filled.index.max()]
        if not len(gap):
            return lab
        # 缺 v2 产物**不降级成空标签**：带空标签的 Series 会让下游以为有标签。
        # 让 FileNotFoundError 上抛，由调用方（state_view）决定怎么退化。
        g, _ = self._primary_group(asset, panel)
        add = g.label_series()
        add = cast(pd.Series, add.loc[add.index > filled.index.max()])
        if add.empty:
            print(f"[state] {asset} 外推没产出标签；检查面板是否覆盖训练特征")
            return lab
        span = pd.DatetimeIndex(add.index)
        print(f"[state] 外推 {len(add)} 天标签（{span[0].date()} ~ {span[-1].date()}）")
        return pd.concat([filled, add.astype(str)]).sort_index()

    def transition(self, asset: str, panel: pd.DataFrame | None = None) -> dict | None:
        """状态黏性：从**历史标签**估经验转移（不读模型内部）。

        与旧实现（读模型 transmat）的差别：这里量的是"实际发生过什么"，
        跨重训不会失真。`p_next` 需要 panel（现算当天后验）。
        """
        s = self.labels(asset)
        if s.empty or len(s) < 30:
            return None
        vals = s.astype(str).to_numpy()
        names = sorted(set(vals))
        idx = {n: i for i, n in enumerate(names)}
        P = np.zeros((len(names), len(names)))
        for a, b in zip(vals[:-1], vals[1:]):
            P[idx[a], idx[b]] += 1.0
        row = P.sum(axis=1, keepdims=True)
        P = np.divide(P, np.where(row == 0, 1, row))
        run = (s != s.shift()).cumsum()
        dur = s.groupby([s, run]).size().groupby(level=0).mean()
        cur = str(s.iloc[-1])
        durations: dict[str, float | None] = {}
        for n in names:
            v = dur.get(n)
            durations[n] = None if v is None else float(v)
        out: dict = {"durations": durations, "transmat": P.tolist()}
        nxt = sorted(((n, float(P[idx[cur], idx[n]])) for n in names if n != cur),
                     key=lambda kv: -kv[1])[:2]
        out.update({"label": cur, "stay_prob": float(P[idx[cur], idx[cur]]),
                    "duration": durations.get(cur), "p_next": nxt})
        if panel is not None and len(panel):
            try:
                g, _ = self._primary_group(asset, panel)
                cur_state = int(g.states.iloc[-1])
                p_today = (g.proba.iloc[-1].to_numpy()
                           if g.proba is not None and len(g.proba) else None)
                if p_today is not None:
                    out["posterior"] = {str(g.labels.get(i, i)): float(p)
                                        for i, p in enumerate(p_today)}
                    out["confident"] = bool(np.max(p_today) >= 0.6)
                    out["label"] = str(g.labels.get(cur_state, cur))
            except Exception:  # noqa: BLE001
                pass
        return out

    # ---------------- 状态的视图
    def state_view(self, asset: str, panel: pd.DataFrame) -> dict:
        """需要的状态数据，一次给全（**形状不变**：primary 组）。

        `{labels, label_name, labels_order, version, spec, line}`。
        """
        pkg = state_assets.load(asset)
        try:
            labels = self.extend(asset, panel)
        except FileNotFoundError as e:
            print(f"[state] {asset} 没有 v2 产物，标签不外推：{e}")
            labels = self.labels(asset)
        c = pkg.config
        cur = self.current_state(asset, panel)
        tr = self.transition(asset, panel) or {}

        bits = [str(cur.get("label_name") or cur.get("label") or "—")]
        conf = cur.get("confidence")
        if conf:
            bits.append(f"后验 {float(conf):.0%}")
        if tr.get("duration"):
            bits.append(f"平均停留 {float(tr['duration']):.0f} 天")

        spec = self.spec(asset)
        return {
            "labels": labels,
            "label_name": dict(c.LABEL_NAME),
            "labels_order": tuple(c.VALUES),
            "version": (cur.get("model") or {}).get("version"),
            "spec": spec,
            "line": " · ".join(bits) + "（详见 /state）",
        }

    # ---------------- 展示（面板由装配点传进来，这里不取数）
    def profile(self, asset: str, panel: pd.DataFrame) -> pd.DataFrame:
        return core_profile.profile(asset, core_profile.observations(panel))

_INSTANCE: StatePort | None = None


def get_port() -> StatePort:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = StatePort()
    return _INSTANCE
