"""A 股插件：中证全指 + 量价状态 + 股票供给。

面板**由装配点传进来**（`load_raw(panel)`）：本文件不取数、不调 `get_data`。
派生列（ETF 四列 / 股息率）也已由耦合层算好，这里只做**装配**与特征计算。
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
from causual.web.render import build_card
from causual.causual_core.signal_layer import build_bundle
from causual.causual_assets.ashare import config as C

# 本资产要哪个面板（装配点按这个取数：`get_data(PANEL_KIND)`）
PANEL_KIND = Panel.CAUSUAL_ASHARE

# 面板底层读哪些表 = 耦合层的声明（**不抄一份**，抄了就是两份真相）
REQUIRED_TABLES = tables_of(PANEL_KIND)

# 基础列 = 耦合层的**语义列名**（`CLOSE`/`VIX` 那套大写内部名已退役）。
# 少一列就 KeyError，比"悄悄缺列"好。
PANEL_COLS: tuple[str, ...] = (
    # 价格 / 量
    "open", "high", "low", "close", "volume",
    # 指数 / 资金
    "csi300", "csi1000", "margin_balance",
    # A 股宏观 / 供给
    "pmi", "insider_net", "retail_sales", "retail_sales_mom",
    "cpi_yoy", "ppi_yoy", "industrial_yoy", "exports_yoy",
    "m1_yoy", "m2_yoy", "m1_level", "m2_level",
    "total_shares", "total_mktcap", "equity_fin_monthly",
    # 共享宏观
    "vix", "usdcny", "usd_index", "fed_rate", "real_yield_10y", "world_tension",
)

# 由**耦合层算好**的派生列（datahub 的 `DERIVED` 注册表 → adapter 落成面板列）。
# 2026-11-16 起 causual 不再自己算这五列。
DERIVED_COLS: tuple[str, ...] = (
    "etf_dispersion", "size_flow", "national_etf", "national_etf_event",
    "dividend_yield",
)

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "causual" / "data"   # 状态模型产物由状态层写，这里只读

ASSET = C.ASSET
TITLE = C.TITLE
TARGET = C.TARGET
LABEL_COL = "regime_label"
LONG_REGIME_COL = "long_regime"
TARGET_COLS = {"close": "close", "ret": "RET", "open": "open"}
# 状态标签词表由调用方注入（set_state_vocab），因果层不 import 状态层
LABELS: tuple[str, ...] = ()
LABEL_NAME: dict[str, str] = {}
EDGES = C.EDGES
TREATMENT_COL = C.TREATMENT_COL
SOURCE_COL = C.SOURCE_COL
# 已知没数据的列（{列名: 原因}）。放在这里是为了让它成为**资产插件契约**的一部分：
# `causual_assets.load(asset)` 返回的就是这个模块，测试只看得见这里的东西。
NOT_CONNECTED = C.NOT_CONNECTED
SIGNAL_LAG = 0


def _derived(df: pd.DataFrame, base: pd.DataFrame) -> dict[str, pd.Series]:
    """派生列接缝：把**耦合层算好的**那几列装配进面板。

    2026-11-16 起这些列由 datahub 的 `DERIVED` 注册表算出、adapter 落成面板列，
    causual **不再计算**（`shared.utils.features` 那套算法已迁走）。这里只剩装配，
    所以是"从 `base` 取列"，不是"算"。

    ⚠️ 这个接缝**必须保留**：`tests/causual/test_causual_panel_derived.py` 的反向验证打桩它
    （让它返回空 dict），那几条"派生列必须存在且新鲜"的断言必须变红。哪怕将来它
    退化成一个空实现，也别删——它是那几条测试唯一的着力点。

    **缺列直接 `KeyError`**（不再"告警 + 整列 NaN"）：与 `PANEL_COLS` 同一套失败策略。
    静默 NaN 比崩溃难查——卡片上只会少一行，没人会去翻 stderr。
    """
    missing = [c for c in DERIVED_COLS if c not in base.columns]
    if missing:
        raise KeyError(f"{PANEL_KIND.name} 缺这些派生列：{missing}")
    return {c: base[c] for c in DERIVED_COLS}


def load_raw(panel: pd.DataFrame) -> pd.DataFrame:
    """面板 → 因果层宽表。**数据由装配点传入，本函数不取数。**

    `panel` = `get_data(Panel.CAUSUAL_ASHARE)` 的输出（基础列 + 派生列）。
    """
    df = panel[list(PANEL_COLS)].copy()
    for col, s in _derived(df, panel).items():
        df[col] = s
    return df



def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """从面板算 causual 自己的特征（**列名保持大写**：它们是 causual 的特征，不是耦合层的列）。

    单位原语来自 `shared.utils.data_processing`（`diff_bp` / `pct_change_pct` /
    `rolling_z`）——和 state_layer 的 `_derive()` 调**同一个函数**，两边一致不再靠测试守，
    而是结构上不可能不一致。**别把 `diff_bp` 和 `pct_change_pct` 混用**：
    利差 1.00% → 1.10% 是 +10 bp，pct_change 算出来也是 +10%（在利率上纯属巧合）。
    """
    df = df.copy()
    df.index = pd.DatetimeIndex(df.index)
    df = df[~df.index.duplicated(keep="last")].sort_index()

    df["RET"] = dp.pct_change_pct(df["close"])
    df["RET_OC"] = (df["close"] / df["open"] - 1.0) * 100.0
    df["RY_CHG_BP"] = dp.diff_bp(df["real_yield_10y"])
    df["USDX_CHG"] = df["usd_index"].diff()
    df["VIX_CHG"] = df["vix"].diff()
    df["USDCNY_RET"] = dp.pct_change_pct(df["usdcny"])
    df["FED_CHG_BP"] = dp.diff_bp(df["fed_rate"])
    df["PMI_CHG"] = df["pmi"].diff()
    df["MARGIN_CHG"] = dp.pct_change_pct(df["margin_balance"])
    # 两融净流入（亿元/日）：余额差分。信号层用"净流入/流出方向"比余额水平更有信息
    df["MARGIN_NET"] = df["margin_balance"].diff()

    # 股票供给：月度净融资额 → z（去趋势后再标准化）。
    # 列 = `equity_fin_monthly`（耦合层的语义名，`PANEL_COLS` 保证它存在；
    # 缺列会在 `base[list(PANEL_COLS)]` 处 KeyError，不会走到这里）。
    # 这里仍保留"全空"守卫：数据源波动时产出整列 NaN 而不是把整张面板带崩，
    # 缺数据这件事由 `config.NOT_CONNECTED` + `test_causual_dead_columns` 卡住。
    sup = df["equity_fin_monthly"]
    if sup.notna().any():
        sup = sup.resample("ME").last()
        sup_d = sup - sup.rolling(8, min_periods=4).mean()
        sd = float(sup_d.std())
        df["SUPPLY_Z"] = ((sup_d / sd).reindex(df.index, method="ffill")
                          if sd > 1e-12 else np.nan)
    else:
        df["SUPPLY_Z"] = np.nan

    # 放量冲击：成交量 60 日 z（成交额不可得，见 PROBLEM P21）
    df["VOLUME_Z"] = dp.rolling_z(df["volume"], window=60, min_periods=20)
    df["AMOUNT_SHOCK"] = df["VOLUME_Z"]

    # 供需派生：**先在月频上算，再 ffill 回日频**。
    # 直接在日频上 diff 会得到 0（日频是 ffill 的），ΔM1 会恒等于 0。
    m1 = df["m1_level"].resample("ME").last()
    mk = df["total_mktcap"].resample("ME").last()
    fin = df["equity_fin_monthly"].resample("ME").last()
    ins = df["insider_net"].resample("ME").last()

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
    df["M1M2_GAP"] = df["m1_yoy"] - df["m2_yoy"]
    return df


def build_panel(asof: str | None = None, panel: pd.DataFrame | None = None,
                state: dict | None = None) -> tuple[pd.DataFrame, dict]:
    """拼面板。

    `panel` 由**装配点**传进来（`get_data(Panel.CAUSUAL_ASHARE)` 的输出）。
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
        outcome_col={C.TARGET: "close"}, target=C.TARGET,
        label_col=LABEL_COL, return_col="RET", train=train, seed=seed,
        local_nodes=C.LOCAL_NODES,
    )
    path = snapshot.save(C.ASSET, models, label_signature_of(spec), fit_date=str(df.index[-1].date()))

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
                  pos_col="m1_level", pos_basis="roll3",
                  mom_col="m1_level", mom_basis="yoy",
                  note="位置用 M1 三月滚动和，边际用同比"),
        pair_view(df, group="供需增量", label="净供给（融资−增持）", direction="-",
                  pos_col="NET_SUPPLY", pos_basis="roll3",
                  mom_col="NET_SUPPLY", mom_basis="level",
                  note="融资 +，增持 −"),
        # ---- 3.3 宏观环境
        pair_view(df, group="宏观环境", label="社零", direction="+",
                  pos_col="retail_sales_mom", pos_basis="level",
                  mom_col="retail_sales_mom", mom_basis="dma3",
                  note="当月同比"),
        pair_view(df, group="宏观环境", label="工业增加值", direction="+",
                  pos_col="industrial_yoy", pos_basis="level",
                  mom_col="industrial_yoy", mom_basis="dma3",
                  note="数据停在 2025-08"),
        pair_view(df, group="宏观环境", label="PMI", direction="+",
                  pos_col="pmi", pos_basis="level",
                  mom_col="pmi", mom_basis="mom",
                  note="扩散指数，50 为中枢"),
        pair_view(df, group="宏观环境", label="M1−M2", direction="+",
                  pos_col="M1M2_GAP", pos_basis="level",
                  mom_col="M1M2_GAP", mom_basis="yoy",
                  note="正=资金活化"),
        pair_view(df, group="宏观环境", label="CPI", direction="0",
                  pos_col="cpi_yoy", pos_basis="level",
                  mom_col="cpi_yoy", mom_basis="dma3",
                  note="方向不定：低通胀偏正、高通胀偏负"),
        pair_view(df, group="宏观环境", label="PPI", direction="+",
                  pos_col="ppi_yoy", pos_basis="level",
                  mom_col="ppi_yoy", mom_basis="dma3"),
        pair_view(df, group="宏观环境", label="出口", direction="+",
                  pos_col="exports_yoy", pos_basis="level",
                  mom_col="exports_yoy", mom_basis="dma3",
                  note="数据停在 2025-08"),
        # ---- 3.4 增长转化
        pair_view(df, group="增长转化", label="大股东净增持", direction="+",
                  pos_col="insider_net", pos_basis="roll3",
                  mom_col="insider_net", mom_basis="level"),
        # ---- 3.5 股东回报 / 估值
        # 年度口径，只有 11 个点：**只给位置，不判边际**（11 个年度点算不出可信 yoy，
        # 硬算出来的"同比分位"是伪量化）。freq="YE" 让分位在**年度序列**上算，
        # 否则按月 resample 后窗口 60 期里其实只有 5 个不同的值。
        pair_view(df, group="股东回报 / 估值", label="股息率（市值加权）", direction="+",
                  pos_col="dividend_yield", pos_basis="level",
                  freq="YE", min_periods=8,
                  note="年度，市值加权；只有位置维度"),
    ]


def supply_detail_items(df: pd.DataFrame) -> list[dict]:
    """详情卡追加：位置/边际的原始值与窗口。"""
    from causual.causual_core.macro_layer import pair_view

    return supply_items(df) + [
        pair_view(df, group="原始", label="总股本", direction="-",
                  pos_col="total_shares", pos_basis="level",
                  mom_col="total_shares", mom_basis="yoy", note="仅参考"),
        pair_view(df, group="原始", label="总市值", direction="+",
                  pos_col="total_mktcap", pos_basis="level",
                  mom_col="total_mktcap", mom_basis="yoy", note="含估值，仅参考"),
        pair_view(df, group="原始", label="股票融资", direction="-",
                  pos_col="equity_fin_monthly", pos_basis="roll3",
                  mom_col="equity_fin_monthly", mom_basis="level"),
        pair_view(df, group="原始", label="M1 同比", direction="+",
                  pos_col="m1_yoy", pos_basis="level", mom_col="m1_yoy", mom_basis="dma3"),
        pair_view(df, group="原始", label="M2 同比", direction="+",
                  pos_col="m2_yoy", pos_basis="level", mom_col="m2_yoy", mom_basis="dma3"),
        pair_view(df, group="原始", label="社零累计同比", direction="+",
                  pos_col="retail_sales", pos_basis="level",
                  mom_col="retail_sales", mom_basis="dma3", note="旧口径，带季节性"),
    ]


def make_card(df: pd.DataFrame, spec: dict, models: dict, when: pd.Timestamp,
              profile: pd.DataFrame,
              data_notes: list[str] | None = None,
              state_line: str | None = None) -> list:
    from causual.causual_core.macro_layer import structural_state

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
        structural=structural_state(asof=when),
        supply=supply_items(df),
        data_notes=data_notes,
        state_line=state_line,
    )


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
        supply_detail=supply_detail_items(df),
    )


