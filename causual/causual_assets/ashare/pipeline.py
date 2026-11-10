"""A 股插件：中证全指 + 量价状态 + 股票供给。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

import regime_engine
from causual.causual_core import snapshot
from causual.causual_core.estimators import EdgeModel, fit_all
from causual.causual_core.narrative import build_card, build_console
from causual.causual_core.signal_layer import build_bundle
from causual.causual_assets.ashare import config as C
from causual.causual_assets.ashare import label as L

def _required_tables() -> tuple[str, ...]:
    """面板用到的表清单——从映射本身推导，避免手写漏表。"""
    from causual.causual_assets.ashare import panel as _p

    specs = {**_p.SHARED, **_p.ASHARE}.values()
    tables = {x[0] if isinstance(x, tuple) else x for x in specs}
    return tuple(sorted(tables | set(getattr(C, "EXTRA_TABLES", ()))))


REQUIRED_TABLES = _required_tables()

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "causual" / "data"   # 模型产物跟代码一起走，不跟原始数据混
PANEL = DATA / "ashare_macro.csv"
REGIME_DIR = DATA / "regime" / C.ASSET

ASSET = C.ASSET
TITLE = C.TITLE
TARGET = C.TARGET
LABEL_COL = "regime_label"
LONG_REGIME_COL = "long_regime"
TARGET_COLS = {"close": "CLOSE", "ret": "RET", "open": "OPEN"}
LABELS = L.VALUES
LABEL_NAME = {**L.NAME_BY_VALUE, **getattr(L, "EXTREME_NAMES", {})}
EDGES = C.EDGES
TREATMENT_COL = C.TREATMENT_COL
SOURCE_COL = C.SOURCE_COL
SIGNAL_LAG = 0

# 8 维：limit_ratio 因涨停池无历史而移除（PROBLEM P20）
FEATURES = ["log_return", "realized_vol_20", "volume_z", "up_day_ratio",
            "size_mom", "margin_chg_z", "rv_ratio", "shibor_chg_z",
            "etf_dispersion"]


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.index = pd.DatetimeIndex(df.index)
    df = df[~df.index.duplicated(keep="last")].sort_index()

    df["RET"] = df["CLOSE"].pct_change() * 100.0
    df["RET_OC"] = (df["CLOSE"] / df["OPEN"] - 1.0) * 100.0
    df["RY_CHG_BP"] = df["REAL_YIELD_10Y"].diff() * 100.0
    df["USDX_CHG"] = df["USD_INDEX"].diff()
    df["VIX_CHG"] = df["VIX"].diff()
    df["USDCNY_RET"] = df["USDCNY"].pct_change() * 100.0
    df["FED_CHG_BP"] = df["FED_RATE"].diff() * 100.0
    df["SHIBOR_CHG_BP"] = df["SHIBOR3M"].diff() * 100.0
    df["PMI_CHG"] = df["PMI"].diff()
    df["SF_YOY_CHG"] = df["SOCIAL_FINANCING"].pct_change(12) * 100.0
    df["MARGIN_CHG"] = df["MARGIN_BALANCE"].pct_change() * 100.0
    # 两融净流入（亿元/日）：余额差分。信号层用"净流入/流出方向"比余额水平更有信息
    df["MARGIN_NET"] = df["MARGIN_BALANCE"].diff()

    # 股票供给：月度净融资额 → z（去趋势后再标准化）
    sup = df["EQUITY_SUPPLY"].resample("ME").last()
    sup_d = sup - sup.rolling(8, min_periods=4).mean()
    sd = float(sup_d.std())
    df["SUPPLY_Z"] = (sup_d / sd).reindex(df.index, method="ffill") if sd > 1e-12 else np.nan

    # 放量冲击：成交量 60 日 z（成交额不可得，见 PROBLEM P21）
    vol = df["VOLUME"]
    vmu = vol.rolling(60, min_periods=20).mean()
    vsd = vol.rolling(60, min_periods=20).std()
    df["VOLUME_Z"] = (vol - vmu) / vsd.replace(0, np.nan)
    df["AMOUNT_SHOCK"] = df["VOLUME_Z"]

    # 股东回报：全市场分红 / 归母净利润（年度）。年报次年才影响市场，
    # 所以用 (年份 - 1) 的值，避免把当年年报提前到年内使用。
    df["PAYOUT_RATIO"] = _payout_series(df.index)

    # 供需派生：**先在月频上算，再 ffill 回日频**。
    # 直接在日频上 diff 会得到 0（日频是 ffill 的），ΔM1 会恒等于 0。
    m1 = df["M1_LEVEL"].resample("ME").last()
    mk = df["TOTAL_MKTCAP"].resample("ME").last()
    fin = df["EQUITY_FIN_MONTHLY"].resample("ME").last()
    ins = df["INSIDER_NET"].resample("ME").last()

    net = fin - ins                       # 融资是新增筹码，大股东增持减少筹码
    dm1 = m1.diff()

    def _back(s: pd.Series, name: str) -> None:
        df[name] = s.reindex(df.index, method="ffill")

    _back(net, "NET_SUPPLY")
    _back(m1 / mk, "M1_TO_MKTCAP")
    _back(dm1, "DM1")
    _back(net.rolling(3, min_periods=1).sum(), "NET_SUPPLY_3M")
    _back(dm1.rolling(3, min_periods=1).sum(), "DM1_3M")

    # 增量比：分子分母可能异号，三个数都单独展示，比值仅作参考
    df["SUPPLY_RATIO"] = df["DM1_3M"] / df["NET_SUPPLY_3M"].replace(0, np.nan)
    # 资金活化：M1 同比 − M2 同比（正值 = 活期化改善）
    df["M1M2_GAP"] = df["M1_YOY"] - df["M2_YOY"]
    return df


def _payout_series(idx: pd.DatetimeIndex) -> pd.Series:
    """年度分红率：直接读 datahub 的 `dividend_annual`。

    用上一年度的值（今年年报还没出），避免前视。
    """
    try:
        from datahub.fin import read

        d = read("dividend_annual")
        if not isinstance(d, pd.DataFrame) or not len(d):
            return pd.Series(np.nan, index=idx)
        m = {int(y): float(v) for y, v in zip(d.index.year, d["payout_ratio"])}
    except Exception:  # noqa: BLE001
        return pd.Series(np.nan, index=idx)
    return pd.Series([m.get(int(y) - 1, np.nan) for y in idx.year], index=idx)


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    ret = df["CLOSE"].pct_change()
    out["log_return"] = np.log1p(ret)
    out["realized_vol_20"] = ret.rolling(20).std()
    out["volume_z"] = df["VOLUME_Z"]
    out["up_day_ratio"] = (ret > 0).astype(float).rolling(20).mean()
    out["size_mom"] = (df["CSI1000"] / df["CSI300"]).pct_change(20) if "CSI1000" in df else np.nan
    out["margin_chg_z"] = df["MARGIN_CHG"].rolling(60, min_periods=20).apply(
        lambda x: (x.iloc[-1] - x.mean()) / x.std() if x.std() > 0 else np.nan, raw=False)
    out["rv_ratio"] = ret.rolling(5).std() / ret.rolling(20).std()
    out["shibor_chg_z"] = df["SHIBOR_CHG_BP"].rolling(60, min_periods=20).apply(
        lambda x: (x.iloc[-1] - x.mean()) / x.std() if x.std() > 0 else np.nan, raw=False)
    out["etf_dispersion"] = df.get("ETF_DISPERSION", pd.Series(np.nan, index=df.index))
    return out[FEATURES]


def load_raw(series, meta=None) -> pd.DataFrame:
    """面板 = 由调用方传入的原始表拼出来的日频宽表（见 `panel.py`）。"""
    from causual.causual_assets.ashare import panel as _panel

    return _panel.panel(series, meta)


def _spec_path() -> Path:
    return REGIME_DIR / "current.json"


def load_spec() -> dict:
    p = _spec_path()
    if not p.exists():
        raise FileNotFoundError(f"没有 {C.ASSET} 的状态模型：{p}")
    return json.loads(p.read_text(encoding="utf-8"))


def load_labels() -> pd.Series:
    p = REGIME_DIR / "labels.csv"
    if not p.exists():
        raise FileNotFoundError(f"没有 {C.ASSET} 的逐日标签：{p}")
    return pd.read_csv(p, index_col=0, parse_dates=True).iloc[:, 0].astype(str)


def fit_regime(df: pd.DataFrame, k: int | None = None, seed: int = 42, verbose: bool = True) -> dict:
    """极端规则 + 常规 HMM 两层装配（共用 causual_core/regime_pipeline.py）。"""
    from causual.causual_core.regime_fit import fit_asset

    return fit_asset(
        df, build_features(df),
        labeler_factory=L.name_states,
        name_by_value={**L.NAME_BY_VALUE, **getattr(L, "EXTREME_NAMES", {})},
        signature_fn=L.signature,
        asset=C.ASSET,
        extreme_params=C.EXTREME_PARAMS,
        k=k, seed=seed, verbose=verbose,
    )


def save_regime(result: dict) -> Path:
    REGIME_DIR.mkdir(parents=True, exist_ok=True)
    p = _spec_path()
    p.write_text(json.dumps(result["spec"], ensure_ascii=False, indent=2), encoding="utf-8")
    result["labels"].rename("label").to_frame().to_csv(REGIME_DIR / "labels.csv", encoding="utf-8-sig")
    result["fit"].save(C.ASSET)
    from regime_engine.serialize import save_named, to_numpy

    if result.get("last"):
        save_named({**to_numpy(result["last"]), "fit_id": result["spec"]["fit_id"]},
                   C.ASSET, "hmm_last.pkl")
    return p


def build_panel(asof: str | None = None, series=None, meta=None) -> tuple[pd.DataFrame, dict]:
    df = prepare(load_raw(series, meta))
    spec = load_spec()
    labels = load_labels().reindex(df.index).fillna("").astype(str)
    labels = _extend_labels(df, spec, labels)
    df[LABEL_COL] = labels.reindex(df.index).fillna("").astype(str)
    if asof:
        df = df.loc[: pd.Timestamp(asof)]
    return df, spec


def _extend_labels(df: pd.DataFrame, spec: dict, labels: pd.Series) -> pd.Series:
    """把标签外推到最新交易日——**委托状态层做**。

    P2 起外推逻辑归 `state_layer`（它管模型和窗口），这里只负责
    把特征矩阵递过去。因果层不再 import regime_engine 的推断部分。
    """
    from state_layer.port import get_port

    feat = build_features(df)
    try:
        return get_port().extend(C.ASSET, feat, labels)
    except Exception as e:  # noqa: BLE001
        print(f"[extend] 状态层外推失败（{type(e).__name__}: {e}），本次不外推")
        return labels


def fit_edges(df: pd.DataFrame, spec: dict, seed: int = 42, verbose: bool = True) -> dict:
    ok = df[LABEL_COL] != ""
    n = int(ok.sum())
    if n < 300:
        raise RuntimeError(f"有效标签样本只有 {n}")

    cut = df.index[ok][int(n * 0.95) - 1]
    train = pd.Series((df.index <= cut) & ok.to_numpy(), index=df.index)
    if verbose:
        print(f"拟合边模型：{int(train.sum())} 个有效样本，截止 {cut.date()}")

    models: dict[tuple[str, str], EdgeModel] = fit_all(
        df, EDGES, labels=list(LABELS), treatment_col=TREATMENT_COL,
        outcome_col={C.TARGET: "CLOSE"}, target=C.TARGET,
        label_col=LABEL_COL, return_col="RET", train=train, seed=seed,
        local_nodes=C.LOCAL_NODES,
    )
    path = snapshot.save(C.ASSET, models, spec["label_signature"], fit_date=str(df.index[-1].date()))

    # 对照实验：USDCNY 若**不控制** USD_INDEX，会不会把美元指数的效果算到汇率头上
    try:
        from dataclasses import replace
        from causual.causual_core.estimators import fit_dml_edge

        e = next(x for x in EDGES if x.source == "USDCNY")
        m_nc = fit_dml_edge(
            df, replace(e, confounders=()), labels=list(L.VALUES),
            tcol=TREATMENT_COL["USDCNY"], ycol="RET", label_col=LABEL_COL, seed=seed,
        )
        with_c = models.get(e.key)
        print("\n[对照] USDCNY→CSI_ALL")
        print(f"  控制 USD_INDEX: β(panic)={with_c.beta_lead.get('panic', float('nan')):+.4f}"
              if with_c else "")
        print(f"  不控制 USD_INDEX: β(panic)={m_nc.beta_lead.get('panic', float('nan')):+.4f}")
        _write_contrast(with_c, m_nc)
    except Exception as exc:  # noqa: BLE001
        print(f"[对照] 失败：{type(exc).__name__}: {exc}")

    try:
        validate_insider(df, seed=seed)
    except Exception as exc:  # noqa: BLE001
        print(f"[分段] 失败：{type(exc).__name__}: {exc}")
    if verbose:
        from causual.causual_core.evaluate import sign_audit
        tbl = sign_audit(models)
        print(tbl.round(4).to_string(index=False) if len(tbl) else "（无可用估计）")
        for key, m in models.items():
            if m.path:
                print(f"\n{key[0]} 的滞后路径：")
                for k in sorted(m.path):
                    lo, hi = m.path_ci.get(k, (float('nan'), float('nan')))
                    print(f"  lag{k}: {m.path[k]:+.2f}%  [{lo:+.2f}, {hi:+.2f}]")
        print(f"\n快照 -> {path}")
    return {"models": models, "path": path}


def _write_contrast(with_c, without_c) -> None:
    lines = ["# USDCNY→CSI_ALL：控制 vs 不控制 USD_INDEX", "",
             "| 标签 | 控制 USD_INDEX | 不控制 |", "|---|---|---|"]
    labels = sorted(set((with_c.beta_lead if with_c else {}) ) | set(without_c.beta_lead))
    for lab in labels:
        a = with_c.beta_lead.get(lab, float("nan")) if with_c else float("nan")
        b = without_c.beta_lead.get(lab, float("nan"))
        lines.append(f"| {lab} | {a:+.4f} | {b:+.4f} |")
    out = ROOT / "output" / C.ASSET / "usdcny_contrast.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_insider(df: pd.DataFrame, seed: int = 42, n_seg: int = 4) -> pd.DataFrame:
    """内部人增持的**分段稳健性**：把样本四等分，各段单独重估。

    只在一段成立 = 拟合噪声；四段同号才敢给高纯度。
    """
    from causual.causual_core.estimators import fit_distributed_lag

    e = next(x for x in EDGES if x.source == "INSIDER_NET")
    ok = df[LABEL_COL] != ""
    sub = df.loc[ok]
    if len(sub) < 200:
        return pd.DataFrame()

    edges_idx = sub.index
    bounds = np.linspace(0, len(edges_idx), n_seg + 1).astype(int)
    rows = []
    for i in range(n_seg):
        seg = sub.iloc[bounds[i]:bounds[i + 1]]
        m = fit_distributed_lag(
            seg, e, tcol=TREATMENT_COL["INSIDER_NET"], target_col="CLOSE",
            is_price_target=True,
        )
        rows.append({
            "segment": f"{seg.index[0].date()} ~ {seg.index[-1].date()}",
            "n": m.n_obs,
            "cum": m.cumulative,
            "p": m.p_value,
            "lag0": m.path.get(0, float("nan")),
        })

    out = pd.DataFrame(rows)
    path = ROOT / "output" / C.ASSET / "insider_segments.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# 内部人增持：分段稳健性", "",
             "| 区间 | n | lag1-3 累积 | p | lag0 | 同号 |", "|---|---|---|---|---|---|"]
    sign0 = np.sign(out["cum"].iloc[0])
    for _, r in out.iterrows():
        same = "是" if np.sign(r["cum"]) == sign0 else "**否**"
        lines.append(f"| {r['segment']} | {int(r['n'])} | {r['cum']:+.2f}% | "
                     f"{r['p']:.3f} | {r['lag0']:+.2f}% | {same} |")
    lines.append("")
    lines.append("> 四段同号且都显著才算稳健；否则该边的纯度应打折扣。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n[分段] 内部人增持：")
    print(out.round(3).to_string(index=False))
    print(f"  -> {path}")
    return out


def transition_info(df: pd.DataFrame, spec: dict, cur_label: str | None = None) -> dict | None:
    """今日后验 × 转移矩阵 → 状态黏性 / 切换预警（当前状态锚定卡片标签）。"""
    from causual.causual_core.regime_infer import current, transition_view
    from regime_engine.serialize import load_named

    try:
        lw = load_named(C.ASSET, "hmm_last.pkl")
    except FileNotFoundError:
        return None
    if lw.get("fit_id") != spec.get("fit_id"):
        print("[warn] hmm_last.pkl 与 spec 的 fit_id 不一致，跳过状态预览")
        return None

    _, p = current(build_features(df), lw)
    if p is None:
        return None

    labels = {int(k): str(v) for k, v in lw["state_labels"].items()}
    cur_sid = next((sid for sid, lab in labels.items() if lab == cur_label), None)
    return transition_view(lw, p, cur_sid)


def supply_items(df: pd.DataFrame) -> list[dict]:
    """供需层：**位置（历史分位）× 边际（在变好还是变差）**。

    两者只给分位，不给原始值——不同量纲的绝对值放一张表里没有可比性。
    `direction` 决定读法：同一个"高位+改善"，对正向指标是"强势仍在增强"，
    对负向指标是"压力持续加大"。
    """
    from causual.causual_core.macro_layer import pair_view

    return [
        # ---- 3.1 供需总量
        pair_view(df, group="供需总量", label="M1 / 总市值", direction="+",
                  pos_col="M1_TO_MKTCAP", pos_basis="level",
                  mom_col="M1_TO_MKTCAP", mom_basis="yoy",
                  note="总市值代替流通市值"),
        # ---- 3.2 供需增量
        pair_view(df, group="供需增量", label="ΔM1", direction="+",
                  pos_col="M1_LEVEL", pos_basis="roll3",
                  mom_col="M1_LEVEL", mom_basis="yoy",
                  note="位置用 M1 三月滚动和，边际用同比"),
        pair_view(df, group="供需增量", label="净供给（融资−增持）", direction="-",
                  pos_col="NET_SUPPLY", pos_basis="roll3",
                  mom_col="NET_SUPPLY", mom_basis="level",
                  note="融资 +，增持 −"),
        # ---- 3.3 宏观环境
        pair_view(df, group="宏观环境", label="社零", direction="+",
                  pos_col="RETAIL_SALES_MOM", pos_basis="level",
                  mom_col="RETAIL_SALES_MOM", mom_basis="dma3",
                  note="当月同比"),
        pair_view(df, group="宏观环境", label="工业增加值", direction="+",
                  pos_col="INDUSTRIAL_YOY", pos_basis="level",
                  mom_col="INDUSTRIAL_YOY", mom_basis="dma3",
                  note="数据停在 2025-08"),
        pair_view(df, group="宏观环境", label="PMI", direction="+",
                  pos_col="PMI", pos_basis="level",
                  mom_col="PMI", mom_basis="mom",
                  note="扩散指数，50 为中枢"),
        pair_view(df, group="宏观环境", label="M1−M2", direction="+",
                  pos_col="M1M2_GAP", pos_basis="level",
                  mom_col="M1M2_GAP", mom_basis="yoy",
                  note="正=资金活化"),
        pair_view(df, group="宏观环境", label="CPI", direction="0",
                  pos_col="CPI_YOY", pos_basis="level",
                  mom_col="CPI_YOY", mom_basis="dma3",
                  note="方向不定：低通胀偏正、高通胀偏负"),
        pair_view(df, group="宏观环境", label="PPI", direction="+",
                  pos_col="PPI_YOY", pos_basis="level",
                  mom_col="PPI_YOY", mom_basis="dma3"),
        pair_view(df, group="宏观环境", label="出口", direction="+",
                  pos_col="EXPORTS_YOY", pos_basis="level",
                  mom_col="EXPORTS_YOY", mom_basis="dma3",
                  note="数据停在 2025-08"),
        # ---- 3.4 增长转化
        pair_view(df, group="增长转化", label="分红 / 净利润", direction="+",
                  pos_col="PAYOUT_RATIO", pos_basis="level",
                  mom_col="PAYOUT_RATIO", mom_basis="yoy",
                  note="年度，滞后 90 天"),
        pair_view(df, group="增长转化", label="大股东净增持", direction="+",
                  pos_col="INSIDER_NET", pos_basis="roll3",
                  mom_col="INSIDER_NET", mom_basis="level"),
    ]


def supply_detail_items(df: pd.DataFrame) -> list[dict]:
    """详情卡追加：位置/边际的原始值与窗口。"""
    from causual.causual_core.macro_layer import pair_view

    return supply_items(df) + [
        pair_view(df, group="原始", label="总股本", direction="-",
                  pos_col="TOTAL_SHARES", pos_basis="level",
                  mom_col="TOTAL_SHARES", mom_basis="yoy", note="仅参考"),
        pair_view(df, group="原始", label="总市值", direction="+",
                  pos_col="TOTAL_MKTCAP", pos_basis="level",
                  mom_col="TOTAL_MKTCAP", mom_basis="yoy", note="含估值，仅参考"),
        pair_view(df, group="原始", label="股票融资", direction="-",
                  pos_col="EQUITY_FIN_MONTHLY", pos_basis="roll3",
                  mom_col="EQUITY_FIN_MONTHLY", mom_basis="level"),
        pair_view(df, group="原始", label="M1 同比", direction="+",
                  pos_col="M1_YOY", pos_basis="level", mom_col="M1_YOY", mom_basis="dma3"),
        pair_view(df, group="原始", label="M2 同比", direction="+",
                  pos_col="M2_YOY", pos_basis="level", mom_col="M2_YOY", mom_basis="dma3"),
        pair_view(df, group="原始", label="社零累计同比", direction="+",
                  pos_col="RETAIL_SALES", pos_basis="level",
                  mom_col="RETAIL_SALES", mom_basis="dma3", note="旧口径，带季节性"),
    ]


def make_card(df: pd.DataFrame, spec: dict, models: dict, when: pd.Timestamp,
              data_notes: list[str] | None = None) -> list:
    from causual.causual_core.macro_layer import structural_state, describe_series  # noqa: F401

    bundle = build_bundle(
        df, models, EDGES, label_col=LABEL_COL,
        treatment_col=TREATMENT_COL, target=C.TARGET, signal_lag=SIGNAL_LAG,
    )
    bundle.label_signature = spec["label_signature"]
    profile = pd.DataFrame(spec["profile"]).set_index("label")
    return build_card(
        C.ASSET, C.TITLE, df, when,
        label_col=LABEL_COL, label_name=LABEL_NAME, label_order=list(L.VALUES),
        profile=profile, edges=EDGES, models=models, bundle=bundle,
        treatment_col=TREATMENT_COL, source_col=SOURCE_COL, target=C.TARGET,
        target_cols=TARGET_COLS,
        separation={"min_dist": spec.get("min_bhattacharyya", 0.0),
                    "passed": spec.get("min_bhattacharyya", 0.0) >= 0.4},
        transition=transition_info(df.loc[:when], spec, str(df.loc[when, LABEL_COL])),
        structural=structural_state(asof=when),
        supply=supply_items(df),
        data_notes=data_notes,
    )


def make_detail_card(df: pd.DataFrame, spec: dict, models: dict, when: pd.Timestamp) -> list:
    from causual.causual_core.narrative import build_detail_card

    bundle = build_bundle(
        df, models, EDGES, label_col=LABEL_COL,
        treatment_col=TREATMENT_COL, target=C.TARGET, signal_lag=SIGNAL_LAG,
    )
    bundle.label_signature = spec["label_signature"]
    profile = pd.DataFrame(spec["profile"]).set_index("label")
    return build_detail_card(
        C.ASSET, C.TITLE, df, when,
        label_col=LABEL_COL, label_name=LABEL_NAME, profile=profile,
        spec=spec, edges=EDGES, models=models, target=C.TARGET,
        source_col=SOURCE_COL, treatment_col=TREATMENT_COL, bundle=bundle,
        supply_detail=supply_detail_items(df),
    )


def console(df: pd.DataFrame, models: dict, bundle) -> str:
    return build_console(
        df, df.index[df[LABEL_COL] != ""][-1], models, bundle, EDGES,
        label_col=LABEL_COL, source_col=SOURCE_COL, target=C.TARGET,
    )
