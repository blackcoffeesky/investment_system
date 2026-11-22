"""黄金插件：把 状态层 + causual_core 组装起来。

面板**由装配点传进来**（`load_raw(panel)`）：本文件不取数、不调 `get_data`。
两条流量列（央行购金 / ETF 持仓变化）也已由耦合层算好，这里只做装配。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from shared.adapter import Panel, tables_of
from shared.utils import data_processing as dp
from causual.causual_core import snapshot
from causual.causual_core.spec import label_signature_of
from causual.causual_core.estimators import EdgeModel, fit_all
from causual.web.render import build_card, build_console
from causual.causual_core.signal_layer import build_bundle
from causual.causual_assets.gold import config as C

# 本资产要哪个面板（装配点按这个取数：`get_data(PANEL_KIND)`）
PANEL_KIND = Panel.CAUSUAL_GOLD

# 面板底层读哪些表 = 耦合层的声明（**不抄一份**）
REQUIRED_TABLES = tables_of(PANEL_KIND)

# 基础列 = 耦合层的**语义列名**（`CLOSE`/`VIX` 那套大写内部名已退役）。
PANEL_COLS: tuple[str, ...] = (
    # 价格 / 量
    "open", "high", "low", "close", "volume",
    # 黄金供需（存量：`cb_holdings` / `gold_etf_holdings` 按 raw 策略给）
    "xau_usd", "cb_holdings", "gold_etf_holdings",
    "gold_open_interest", "gold_cot_net_long", "gold_cpi_surprise",
    # 共享宏观
    "vix", "usdcny", "usd_index", "fed_rate", "real_yield_10y", "world_tension",
)

# 由**耦合层算好**的派生列（存量 → 流量的差分，落在发布**可见日**）。
DERIVED_COLS: tuple[str, ...] = ("cb_gold_buy", "etf_flow")

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "causual" / "data"   # 状态模型产物由状态层写，这里只读

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
    """派生列接缝：把**耦合层算好的**两条流量列装配进面板。

    存量（`cb_holdings` / `gold_etf_holdings`）→ 流量的差分现在由 datahub 的
    `DERIVED` 注册表算（`shared.utils.features.cb_gold_buy` / `etf_flow` 已迁走），
    落位日是**发布可见日**（央行购金 +45 天、ETF 持仓 +40 天），不是数据所属日
    ——旧实现在 causual 里按观测日差分，等于提前 40~45 天用到数据（前视）。
    见 CHANGES v5.45（含季度聚合变化的对照表）。

    ⚠️ 接缝**必须保留**：测试的反向验证打桩它（返回空 dict → 派生列消失 → 断言变红）。

    **缺列直接 `KeyError`**：与 `PANEL_COLS` 同一套失败策略（静默 NaN 比崩溃难查）。
    """
    missing = [c for c in DERIVED_COLS if c not in base.columns]
    if missing:
        raise KeyError(f"{PANEL_KIND.name} 缺这些派生列：{missing}")
    return {c: base[c] for c in DERIVED_COLS}


def load_raw(panel: pd.DataFrame) -> pd.DataFrame:
    """面板 → 因果层宽表。**数据由装配点传入，本函数不取数。**"""
    df = panel[list(PANEL_COLS)].copy()
    for col, s in _derived(df, panel).items():
        df[col] = s
    return df



def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """从面板算 causual 自己的特征（**列名保持大写**：causual 的特征，不是耦合层的列）。

    单位原语来自 `shared.utils.data_processing`：利率类是 `diff_bp`（bp），
    价格/持仓类是 `pct_change_pct`（百分数）——两者别混用。
    """
    df = df.copy()
    df.index = pd.DatetimeIndex(df.index)
    df = df[~df.index.duplicated(keep="last")].sort_index()

    df["GOLD_RET"] = dp.pct_change_pct(df["close"])
    df["XAU_RET"] = dp.pct_change_pct(df["xau_usd"])
    df["USDCNY_RET"] = dp.pct_change_pct(df["usdcny"])
    df["RY_CHG_BP"] = dp.diff_bp(df["real_yield_10y"])
    df["VIX_CHG"] = df["vix"].diff()
    df["USDX_CHG"] = df["usd_index"].diff()
    df["GEO_CHG"] = df["world_tension"].diff()
    df["FED_CHG_BP"] = dp.diff_bp(df["fed_rate"])
    oi = df["gold_open_interest"].replace(0, np.nan)
    df["OI_CHG_PCT"] = dp.pct_change_pct(oi).clip(-50, 50)
    return df


# ------------------------------------------------------------------ 状态



def build_panel(asof: str | None = None, panel: pd.DataFrame | None = None,
                state: dict | None = None) -> tuple[pd.DataFrame, dict]:
    """拼面板。

    `panel` 由**装配点**传进来（`get_data(Panel.CAUSUAL_GOLD)` 的输出）。
    `state` 也由调用方给出（service 从状态层端口取、CLI 直接取），
    结构 `{"labels": Series, "label_name": dict, "version": str}`。
    因果层不 import 状态层——只消费传进来的标签。
    """
    if not state or "spec" not in state:
        raise RuntimeError(
            f"{C.ASSET} 缺少状态模型的 spec——调用方要把 state['spec'] 传进来"
            "（state_layer.port.state_view 会给）")
    if panel is None:
        raise RuntimeError(f"{C.ASSET} 的面板没有传进来（装配点负责 get_data）")
    df = prepare(load_raw(panel))
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
    path = snapshot.save(C.ASSET, models, label_signature_of(spec), fit_date=str(df.index[-1].date()))
    if verbose:
        from causual.causual_core.evaluate import sign_audit
        print(sign_audit(models).round(4).to_string(index=False))
        print(f"\n快照 -> {path}")
    return {"models": models, "path": path}


def make_card(df: pd.DataFrame, spec: dict, models: dict, when: pd.Timestamp,
              profile: pd.DataFrame,
              data_notes: list[str] | None = None,
              state_line: str | None = None) -> list:
    bundle = build_bundle(
        df, models, EDGES, label_col=LABEL_COL,
        treatment_col=TREATMENT_COL, target=C.TARGET, signal_lag=SIGNAL_LAG,
    )
    bundle.label_signature = label_signature_of(spec)
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


def make_detail_card(df: pd.DataFrame, spec: dict, models: dict, when: pd.Timestamp,
                      profile: pd.DataFrame) -> list:
    from causual.web.render import build_detail_card

    bundle = build_bundle(
        df, models, EDGES, label_col=LABEL_COL,
        treatment_col=TREATMENT_COL, target=C.TARGET, signal_lag=SIGNAL_LAG,
    )
    bundle.label_signature = label_signature_of(spec)
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
