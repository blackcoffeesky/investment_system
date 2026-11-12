"""极端行情的判定规则：什么时候算极端、叫什么名字。

这里**只有规则**，不做任何模型拟合——它和 HMM 无关，换个统计模型照样能用。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from state_layer.model.extreme.params import (EXTREME_DOWN, EXTREME_LABELS,
                                              EXTREME_UP, EXTREME_VOL,
                                              ExtremeParams)


def _raw_flags(feat: pd.DataFrame, p: ExtremeParams) -> tuple[pd.Series, pd.Series, pd.Series]:
    ret = feat[p.ret_col]
    vol = feat[p.vol_col] if p.vol_col in feat.columns else None

    roll_sd = ret.rolling(p.sigma_window, min_periods=20).std()
    hit_ret = ret.abs() > (p.sigma * roll_sd)

    hit_vol = pd.Series(False, index=feat.index)
    if vol is not None:
        q = vol.rolling(p.pct_window, min_periods=60).quantile(p.pct)
        hit_vol = vol > q

    hit_abs = pd.Series(False, index=feat.index)
    if p.abs_cap_enabled:
        hit_abs = ret.abs() > p.abs_cap

    return hit_ret.fillna(False), hit_vol.fillna(False), hit_abs.fillna(False)


def classify(feat: pd.DataFrame, p: ExtremeParams | None = None) -> pd.Series:
    """返回极端标签 Series（非极端为 ""）。"""
    p = p or ExtremeParams()
    if not p.enabled or p.ret_col not in feat.columns:
        return pd.Series("", index=feat.index, dtype=object)

    hit_ret, hit_vol, hit_abs = _raw_flags(feat, p)
    hit = (hit_ret | hit_vol | hit_abs).to_numpy()
    hit_vol_np = hit_vol.to_numpy()

    # 进入后至少停留 exit_days 天（企稳条件）
    n = len(hit)
    state = np.zeros(n, dtype=bool)
    cooldown = 0
    for i in range(n):
        if hit[i]:
            state[i] = True
            cooldown = p.exit_days
        elif cooldown > 0:
            state[i] = True
            cooldown -= 1

    ret = feat[p.ret_col].to_numpy(dtype=float)
    # 当日触发强度（用于判方向）：用触发时的 |ret| 与触发阈值比
    out = np.array([""] * n, dtype=object)
    for i in range(n):
        if not state[i]:
            continue
        r = ret[i] if np.isfinite(ret[i]) else 0.0
        if hit_vol_np[i] and abs(r) < p.abs_cap * p.direction_ratio:
            out[i] = EXTREME_VOL
        else:
            out[i] = EXTREME_UP if r > 0 else EXTREME_DOWN

    return pd.Series(out, index=feat.index, dtype=object)


def apply(labels: pd.Series, ext: pd.Series) -> pd.Series:
    """用极端标签覆盖统计标签（极端日走规则）。

    两个序列的索引范围常不一样（统计标签只覆盖常规日），
    这里统一按 `labels` 的索引对齐，调用方不用管。
    """
    out = labels.copy()
    aligned = ext.reindex(out.index).fillna("").astype(str)
    mask = aligned != ""
    out.loc[mask] = aligned[mask]
    return out


def to_dict(p: "ExtremeParams | None") -> dict:
    """参数 → 纯 dict（可 JSON）。"""
    if p is None:
        return {}
    return {k: getattr(p, k) for k in p.__dataclass_fields__}


def from_dict(d: dict) -> "ExtremeParams | None":
    if not d:
        return None
    return ExtremeParams(**{k: v for k, v in d.items()
                            if k in ExtremeParams.__dataclass_fields__})


def duration_stats(labels: pd.Series) -> dict[str, float]:
    """各极端标签的平均持续天数（几何分布近似：p = 1/mean）。"""
    s = labels[labels != ""]
    if s.empty:
        return {}
    run = (s != s.shift()).cumsum()
    seg = pd.DataFrame({"label": s.to_numpy(), "run": run.to_numpy()})
    seg = seg.groupby(["label", "run"]).size().rename("n").reset_index()
    out = {}
    for lab, grp in seg.groupby("label"):
        m = float(grp["n"].mean())
        out[str(lab)] = m
    return out
