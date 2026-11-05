"""评估：符号审计 + Rank IC。与资产无关。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from causual.causual_core.edge import Edge
from causual.causual_core.estimators import EdgeModel


def sign_audit(models: dict[tuple[str, str], EdgeModel]) -> pd.DataFrame:
    """理论符号 vs 估计符号。只审计，不翻转。

    分布滞后边只审计 **lag0**（当期，进 α）；滞后段合计单列一行作参考（无 CI）。
    """
    rows = []
    for (src, tgt), m in models.items():
        kind = m.estimator_used or m.edge.resolve_estimator()
        if kind == "distributed_lag":
            b0 = m.path.get(0)
            if b0 is not None and np.isfinite(b0):
                lo, hi = m.path_ci.get(0, (np.nan, np.nan))
                sig = bool(np.isfinite(lo) and np.isfinite(hi) and not (lo <= 0 <= hi))
                theory = m.edge.dir_for("all")
                rows.append({
                    "edge": f"{src}->{tgt}", "layer": m.edge.layer,
                    "label": "lag0", "理论": theory,
                    "估计": int(np.sign(b0)) if b0 != 0 else 0,
                    "一致性": "OK" if np.sign(b0) == np.sign(theory) else "冲突",
                    "显著": "是" if sig else "否",
                    "估计值": float(b0), "n": m.n_obs,
                })
            lag_sum = sum(v for k, v in m.path.items() if int(k) >= 1)
            if np.isfinite(lag_sum) and any(int(k) >= 1 for k in m.path):
                rows.append({
                    "edge": f"{src}->{tgt}", "layer": m.edge.layer,
                    "label": "lag1..N", "理论": m.edge.direction,
                    "估计": int(np.sign(lag_sum)) if lag_sum != 0 else 0,
                    "一致性": "参考（无 CI）", "显著": "—",
                    "估计值": float(lag_sum), "n": m.n_obs,
                })
            continue

        for label, beta in m.beta_lead.items():
            if beta is None or not np.isfinite(beta):
                continue
            lo, hi = m.beta_ci.get(label, (np.nan, np.nan))
            sig = bool(np.isfinite(lo) and np.isfinite(hi) and not (lo <= 0 <= hi))
            theory = m.edge.dir_for(label)
            rows.append({
                "edge": f"{src}->{tgt}",
                "layer": m.edge.layer,
                "label": label,
                "理论": theory,
                "估计": int(np.sign(beta)) if beta != 0 else 0,
                "一致性": "OK" if np.sign(beta) == np.sign(theory) else "冲突",
                "显著": "是" if sig else "否",
                "估计值": float(beta),
                "n": m.n_obs,
            })
    return pd.DataFrame(rows)


def forward_returns(close: pd.Series, open_: pd.Series | None, horizons=(1, 5, 10)) -> pd.DataFrame:
    out = pd.DataFrame(index=close.index)
    for h in horizons:
        out[f"fwd{h}"] = (close.shift(-h) / close - 1.0) * 100.0
        if open_ is not None:
            out[f"fwd_oc{h}"] = (close.shift(-h) / open_.shift(-1) - 1.0) * 100.0
    return out


def rank_ic(alpha: pd.Series, fwd: pd.Series) -> float:
    d = pd.concat([alpha.rename("a"), fwd.rename("f")], axis=1).dropna()
    if len(d) < 30:
        return float("nan")
    return float(d["a"].corr(d["f"], method="spearman"))


def rank_ic_table(alpha: pd.Series, fwd: pd.DataFrame, n_boot: int = 300, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for col in fwd.columns:
        d = pd.concat([alpha.rename("a"), fwd[col].rename("f")], axis=1).dropna()
        if len(d) < 30:
            continue
        ic = float(d["a"].corr(d["f"], method="spearman"))
        vals = []
        n = len(d)
        for _ in range(n_boot):
            idx = rng.integers(0, n, n)
            s = d.iloc[idx]
            vals.append(float(s["a"].corr(s["f"], method="spearman")))
        lo, hi = np.nanpercentile(vals, [2.5, 97.5])
        rows.append({"horizon": col, "n": n, "rank_ic": ic, "ci_low": lo, "ci_high": hi})
    return pd.DataFrame(rows)
