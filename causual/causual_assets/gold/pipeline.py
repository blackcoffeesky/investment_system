"""黄金插件：把 regime_engine + causual_core 组装起来。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

import regime_engine
from causual.causual_core import snapshot
from causual.causual_core.utils.calendar import merge_shared_nodes
from causual.causual_core.estimators import EdgeModel, fit_all
from causual.causual_core.narrative import build_card, build_console
from causual.causual_core.signal_layer import build_bundle
from causual.causual_assets.gold import config as C
from causual.causual_assets.gold import label as L

def _required_tables() -> tuple[str, ...]:
    """面板用到的表清单——从映射本身推导，避免手写漏表。"""
    from causual.causual_assets.gold import panel as _p

    specs = {**_p.SHARED, **_p.GOLD}.values()
    tables = {x[0] if isinstance(x, tuple) else x for x in specs}
    return tuple(sorted(tables | set(getattr(C, "EXTRA_TABLES", ()))))


REQUIRED_TABLES = _required_tables()

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "causual" / "data"   # 模型产物跟代码一起走，不跟原始数据混
PANEL = DATA / "gold_macro.csv"
REGIME_DIR = DATA / "regime" / C.ASSET

ASSET = C.ASSET
TITLE = C.TITLE
TARGET = C.TARGET
LABEL_COL = "regime_label"
TARGET_COLS = {"close": "CLOSE", "ret": "GOLD_RET", "open": "OPEN"}
LABELS = L.VALUES
LABEL_NAME = {**L.NAME_BY_VALUE, **getattr(L, "EXTREME_NAMES", {})}
EDGES = C.EDGES
TREATMENT_COL = C.TREATMENT_COL
SOURCE_COL = C.SOURCE_COL
SIGNAL_LAG = 1

FEATURES = ["log_return", "realized_vol_5d", "range_pct", "vol_z",
            "usdcny_vol_5d", "vix_level", "mom_20", "rv_ratio", "gap"]


# ------------------------------------------------------------------ 数据
def prepare(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.index = pd.DatetimeIndex(df.index)
    df = df[~df.index.duplicated(keep="last")].sort_index()

    df["GOLD_RET"] = df["CLOSE"].pct_change() * 100.0
    df["XAU_RET"] = df["XAU_USD"].pct_change() * 100.0
    df["USDCNY_RET"] = df["USDCNY"].pct_change() * 100.0
    df["RY_CHG_BP"] = df["REAL_YIELD_10Y"].diff() * 100.0
    df["VIX_CHG"] = df["VIX"].diff()
    df["USDX_CHG"] = df["USD_INDEX"].diff()
    df["GEO_CHG"] = df["GEOPOLITIC_RISK"].diff()
    df["FED_CHG_BP"] = df["FED_RATE"].diff() * 100.0
    oi = df["OPEN_INTEREST"].replace(0, np.nan)
    df["OI_CHG_PCT"] = (oi.pct_change() * 100.0).clip(-50, 50)
    return df


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """状态识别用的 9 维特征（与 A 股完全不同，所以各自实现）。"""
    out = pd.DataFrame(index=df.index)
    ret = df["CLOSE"].pct_change()
    out["log_return"] = np.log1p(ret)
    out["realized_vol_5d"] = ret.rolling(5).std()
    out["range_pct"] = (df["HIGH"] - df["LOW"]) / df["CLOSE"].replace(0, np.nan)
    vol = out["realized_vol_5d"]
    out["vol_z"] = (vol - vol.rolling(60, min_periods=20).mean()) / vol.rolling(60, min_periods=20).std()
    out["usdcny_vol_5d"] = df["USDCNY"].pct_change().rolling(5).std()
    out["vix_level"] = df["VIX"] / 100.0
    out["mom_20"] = df["CLOSE"] / df["CLOSE"].shift(20) - 1.0
    out["rv_ratio"] = ret.rolling(5).std() / ret.rolling(20).std()
    out["gap"] = df["OPEN"] / df["CLOSE"].shift(1) - 1.0
    return out[FEATURES]


def load_raw(series, meta=None) -> pd.DataFrame:
    """面板 = 由调用方传入的原始表拼出来的日频宽表（见 `panel.py`）。"""
    from causual.causual_assets.gold import panel as _panel

    return _panel.panel(series, meta)


# ------------------------------------------------------------------ 状态
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
    s = pd.read_csv(p, index_col=0, parse_dates=True).iloc[:, 0]
    return s.astype(str)


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
    # HMM 参数按资产隔离保存（纯 dict，不存对象）
    result["fit"].save(C.ASSET)
    # 最后一个 walk-forward 窗口的模型：放在**模型库**而不是 spec，
    # 两者生命周期不同；用 fit_id 保证一定对得上。
    from regime_engine.serialize import save_named, to_numpy

    if result.get("last"):
        save_named({**to_numpy(result["last"]), "fit_id": result["spec"]["fit_id"]},
                   C.ASSET, "hmm_last.pkl")
    return p


# ------------------------------------------------------------------ 组装
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
    """把标签外推到最新交易日（上次 fit 之后的断档期）。

    用的是 `saved_models/{asset}/hmm_last.pkl`（上一次拟合的最后一个窗口模型），
    因果上干净：模型固定在过去，只用新数据做预测。
    """
    from causual.causual_core.regime_infer import predict_labels
    from regime_engine import extreme as X
    from regime_engine.serialize import load_named

    feat = build_features(df)
    valid = feat.dropna().index
    if not len(valid) or not labels.notna().any():
        return labels
    last_lab = labels[labels != ""].index.max()
    gap = valid[valid > last_lab]
    if not len(gap):
        return labels

    try:
        lw = load_named(C.ASSET, "hmm_last.pkl")
    except FileNotFoundError:
        print(
            "[extend] 缺少 regime_engine/saved_models/"
            f"{C.ASSET}/hmm_last.pkl，无法外推标签——"
            "当日状态会是空的。先跑 `python -m causual.causual_core.cli regime`。"
        )
        return labels
    if lw.get("fit_id") != spec.get("fit_id"):
        print("[extend] hmm_last.pkl 与 spec 的 fit_id 不一致，不外推")
        return labels

    add = predict_labels(feat, lw, gap)
    ext = X.classify(feat, C.EXTREME_PARAMS).reindex(add.index)
    add.loc[ext != ""] = ext[ext != ""]
    print(f"[extend] 用上次模型外推 {len(add)} 天标签（{add.index[0].date()} ~ {add.index[-1].date()}）")
    return pd.concat([labels[labels != ""], add]).sort_index()


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
        outcome_col={C.TARGET: "CLOSE", "GOLD_RET": "GOLD_RET"},
        target=C.TARGET, label_col=LABEL_COL, return_col="GOLD_RET",
        train=train, seed=seed,
    )
    path = snapshot.save(C.ASSET, models, spec["label_signature"], fit_date=str(df.index[-1].date()))
    if verbose:
        from causual.causual_core.evaluate import sign_audit
        print(sign_audit(models).round(4).to_string(index=False))
        print(f"\n快照 -> {path}")
    return {"models": models, "path": path}


def transition_info(df: pd.DataFrame, spec: dict, cur_label: str | None = None) -> dict | None:
    """今日后验 × 转移矩阵 → 状态黏性 / 切换预警。

    当前状态**锚定到卡片上显示的那个标签**（而不是再推一次 Viterbi 状态）：
    Viterbi 路径与后验 argmax 本来就可能不同，锚定标签能保证"当前状态"和
    "留在哪个状态"两句话说的是同一个东西。
    """
    from causual.causual_core.regime_infer import current, transition_view
    from regime_engine.serialize import load_named

    try:
        lw = load_named(C.ASSET, "hmm_last.pkl")
    except FileNotFoundError:
        return None
    # 漂移保护：模型文件的 fit_id 必须和当前 spec 一致
    if lw.get("fit_id") != spec.get("fit_id"):
        print(f"[warn] hmm_last.pkl 与 spec 的 fit_id 不一致，跳过状态预览")
        return None

    _, p = current(build_features(df), lw)
    if p is None:
        return None

    labels = {int(k): str(v) for k, v in lw["state_labels"].items()}
    cur_sid = next((sid for sid, lab in labels.items() if lab == cur_label), None)
    return transition_view(lw, p, cur_sid)


def make_card(df: pd.DataFrame, spec: dict, models: dict, when: pd.Timestamp,
              data_notes: list[str] | None = None) -> list:
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
        structural=None,          # 黄金不做结构层（制度/政策不适用）
        supply=supply_items(df),
        data_notes=data_notes,
    )


def supply_items(df: pd.DataFrame) -> list[dict]:
    """供需层：位置（历史分位）× 边际（在变好还是变差）。黄金侧看央行与 ETF。"""
    from causual.causual_core.macro_layer import pair_view

    return [
        pair_view(df, group="央行需求", label="央行总持仓", direction="+",
                  freq="QE", pos_col="CB_HOLDINGS", pos_basis="level",
                  mom_col="CB_HOLDINGS", mom_basis="yoy", roll_n=4,
                  note="87 国篮子，季频"),
        pair_view(df, group="央行需求", label="央行净购金（持仓差分）", direction="+",
                  freq="QE", pos_col="CB_GOLD_BUY", pos_basis="roll3",
                  mom_col="CB_GOLD_BUY", mom_basis="level", roll_n=4,
                  note="季度差分，4 季滚动和"),
        pair_view(df, group="投资需求", label="黄金 ETF 净流入", direction="+",
                  freq="ME", pos_col="ETF_FLOW", pos_basis="roll3",
                  mom_col="ETF_FLOW", mom_basis="level", roll_n=3,
                  note="吨/月"),
    ]


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
        supply_detail=supply_items(df),
    )


def console(df: pd.DataFrame, models: dict, bundle) -> str:
    return build_console(
        df, df.index[df[LABEL_COL] != ""][-1], models, bundle, EDGES,
        label_col=LABEL_COL, source_col=SOURCE_COL, target=C.TARGET,
    )
