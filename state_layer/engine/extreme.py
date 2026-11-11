"""极端行情的规则层：HMM 管常规 regime，规则管极端事件。

为什么不用 HMM 识别极端
----------------------
极端样本占比通常 <1%，HMM 的 BIC 会把它并进相邻状态；即使分出来，
状态均值也会被少量样本拖得不稳。极端行情的定义本身有很强的业务直觉
（暴跌、暴涨、波动爆表），用规则更直接、更可解释。

设计
----
* **进入**：任一条件触发即进入
    - ``|log_return| > sigma × 滚动 60 日 σ``
    - ``realized_vol_20 > 滚动 252 日的 pct 分位``
    - ``|log_return| > abs_cap``（兜底上限，防止分位在样本极小时失效）
* **标签**：按当日收益符号定 EXTREME_UP / EXTREME_DOWN；
  方向不明显但波动爆表 → EXTREME_VOL
* **退出**：「企稳条件」——连续 ``exit_days`` 日不再触发才退出
* **持续期**：几何分布近似，p = 1/均值持续天数

资产可以在自己的 config 里覆盖全部参数。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

EXTREME_UP = "extreme_up"
EXTREME_DOWN = "extreme_down"
EXTREME_VOL = "extreme_vol"
EXTREME_LABELS: tuple[str, ...] = (EXTREME_UP, EXTREME_DOWN, EXTREME_VOL)


@dataclass(frozen=True)
class ExtremeParams:
    enabled: bool = True
    ret_col: str = "log_return"
    vol_col: str = "realized_vol_20"
    sigma: float = 3.0          # |ret| > sigma × 滚动 σ
    sigma_window: int = 252
    pct: float = 0.99           # 波动率分位
    pct_window: int = 252
    abs_cap: float = 0.05       # 兜底：|ret| > 5%
    abs_cap_enabled: bool = False
    exit_days: int = 1          # 连续 N 日不触发才退出
    # 方向判定：|ret| 小于这个比例 × 触发阈值时，认为"方向不明" → EXTREME_VOL
    direction_ratio: float = 0.35

    def with_overrides(self, **kw) -> "ExtremeParams":
        return replace(self, **{k: v for k, v in kw.items() if k in self.__dataclass_fields__})


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
