"""黄金插件：把 状态层 + causual_core 组装起来。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from causual.causual_core import snapshot
from causual.causual_core.utils.calendar import merge_shared_nodes
from causual.causual_core.estimators import EdgeModel, fit_all
from causual.web.render import build_card, build_console
from causual.causual_core.signal_layer import build_bundle
from causual.causual_assets.gold import config as C
from state_layer.state_assets.gold import config as L

def _required_tables() -> tuple[str, ...]:
    """面板用到的表清单——从映射本身推导，避免手写漏表。"""
    from causual.causual_assets.gold import panel as _p

    specs = {**_p.SHARED, **_p.GOLD}.values()
    tables = {x[0] if isinstance(x, tuple) else x for x in specs}
    return tuple(sorted(tables | set(getattr(C, "EXTRA_TABLES", ()))))


def _state_features(df: pd.DataFrame) -> pd.DataFrame:
    """状态特征：P3 起由状态层提供（因果层只消费标签）。"""
    from state_layer.state_assets.gold.features import build

    # 因果层的 df 已经是 prepare 后的面板，状态特征直接吃它
    return build(df)


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
    把特征矩阵递过去。因果层不再 import 状态层的推断部分。
    """
    from state_layer.port import get_port

    feat = _state_features(df)
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


def make_card(df: pd.DataFrame, spec: dict, models: dict, when: pd.Timestamp,
              data_notes: list[str] | None = None,
              state_line: str | None = None) -> list:
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
        structural=None,          # 黄金不做结构层（制度/政策不适用）
        supply=supply_items(df),
        data_notes=data_notes,
        state_line=state_line,
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
    from causual.web.render import build_detail_card

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
