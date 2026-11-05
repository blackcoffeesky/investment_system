"""把每天的读数折算成"净指向"。与资产无关，资产通过 config 提供边和列映射。

signal_e(t) = magnitude × β_e(当前标签) × z_e(t) × gate × active
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from causual.causual_core.edge import Edge
from causual.causual_core.estimators import EdgeModel

MONTH_DAYS = 21.0

# 方向纯度的显著性门槛。0.5 ≈ 单边 75% 置信（双侧 p≈0.50）0.70 ≈ 单边 86.5%（p≈0.26）；0.90 ≈ 单边 95%（p≈0.10）。改这一个常数即可。
PURITY_MIN = 0.7

# 交易视界：A 股主观交易以"一周"为操作周期
HORIZON = 5
# tanh 的分母（%）。调小 = α 更容易打满；纯展示尺度，不改变排序
ALPHA_SCALE = 0.5

# 先验权重系数：magnitude 是人工给定的"机制强度"，乘这个数整体放大/缩小。
# 1.0 = 原始先验；调大表示"我认为这些机制比原先给的更重要"。
# 只影响 α 的量级，不影响 β 的估计。
PRIOR_SCALE = 2.0
# 慢变量的日释放衰减（指数，半衰期约 2 天）
TAU = 2.0


def release_profile(max_lag: int, horizon: int = HORIZON, tau: float = TAU) -> np.ndarray:
    """把一条边的总效应分配到未来 H 个交易日。

    max_lag=0（快变量）→ 全部在 Day0 释放；慢变量 → 指数衰减。
    """
    if max_lag <= 0:
        w = np.zeros(horizon)
        w[0] = 1.0
        return w
    w = np.exp(-np.arange(horizon) / tau)
    return w / w.sum()


def dl_aggregate(df: pd.DataFrame, edge: Edge, tcol: str, model: EdgeModel,
                 freq: str | None = None) -> tuple[pd.Series, pd.Series]:
    """分布滞后边拆成两段（**不合并**）：
    - ``agg_contemp`` = β_0 · z_t —— 当期，进 α
    - ``agg_lagged``  = Σ_{k≥1} β_k · z_{t−k} —— 滞后，仅作参考展示
    z 由 `zscore.periodic_z` 给出——和估计层**同一个函数、同一个窗口**。
    """
    from causual.causual_core.utils.zscore import periodic_z

    freq = freq or (model.freq_used or edge.resolved_freq())
    s = df[tcol].resample(freq).last()
    z = periodic_z(s, str(freq))

    b0_full = float(model.path.get(0, float("nan"))) if model.path else float("nan")

    # 当期系数：有分组估计就用当前长期状态那一组，否则回退全样本
    if model.path_by_group and "long_regime" in df.columns:
        lr = df["long_regime"].resample(freq).last().reindex(z.index, method="ffill")
        b0 = lr.map(lambda g: model.path_by_group.get(str(g), {}).get(0, b0_full))
        b0 = b0.astype(float).fillna(b0_full)
    else:
        b0 = pd.Series(b0_full, index=z.index)

    contemp = b0 * z
    lagged = pd.Series(0.0, index=z.index)
    for k, b in model.path.items():
        if int(k) == 0:
            continue
        lagged = lagged.add(b * z.shift(k), fill_value=0.0)

    idx = df.index
    return (contemp.reindex(idx, method="ffill"),
            lagged.reindex(idx, method="ffill").fillna(0.0))


def edge_z_daily(df: pd.DataFrame, edge: Edge, tcol: str,
                 freq: str | None = None) -> pd.Series:
    """信号层与卡片共用的 z：边自己频率上的滚动 z，ffill 回日频。

    `freq` 优先用估计时实际使用的频率（EdgeModel.freq_used）：
    配置改了但快照没重估时，也不会按新配置算 z 去配旧 β。
    """
    from causual.causual_core.utils.zscore import edge_z

    return edge_z(df, tcol, freq=freq or edge.resolved_freq())


def rolling_z(s: pd.Series, window: int = 252) -> pd.Series:
    """日频滚动 z（保留给非边场景使用；边一律走 `edge_z_daily`）。"""
    from causual.causual_core.utils.zscore import periodic_z

    return periodic_z(s, "D", window=window, min_periods=60)


@dataclass
class Contribution:
    edge: Edge
    label: str
    reading: float
    reading_z: float
    reading_pct: float
    beta: float
    contrib: float
    monthly_beta: float
    significant: bool
    sign_ok: bool
    active: bool
    reason: str = ""
    beta_ci: tuple[float, float] = (float("nan"), float("nan"))
    stale_days: int = 0
    last_update: str = ""
    # 分布滞后边：每期的方向纯度（k=0 进 α，k>=1 仅参考）
    purity_by_lag: dict[int, float] = field(default_factory=dict)
    purity: float = 0.0


@dataclass
class SignalBundle:
    """三层语义，别混用：

    - ``param``：纯参数（DML 的 β、DL 的累积 β、事件的 CAAR/窗口）——**不随日期变**
    - ``dose`` ：参数 × 读数（DML 的 β×z、DL 的 Σβ_k z_{t−k}、事件同上）——随日期变
    - ``contrib``：magnitude × dose × gate × active × 纯度 × 释放比例

    ``beta`` 保留为 ``param`` 的别名，仅为兼容旧调用。
    """

    contrib: pd.DataFrame
    beta: pd.DataFrame
    z: pd.DataFrame
    pct: pd.DataFrame
    gate: pd.DataFrame
    predicted: pd.Series
    alpha: pd.Series
    sig: pd.DataFrame = field(default_factory=pd.DataFrame)
    param: pd.DataFrame = field(default_factory=pd.DataFrame)
    dose: pd.DataFrame = field(default_factory=pd.DataFrame)
    column: dict[tuple[str, str], str] = field(default_factory=dict)
    lag: int = 0
    label_signature: str = ""
    # 时间轴：Day0 与未来 H 日累积两个口径
    contrib_h: pd.DataFrame = field(default_factory=pd.DataFrame)
    predicted_h: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    alpha_h: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))


def _ci_significant(model: EdgeModel, label: str) -> bool:
    """该标签下的 95% CI 是否排除 0。没有 CI 时保守地认为"不显著"。"""
    if (model.estimator_used or model.edge.resolve_estimator()) == "distributed_lag":
        ci = model.path_ci.get(0, (float("nan"), float("nan")))
    else:
        ci = model.beta_ci.get(label, (float("nan"), float("nan")))
    return bool(np.isfinite(ci[0]) and np.isfinite(ci[1]) and not (ci[0] <= 0 <= ci[1]))


def _phi(x: float) -> float:
    """标准正态 CDF。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def purity(beta: float, lo: float, hi: float) -> float:
    """方向纯度 = |P(β>0) − P(β<0)|，把 β 的估计分布近似为 N(β, SE²)。

    SE 由 95% CI 反推：SE = (hi − lo) / (2 × 1.96)。
    比"CI 是否含 0"平滑：0.90 ≈ p<0.10，0.95 ≈ p<0.05，1.0 = 完全确定。
    没有 CI 时返回 0——没估计就没有信心。
    """
    if not (np.isfinite(beta) and np.isfinite(lo) and np.isfinite(hi)):
        return 0.0
    se = (hi - lo) / (2 * 1.96)
    if se <= 1e-12:
        return 1.0 if beta != 0 else 0.0
    return float(abs(2.0 * _phi(beta / se) - 1.0))


def _purity_of(model: EdgeModel, label: str) -> float:
    """按边类型取合适的 CI 算纯度。"""
    kind = model.estimator_used or model.edge.resolve_estimator()
    if kind == "event_study":
        t = float(model.t_stat)
        if not np.isfinite(t):
            return 0.0
        return float(abs(2.0 * _phi(t) - 1.0))
    if kind == "distributed_lag":
        # 卡片只审计当期：lag0 的 β 与 CI
        b0 = float(model.path.get(0, float("nan")))
        lo, hi = model.path_ci.get(0, (float("nan"), float("nan")))
        return purity(b0, float(lo), float(hi))

    ci = model.beta_ci.get(label, (float("nan"), float("nan")))
    beta = model.beta_lead.get(label, float("nan"))
    return purity(float(beta), float(ci[0]), float(ci[1]))


def dl_purity_series(model: EdgeModel, df: pd.DataFrame, freq: str) -> pd.Series:
    """DL 边的当期纯度：**按当期所属分组**取 CI，回退全样本。

    分组后的 β 和全样本的 CI 是两回事——用全样本的窄 CI 去给分组的 β 打分，
    会凭空给出 1.00 的纯度。
    """
    b0_full = float(model.path.get(0, float("nan")))
    lo_f, hi_f = model.path_ci.get(0, (float("nan"), float("nan")))
    p_full = purity(b0_full, lo_f, hi_f)
    if not model.path_by_group or "long_regime" not in df.columns:
        return pd.Series(p_full, index=df.index)

    lr = df["long_regime"].resample(freq).last().reindex(df.index, method="ffill")
    per_group = {}
    for g, d in model.path_by_group.items():
        lo, hi = model.path_ci_by_group.get(g, {}).get(0, (float("nan"), float("nan")))
        per_group[g] = purity(float(d[0]), float(lo), float(hi))
    return lr.map(lambda g: per_group.get(str(g), p_full)).astype(float).fillna(p_full)


def build_bundle(
    df: pd.DataFrame,
    models: dict[tuple[str, str], EdgeModel],
    edges: list[Edge],
    *,
    label_col: str,
    treatment_col: dict[str, str],
    target: str,
    signal_lag: int = 0,
) -> SignalBundle:
    cols, beta, zmat, pmat, gate_mat, contrib, contrib_h = {}, {}, {}, {}, {}, {}, {}
    sig_mat: dict[str, pd.Series] = {}
    dose: dict[str, pd.Series] = {}

    for e in edges:
        model = models.get(e.key)
        tcol = treatment_col.get(e.source)
        if model is None or tcol is None or tcol not in df.columns:
            continue

        name = f"{e.source}->{e.target}"
        cols[e.key] = name
        kind = model.estimator_used or e.resolve_estimator()
        # 用**估计时实际使用的频率**，而不是当前配置里的：配置改了但快照没重估时，
        # 按新频率算 z 会和旧 β 对不上。
        mfreq = model.freq_used or e.resolved_freq()

        if kind == "distributed_lag":
            contemp, lagged = dl_aggregate(df, e, tcol, model, mfreq)
            b0 = float(model.path.get(0, float("nan"))) if model.path else float("nan")
            param_s = pd.Series(b0, index=df.index)     # 纯参数：lag0 的 β
            driver = contemp                            # 只有当期进 α
            z = edge_z_daily(df, e, tcol, mfreq)
            gate = pd.Series(1.0, index=df.index)
            span = 0                                    # lag0 不摊释放曲线
            lag_ref = lagged                            # 仅展示，不进 α
        elif kind == "event_study":
            z = edge_z_daily(df, e, tcol, mfreq)
            # 触发方向必须和估计层一致：event_sign>0 只认放量，不能拿 |z| 把缩量也算进来
            if e.event_from_flag:
                gate = (df[tcol].fillna(0) != 0).astype(float)
            elif e.event_sign > 0:
                gate = (z >= e.event_threshold).astype(float)
            elif e.event_sign < 0:
                gate = (z <= -e.event_threshold).astype(float)
            else:
                gate = (z.abs() >= e.event_threshold).astype(float)
            b = pd.Series(model.caar / max(model.window_days, 1), index=df.index)
            param_s = b
            driver = b                                      # 事件的"读数"就是 gate
            span = 0
        else:
            mapping = {str(k): float(v) for k, v in model.beta_lead.items()}
            b = df[label_col].map(mapping).astype(float)
            param_s = b                                     # 纯 β
            z = edge_z_daily(df, e, tcol, mfreq)
            gate = pd.Series(1.0, index=df.index)
            driver = b * z          # **剂量 = β × z**，之前漏了 z
            span = 0

        active = df[label_col].isin(list(e.regime_valid)).astype(float)

        # 方向纯度：贡献 ×= |P(β>0) − P(β<0)|
        if kind == "distributed_lag":
            sig = dl_purity_series(model, df, mfreq)   # 按当期分组取 CI
        elif kind == "event_study":
            sig = _purity_of(model, "all")
        else:
            sig = df[label_col].map(lambda l: _purity_of(model, str(l)))

        # **时间轴**：Day0 的部分进 α（当期归因），未来 H 天合计仅作参考
        prof = release_profile(span)
        base = e.magnitude * PRIOR_SCALE * driver * gate * active * sig
        beta[name] = param_s
        pmat[name] = z.rank(pct=True)
        dose[name] = driver
        zmat[name] = z
        gate_mat[name] = gate
        sig_mat[name] = sig
        contrib[name] = base * prof[0]
        if kind == "distributed_lag":
            # DL 的"未来参考"是滞后段效应，不摊释放曲线，也不进任何 α
            contrib_h[name] = e.magnitude * PRIOR_SCALE * lag_ref * gate * active * sig
        else:
            contrib_h[name] = base * prof.sum()

    beta_df = pd.DataFrame(beta, index=df.index)
    dose_df = pd.DataFrame(dose, index=df.index)
    sig_df = pd.DataFrame(sig_mat, index=df.index)
    z_df = pd.DataFrame(zmat, index=df.index)
    pct_df = pd.DataFrame(pmat, index=df.index)
    gate_df = pd.DataFrame(gate_mat, index=df.index)
    contrib_df = pd.DataFrame(contrib, index=df.index)
    contrib_h_df = pd.DataFrame(contrib_h, index=df.index)

    direct = [
        cols[e.key] for e in edges
        if e.key in cols and e.target == target and e.use_in_alpha
        and e.tier == "signal"        # 只有信号层进 α
    ]
    raw1 = contrib_df[direct].sum(axis=1) if direct else pd.Series(0.0, index=df.index)
    rawh = contrib_h_df[direct].sum(axis=1) if direct else pd.Series(0.0, index=df.index)
    pred1 = raw1.shift(signal_lag) if signal_lag else raw1
    predh = rawh.shift(signal_lag) if signal_lag else rawh

    return SignalBundle(
        contrib=contrib_df, beta=beta_df, z=z_df, pct=pct_df, gate=gate_df, sig=sig_df,
        param=beta_df, dose=dose_df,
        predicted=pred1, alpha=np.tanh(pred1 / ALPHA_SCALE),
        alpha_h=np.tanh(predh / ALPHA_SCALE), predicted_h=predh,
        contrib_h=contrib_h_df, column=cols, lag=signal_lag,
    )


def contributions_at(
    df: pd.DataFrame,
    models: dict[tuple[str, str], EdgeModel],
    bundle: SignalBundle,
    edges: list[Edge],
    when: pd.Timestamp,
    *,
    label_col: str,
    source_col: dict[str, str],
) -> list[Contribution]:
    label = str(df.loc[when, label_col])
    out: list[Contribution] = []

    for e in edges:
        model, name = models.get(e.key), bundle.column.get(e.key)
        if model is None or name is None:
            continue

        z = float(bundle.z.loc[when, name])
        beta = float(bundle.beta.loc[when, name])        # 纯参数
        dose = float(bundle.dose.loc[when, name]) if len(bundle.dose.columns) else beta
        contrib = float(bundle.contrib.loc[when, name])
        gate = float(bundle.gate.loc[when, name])
        valid = list(e.regime_valid)

        if label not in valid:
            reason = f"当前「{label}」不在 regime_valid {valid}，机制停用"
        elif e.estimator == "event_study" and gate <= 0:
            reason = "事件型路径：无活跃事件窗口"
        else:
            reason = ""

        src = source_col.get(e.source)
        last, stale = "", 0
        if src and src in df.columns:
            s = df[src].loc[:when]
            nz = s.dropna()
            if len(nz):
                last = str(nz.index[-1].date())
                stale = int((when - nz.index[-1]).days)

        kind = model.estimator_used or e.resolve_estimator()
        if kind == "distributed_lag":
            mf = model.freq_used or e.resolved_freq()
            g = None
            if model.path_by_group and "long_regime" in df.columns:
                lr = df["long_regime"].resample(mf).last().reindex(df.index, method="ffill")
                g = str(lr.loc[:when].dropna().iloc[-1]) if lr.loc[:when].notna().any() else None
            if g and g in model.path_by_group:
                ci = model.path_ci_by_group[g][0]
                monthly = float(model.path_by_group[g][0]) / MONTH_DAYS
            else:
                ci = model.path_ci.get(0, (float("nan"), float("nan")))
                monthly = (float(model.path.get(0, float("nan"))) / MONTH_DAYS
                           if model.path else float("nan"))
            purity_by_lag = {
                int(k): purity(float(b), *model.path_ci.get(k, (float("nan"), float("nan"))))
                for k, b in model.path.items()
            }
        else:
            ci = model.beta_ci.get(label, (float("nan"), float("nan")))
            monthly = float("nan")
            purity_by_lag = {}
        significant = bool(
            np.isfinite(ci[0]) and np.isfinite(ci[1]) and not (ci[0] <= 0 <= ci[1])
        )

        out.append(Contribution(
            edge=e, label=label,
            reading=float(df.loc[when, src]) if src and src in df.columns else float("nan"),
            reading_z=z,
            reading_pct=float(bundle.pct.loc[when, name]) * 100.0,
            beta=beta, contrib=contrib, monthly_beta=monthly,
            significant=significant,
            sign_ok=(np.sign(beta) == np.sign(e.dir_for(label))) if beta != 0 else True,
            active=bool(label in valid and gate > 0),
            reason=reason, beta_ci=ci, stale_days=stale, last_update=last,
            purity=float(bundle.sig.loc[when, name]) if name in bundle.sig.columns
            else _purity_of(model, label),
            purity_by_lag=purity_by_lag,
        ))
    return out
