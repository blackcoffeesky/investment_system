"""黄金插件：把 状态层 + causual_core 组装起来。

面板：基础列来自耦合层 `shared.adapter.get_data`，流量列（央行购金 / ETF 持仓变化）
来自 `shared.utils.features`（纯函数）。**本文件是唯一装配点**（`load_raw`）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from shared.adapter import Panel, get_data, tables_of
from shared.utils import features
from shared.utils.calendar import merge_shared_nodes
from causual.causual_core import snapshot
from causual.causual_core.estimators import EdgeModel, fit_all
from causual.web.render import build_card, build_console
from causual.causual_core.signal_layer import build_bundle
from causual.causual_assets.gold import config as C

# 面板底层读哪些表 = 耦合层的声明（**不抄一份**）
REQUIRED_TABLES = tables_of(Panel.CAUSUAL_GOLD)

# 面板列 = 耦合层的**语义列名**（2026-11-15 起；`CLOSE`/`VIX` 那套大写内部名退役）。
PANEL_COLS: tuple[str, ...] = (
    # 价格 / 量
    "open", "high", "low", "close", "volume",
    # 黄金供需（`cb_holdings` / `gold_etf_holdings` 按 raw 策略给，`_derived` 再差分）
    "xau_usd", "cb_holdings", "gold_etf_holdings",
    "gold_open_interest", "gold_cot_net_long", "gold_cpi_surprise",
    # 共享宏观
    "vix", "usdcny", "usd_index", "fed_rate", "real_yield_10y", "geopolitic_risk",
)

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "causual" / "data"   # 状态模型产物由状态层写，这里只读
PANEL = DATA / "gold_macro.csv"

ASSET = C.ASSET
TITLE = C.TITLE
TARGET = C.TARGET
LABEL_COL = "regime_label"
LONG_REGIME_COL = "long_regime"
TARGET_COLS = {"close": "close", "ret": "GOLD_RET", "open": "open"}
# 状态标签词表由调用方注入（set_state_vocab），因果层不 import 状态层
LABELS: tuple[str, ...] = ()
LABEL_NAME: dict[str, str] = {}
EDGES = C.EDGES
TREATMENT_COL = C.TREATMENT_COL
SOURCE_COL = C.SOURCE_COL
# 已知没数据的列（{列名: 原因}）；黄金当前为空，但**必须显式声明**，
# 否则这个资产里的空列没人管（口径见 ashare/pipeline.py 同一行）。
NOT_CONNECTED = C.NOT_CONNECTED
SIGNAL_LAG = 1


def _warn(msg: str) -> None:
    print(f"[面板] {msg}", file=sys.stderr)


def _derived(df: pd.DataFrame, base: pd.DataFrame) -> dict[str, pd.Series]:
    """黄金的两条流量列——**唯一的装配点**。

    存量（`cb_holdings` / `gold_etf_holdings`，耦合层按 `raw` 策略给：只在
    **发布可见日**有值）→ 流量：相邻观测差分。算法在
    `shared.utils.features`（`cb_gold_buy` / `etf_flow`），这里只负责拼。

    ⚠️ 落位日是**可见日**（央行购金 +45 天、ETF 持仓 +40 天），不是数据所属日
    ——旧实现用未推后的序列，等于提前 40~45 天就用到了数据（前视）。
    见 CHANGES v5.45（含季度聚合变化的对照表）。
    """
    getattr_ok = ("cb_holdings", "gold_etf_holdings")
    if not all(c in base.columns for c in getattr_ok):
        _warn(f"耦合层没给 {getattr_ok}，CB_GOLD_BUY / ETF_FLOW 整列 NaN")
        return {}
    out = {
        "CB_GOLD_BUY": features.cb_gold_buy(base),
        "ETF_FLOW": features.etf_flow(base),
    }
    return {k: v for k, v in out.items() if v.notna().any()}


def load_raw(series=None, meta=None) -> pd.DataFrame:
    """面板 = 耦合层基础列 + 两条流量列。`meta` / `series` 都不再需要。"""
    base = get_data(Panel.CAUSUAL_GOLD)
    df = base[list(PANEL_COLS)].copy()
    for col, s in _derived(df, base).items():
        df[col] = s
    return df



def prepare(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.index = pd.DatetimeIndex(df.index)
    df = df[~df.index.duplicated(keep="last")].sort_index()

    df["GOLD_RET"] = df["close"].pct_change() * 100.0
    df["XAU_RET"] = df["xau_usd"].pct_change() * 100.0
    df["USDCNY_RET"] = df["usdcny"].pct_change() * 100.0
    df["RY_CHG_BP"] = df["real_yield_10y"].diff() * 100.0
    df["VIX_CHG"] = df["vix"].diff()
    df["USDX_CHG"] = df["usd_index"].diff()
    df["GEO_CHG"] = df["geopolitic_risk"].diff()
    df["FED_CHG_BP"] = df["fed_rate"].diff() * 100.0
    oi = df["gold_open_interest"].replace(0, np.nan)
    df["OI_CHG_PCT"] = (oi.pct_change() * 100.0).clip(-50, 50)
    return df


# ------------------------------------------------------------------ 状态



def build_panel(asof: str | None = None, series=None, meta=None,
                state: dict | None = None) -> tuple[pd.DataFrame, dict]:
    """拼面板。

    `state` 由**调用方**给出（service 从状态层端口取、CLI 直接取），
    结构 `{"labels": Series, "label_name": dict, "version": str}`。
    因果层不 import 状态层——只消费传进来的标签。
    """
    if not state or "spec" not in state:
        raise RuntimeError(
            f"{C.ASSET} 缺少状态模型的 spec——调用方要把 state['spec'] 传进来"
            "（state_layer.port.causual_view 会给）")
    df = prepare(load_raw(series, meta))
    spec = state["spec"]

    set_state_vocab(state)
    # 注意不能写 `x or default`：Series 的真值有歧义
    labels = (state or {}).get("labels")
    if labels is None:
        labels = pd.Series(dtype=str)
    labels = labels.reindex(df.index).fillna("").astype(str)
    df[LABEL_COL] = labels
    if asof:
        df = df.loc[: pd.Timestamp(asof)]
    return df, spec


def set_state_vocab(state: dict | None) -> None:
    """把状态层的标签词表装进本模块（卡片分组、展示名要用）。"""
    global LABELS, LABEL_NAME
    if not state:
        return
    LABELS = tuple(state.get("labels_order") or LABELS)
    LABEL_NAME = dict(state.get("label_name") or LABEL_NAME)



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
        outcome_col={C.TARGET: "close", "GOLD_RET": "GOLD_RET"},
        target=C.TARGET, label_col=LABEL_COL, return_col="GOLD_RET",
        train=train, seed=seed,
    )
    path = snapshot.save(C.ASSET, models, spec["data"]["label_signature"], fit_date=str(df.index[-1].date()))
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
    bundle.label_signature = spec["data"]["label_signature"]
    profile = pd.DataFrame(spec["data"]["profile"]).set_index("label")
    return build_card(
        C.ASSET, C.TITLE, df, when,
        label_col=LABEL_COL, label_name=LABEL_NAME, label_order=list(LABELS),
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
                  freq="QE", pos_col="cb_holdings", pos_basis="level",
                  mom_col="cb_holdings", mom_basis="yoy", roll_n=4,
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
    bundle.label_signature = spec["data"]["label_signature"]
    profile = pd.DataFrame(spec["data"]["profile"]).set_index("label")
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
