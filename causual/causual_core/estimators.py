"""因果效应估计核心。**完全与资产无关**。

与旧版的关键差别：状态标签不再从全局 DAG 里取，而是由调用方把当前资产的
标签列表传进来（`labels`），作为异质性特征 X 做独热。这样同一份代码可以
给黄金（6 个标签）和 A 股（另一套 6 个标签）用，互不干扰。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import statsmodels.api as sm
from econml.dml import CausalForestDML, LinearDML
from sklearn.ensemble import RandomForestRegressor

from causual.causual_core.edge import Edge

MONTH_DAYS = 21.0


@dataclass
class EdgeModel:
    edge: Edge
    n_obs: int
    beta_lead: dict[str, float] = field(default_factory=dict)
    beta_ci: dict[str, tuple[float, float]] = field(default_factory=dict)
    p_value: float = float("nan")
    caar: float = float("nan")
    t_stat: float = float("nan")
    n_events: int = 0
    window_days: int = 0
    notes: str = ""
    fitted_until: pd.Timestamp | None = None
    modifier_cols: list[str] = field(default_factory=list)
    use_forest: bool = False
    # 分布滞后：逐期系数（k=0 当期，k>=1 滞后）。DL 边不使用 beta_lead。
    path: dict[int, float] = field(default_factory=dict)
    path_ci: dict[int, tuple[float, float]] = field(default_factory=dict)
    # 按长期状态分组的当期系数（只估 lag0，且不带滞后项与控制变量）。
    # path 仍是全样本单一模型；分组结果另存，避免把两个模型的分量混在一张表里。
    path_by_group: dict[str, dict[int, float]] = field(default_factory=dict)
    path_ci_by_group: dict[str, dict[int, tuple[float, float]]] = field(default_factory=dict)
    n_by_group: dict[str, int] = field(default_factory=dict)
    # 实际使用的估计器（可能是节点标签自动决定的，不一定等于 Edge.estimator）
    estimator_used: str = ""
    # 实际使用的频率（同上：快照要和当时的频率绑定，不能靠当前配置反推）
    freq_used: str = ""
    # 估计时是否启用 split_contemp（lag0 只诊断、不进 α）
    split_contemp: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return self.edge.key


# ------------------------------------------------------------------ 修饰变量
def modifier_frame(df: pd.DataFrame, labels: list[str], label_col: str) -> pd.DataFrame:
    """标签独热（去掉第一个作为基准组）。"""
    if label_col not in df.columns:
        raise ValueError(f"缺少标签列 {label_col}")
    present = [l for l in labels if (df[label_col] == l).any()]
    if not present:
        raise ValueError("标签列全为空")
    mods = pd.DataFrame(index=df.index)
    for lab in present[1:]:
        mods[lab] = (df[label_col] == lab).astype(float)
    return mods


def _label_rows(mods: pd.DataFrame, labels: pd.Series, train: pd.Series,
                label_values: list[str]) -> dict[str, pd.Series]:
    rows, base = {}, {c: 0.0 for c in mods.columns}
    base_label = None
    for lab in label_values:
        if lab not in mods.columns and (labels == lab).any():
            base_label = lab
            break
    for lab in label_values:
        mask = (labels == lab) & train
        if mask.sum() < 30:
            continue
        row = dict(base)
        if lab in mods.columns:
            row[lab] = 1.0
        elif lab != base_label:
            continue
        rows[lab] = pd.Series(row)
    return rows


def effect_at(est, modifier_row: pd.Series, cols: list[str]) -> float:
    x = modifier_row[cols].to_numpy(dtype=float).reshape(1, -1)
    return float(np.ravel(est.effect(X=x))[0])


# ------------------------------------------------------------------ DML
def _rf(seed: int) -> RandomForestRegressor:
    return RandomForestRegressor(n_estimators=150, max_depth=5, n_jobs=1, random_state=seed)


def _learner(n_modifiers: int, seed: int, use_forest: bool):
    from sklearn.model_selection import TimeSeriesSplit

    if use_forest:
        return CausalForestDML(
            model_y=_rf(seed), model_t=_rf(seed),
            n_estimators=100, min_samples_leaf=20, random_state=seed, n_jobs=1,
        )
    # **时间序列交叉验证**：默认的随机 KFold 会让训练集包含测试期之后的样本，
    # 而 y 是 shift(-1) 的未来收益——那就等于用未来预测未来。
    return LinearDML(
        model_y=_rf(seed), model_t=_rf(seed),
        cv=TimeSeriesSplit(n_splits=5), random_state=seed,
    )


def _fit_dml_ols(data, edge, T, Y, X, mod_cols, W, ctrl_cols, base_label=None) -> EdgeModel:
    """小样本回退：OLS + HAC，T 与标签独热做交互。"""
    idx = data.index
    dfx = pd.DataFrame({"t": T}, index=idx)
    for j, c in enumerate(mod_cols):
        dfx[c] = X[:, j]
        dfx[f"t_x_{c}"] = T * X[:, j]
    if W is not None:
        for j, c in enumerate(ctrl_cols):
            dfx[c] = W[:, j]

    D = sm.add_constant(dfx)
    res = sm.OLS(Y, D).fit(cov_type="HAC", cov_kwds={"maxlags": 5})

    out = EdgeModel(edge=edge, n_obs=len(data), fitted_until=idx.max(),
                    modifier_cols=list(mod_cols), use_forest=False)
    base_t = float(res.params["t"])
    for j, c in enumerate(mod_cols):
        beta_lab = base_t + float(res.params[f"t_x_{c}"])
        out.beta_lead[c] = beta_lab
        var = res.cov_params()
        names = ["t", f"t_x_{c}"]
        se = float(np.sqrt(var.loc[names, names].to_numpy().sum()))
        out.beta_ci[c] = (beta_lab - 1.96 * se, beta_lab + 1.96 * se)

    # 基准组（未进独热的那个标签）：只有 t
    out.beta_lead[base_label or "base"] = base_t
    se_b = float(res.bse["t"])
    out.beta_ci[base_label or "base"] = (base_t - 1.96 * se_b, base_t + 1.96 * se_b)
    out.notes = f"OLS+HAC 回退（样本 {len(data)} < 300，DML 不适用）"
    return out


def fit_dml_edge(
    df: pd.DataFrame,
    edge: Edge,
    *,
    labels: list[str],
    tcol: str,
    ycol: str,
    label_col: str = "regime_label",
    horizon: str = "lead",
    seed: int = 42,
    use_forest: bool = False,
) -> EdgeModel:
    """线性 DML：T=读数，Y=次日收益，X=标签独热，W=混杂。"""
    mods = modifier_frame(df, labels, label_col)
    y = df[ycol].shift(-1) if horizon == "lead" else df[ycol]

    cols = [y.rename("y"), df[tcol].rename("t_raw")]
    frame = pd.concat(cols, axis=1)
    if edge.confounders:
        for c in edge.confounders:
            # 全 NaN 的控制变量（数据源挂掉时常见）直接跳过，否则 DML 会因 NaN 报错
            if c in df.columns and df[c].notna().any():
                frame[c] = df[c]
    frame = pd.concat([frame, mods], axis=1)

    data = frame.dropna(subset=["y", "t_raw"])
    # 控制变量里的零散 NaN 也要去掉，否则 econml 会报 Input contains NaN
    ctrl_present = [c for c in edge.confounders if c in frame.columns]
    if ctrl_present:
        data = data.dropna(subset=ctrl_present)
    if len(data) < 200:
        return EdgeModel(edge=edge, n_obs=len(data), notes="样本不足，跳过")

    # 处理变量标准化，β 才有"每 1σ"的含义。
    # 必须用**滚动 z**（和信号层同一个函数），否则 β 的尺度和乘上去的 z 不一致，
    # 而且全样本均值/标准差在实盘不可得。
    from shared.data_processing import edge_z

    z_all = edge_z(df, tcol, freq="D")
    data = data.assign(t_z=z_all.reindex(data.index))
    data = data[np.isfinite(data["t_z"].to_numpy(dtype=float))]
    if len(data) < 200:
        return EdgeModel(edge=edge, n_obs=len(data), notes="滚动 z 预热后样本不足")
    T = data["t_z"].to_numpy(dtype=float)
    Y = data["y"].to_numpy(dtype=float)
    X = mods.loc[data.index].to_numpy(dtype=float)
    ctrl_cols = [c for c in edge.confounders if c in data.columns]
    W = data[ctrl_cols].to_numpy(dtype=float) if ctrl_cols else None

    # 小样本（月度 ~119 个）做 DML 没意义：交叉验证后每折训练集不足百条。
    # 直接退化为「T + 标签独热 + T×独热 + 控制变量」的 OLS（HAC 标准误）。
    if len(data) < 300:
        # 基准组 = "在数据里出现过、但没进独热"的那个标签。
        # 只按 "not in mods.columns" 筛会取到从未出现的标签，交互项就解释错了。
        present = set(pd.unique(df.loc[data.index, label_col]))
        base_label = next(
            (l for l in labels if l in present and l not in mods.columns), None
        )
        return _fit_dml_ols(data, edge, T, Y, X, mods.columns, W, ctrl_cols, base_label)

    est = _learner(X.shape[1], seed, use_forest)
    try:
        est.fit(Y=Y, T=T, X=X, W=W)
    except TypeError:
        est.fit(Y=Y, T=T, X=X) if W is None else est.fit(Y=Y, T=T, X=X, W=W)

    out = EdgeModel(
        edge=edge, n_obs=int(len(data)), fitted_until=data.index.max(),
        modifier_cols=list(mods.columns), use_forest=use_forest,
    )
    rows = _label_rows(mods.loc[data.index], df.loc[data.index, label_col],
                       pd.Series(True, index=data.index), labels)
    for lab, row in rows.items():
        val = effect_at(est, row, out.modifier_cols)
        out.beta_lead[lab] = float(val)
        try:
            lo, hi = est.effect_interval(X=row.to_numpy(dtype=float).reshape(1, -1), alpha=0.05)
            out.beta_ci[lab] = (float(np.ravel(lo)[0]), float(np.ravel(hi)[0]))
        except Exception:  # noqa: BLE001
            pass
    out.notes = f"DML({('forest' if use_forest else 'linear')}), n={len(data)}"
    return out


# ------------------------------------------------------------------ 分布滞后
def fit_distributed_lag(
    df: pd.DataFrame,
    edge: Edge,
    *,
    tcol: str,
    target_col: str,
    is_price_target: bool = True,
    label_value: str = "all",
) -> EdgeModel:
    """慢变量的累积效应：y 为该频率收益，x 为水平/流量 z，取 1..max_lag 阶之和。"""
    freq = edge.resolved_freq()
    unit = "周" if str(freq).startswith("W") else ("月" if str(freq).startswith("M") else "季")

    if is_price_target:
        panel = df[[tcol, target_col]].resample(freq).last()
        panel["y"] = panel[target_col].pct_change() * 100.0
    else:
        panel = df[[tcol, target_col]].resample(freq).last()
        panel["y"] = panel[target_col]

    x = panel[tcol]
    if edge.detrend:
        x = x - x.rolling(8, min_periods=4).mean()

    # 与信号层同一个函数、同一个窗口：滚动 z（无前视）
    from shared.data_processing import periodic_z

    panel["x"] = periodic_z(x, str(freq))
    if not panel["x"].notna().any():
        return EdgeModel(edge=edge, n_obs=0, notes="滚动 z 全为 NaN")

    lag_cols = []
    for k in range(1, edge.max_lag + 1):
        c = f"x_lag{k}"
        panel[c] = panel["x"].shift(k)
        lag_cols.append(c)

    ctrl_cols = []
    for c in edge.confounders:
        if c not in df.columns:
            continue
        col = f"w_{c}"
        series = df[c].resample(freq).last()
        sd_c = float(series.std())
        if not np.isfinite(sd_c) or sd_c < 1e-12:
            continue
        panel[col] = (series - float(series.mean())) / sd_c
        ctrl_cols.append(col)

    data = panel[["y", "x"] + lag_cols + ctrl_cols].dropna()
    if len(data) < edge.max_lag + 16:
        return EdgeModel(edge=edge, n_obs=len(data), notes=f"{unit}度样本不足，跳过")

    X = sm.add_constant(data[["x"] + lag_cols + ctrl_cols])
    res = sm.OLS(data["y"], X).fit(cov_type="HAC", cov_kwds={"maxlags": edge.max_lag})

    model = EdgeModel(
        edge=edge, n_obs=len(data),
        p_value=float(res.pvalues["x"]),          # lag0 的 p（卡片只审计当期）
        window_days=int(round(MONTH_DAYS)),
        fitted_until=data.index.max(),
        notes=(
            f"分布滞后逐期系数（%/{unit}，每 1σ）；"
            f"lag0 当期，lag1-{edge.max_lag} 供参考"
        ),
    )

    # 逐期系数：lag0..max_lag 全部保存（lag0 进 α，其余仅展示）
    ci_all = res.conf_int()
    for k in range(0, edge.max_lag + 1):
        col = "x" if k == 0 else f"x_lag{k}"
        lo_k, hi_k = ci_all.loc[col].to_numpy()
        model.path[k] = float(res.params[col])
        model.path_ci[k] = (float(lo_k), float(hi_k))

    _fit_grouped_lag0(model, edge, df, data, freq)
    return model


MIN_GROUP_N = 18


def _fit_grouped_lag0(model: EdgeModel, edge: Edge, df: pd.DataFrame,
                      data: pd.DataFrame, freq: str) -> None:
    """按长期状态分组，**只估当期系数**。

    为什么不把分组变量塞进主回归：月频每组只有 ~40 个样本，
    `x + 6 个滞后 + 控制变量` 已经是 10 个参数，再交互就纯噪音。
    所以分组这一步只估 `y ~ const + x`（不带滞后、不带控制变量）。

    这也是为什么 `path` 保持全样本、分组结果单独放 `path_by_group`：
    两者是**不同的模型**，不能拼成一条 path。
    """
    if edge.resolved_group_by() != "long":
        return
    col = "long_regime"
    if col not in df.columns:
        return

    lr = df[col].resample(freq).last()
    lr = lr.reindex(data.index, method="ffill")

    for g in pd.unique(lr.dropna()):
        if not str(g):
            continue
        sub = data.loc[lr == g, ["y", "x"]].dropna()
        if len(sub) < MIN_GROUP_N:
            continue
        Xg = sm.add_constant(sub[["x"]])
        rg = sm.OLS(sub["y"], Xg).fit(cov_type="HAC", cov_kwds={"maxlags": edge.max_lag})
        lo, hi = rg.conf_int().loc["x"]
        model.path_by_group[str(g)] = {0: float(rg.params["x"])}
        model.path_ci_by_group[str(g)] = {0: (float(lo), float(hi))}
        model.n_by_group[str(g)] = int(len(sub))


# ------------------------------------------------------------------ 事件研究
def fit_event_study(
    df: pd.DataFrame,
    edge: Edge,
    *,
    tcol: str,
    ycol: str,
    label_value: str = "all",
) -> EdgeModel:
    """事件窗口内的累计异常收益（CAAR）。"""
    x = df[tcol]
    if edge.event_from_flag:
        events = x.fillna(0).astype(float) != 0
        n_events_total = int(events.sum())
    else:
        # 事件定义必须和信号层的 gate 用同一个 z（滚动、无前视）
        from shared.data_processing import edge_z

        z = edge_z(df, tcol, freq="D")
        if edge.event_sign > 0:
            events = z >= edge.event_threshold
        elif edge.event_sign < 0:
            events = z <= -edge.event_threshold
        else:
            events = z.abs() >= edge.event_threshold
        events = events.fillna(False)
        n_events_total = int(events.sum())

    lo, hi = edge.event_window
    rets = df[ycol]
    caars = []
    for dt in df.index[events.fillna(False)]:
        pos = df.index.get_loc(dt)
        a, b = pos + lo, pos + hi + 1
        if a < 0 or b > len(df):
            continue
        window = rets.iloc[a:b].dropna()
        if len(window):
            caars.append(float(window.sum()))

    if len(caars) < 10:
        return EdgeModel(edge=edge, n_obs=0, n_events=len(caars), notes="事件太少，跳过")

    arr = np.asarray(caars)
    caar = float(arr.mean())
    se = float(arr.std(ddof=1) / np.sqrt(len(arr)))
    return EdgeModel(
        edge=edge, n_obs=int(events.sum()), caar=caar,
        t_stat=caar / se if se > 0 else float("nan"),
        n_events=len(arr), window_days=int(hi - lo + 1),
        beta_lead={label_value: caar / max(hi - lo + 1, 1)},
        fitted_until=df.index[-1],
        notes=f"CAAR={caar:+.2f}%（{len(arr)} 个事件）",
    )


# ------------------------------------------------------------------ 批量
def fit_all(
    df: pd.DataFrame,
    edges: list[Edge],
    *,
    labels: list[str],
    treatment_col: dict[str, str],
    outcome_col: dict[str, str],
    target: str,
    label_col: str = "regime_label",
    return_col: str = "RET",
    train: pd.Series | None = None,
    seed: int = 42,
    use_forest: bool = False,
    local_nodes=None,
) -> dict[tuple[str, str], EdgeModel]:
    """拟合全部边。`train` 给定时只用其覆盖的区间（滚动重估用）。"""
    sub = df if train is None else df.loc[train.reindex(df.index).fillna(False).to_numpy()]
    models: dict[tuple[str, str], EdgeModel] = {}

    for e in edges:
        if e.source not in treatment_col:
            continue
        tcol = treatment_col[e.source]
        if tcol not in df.columns:
            continue

        kind = e.resolve_estimator(local_nodes)
        if kind == "distributed_lag":
            models[e.key] = fit_distributed_lag(
                df, e, tcol=tcol, target_col=outcome_col.get(e.target, target),
                is_price_target=(e.target == target),
            )
        elif kind == "event_study":
            models[e.key] = fit_event_study(df, e, tcol=tcol, ycol=return_col)
        else:
            models[e.key] = fit_dml_edge(
                sub, e, labels=labels, tcol=tcol,
                ycol=return_col, label_col=label_col,
                seed=seed, use_forest=use_forest,
            )
        if e.key in models:
            models[e.key].estimator_used = kind
            models[e.key].freq_used = e.resolved_freq(local_nodes)
            models[e.key].split_contemp = bool(e.split_contemp)
    return models
