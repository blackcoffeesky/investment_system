"""A 股面板：基础列来自耦合层，派生列在这里现算。

调用方（`pipeline.load_raw`）只需要 `panel()`；其余是内部映射与派生。

**基础列（价格 + 宏观 + 供给）走 `shared.adapter.get_data`**：数据层内部怎么变
（表名、宽表拆解、代码格式）都不用改这里。派生列（ETF 份额、股息率）本轮仍从
`series` 取——它们下一步才搬进 `shared/utils/features.py`。
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from shared.adapter import Panel, column_of, get_data
from shared.adapter import bare_code as _bare_code
from shared.utils import panel_align as common

# 共享宏观：两个资产都用同一份
SHARED = {
    "VIX": "vix",
    "USDCNY": "usdcny",
    "USD_INDEX": "usd_index",
    "FED_RATE": "fed_rate",
    "REAL_YIELD_10Y": "real_yield_10y",
    "GEOPOLITIC_RISK": "geopolitic_risk",
}

ASHARE = {
    "OPEN": ("csi_all", "open"),
    "HIGH": ("csi_all", "high"),
    "LOW": ("csi_all", "low"),
    "CLOSE": ("csi_all", "close"),
    "VOLUME": ("csi_all", "volume"),
    "CSI_ALL": ("csi_all", "close"),
    "CSI300": "csi300",
    "CSI1000": "csi1000",
    "SHIBOR3M": "shibor3m",
    "MARGIN_BALANCE": "margin_balance",
    "PMI": "pmi",
    "INSIDER_NET": "insider_net",
    "RETAIL_SALES": "retail_sales",
    "RETAIL_SALES_MOM": "retail_sales_mom",
    "CPI_YOY": "cpi_yoy",
    "PPI_YOY": "ppi_yoy",
    "INDUSTRIAL_YOY": "industrial_yoy",
    "EXPORTS_YOY": "exports_yoy",
    "M1_YOY": "m1_yoy",
    "M2_YOY": "m2_yoy",
    "M1_LEVEL": "m1_level",
    "M2_LEVEL": "m2_level",
    "TOTAL_SHARES": "total_shares",
    "TOTAL_MKTCAP": "total_mktcap",
    "EQUITY_FIN_MONTHLY": "equity_fin_monthly",
}

# 旧面板有、数据层没有的列：补空列，避免下游 KeyError
STUB_COLS = ["SOCIAL_FINANCING", "EQUITY_SUPPLY", "AFRE_STOCK", "EQUITY_FIN_STOCK"]

# 股息率（年度，百分数）：datahub **现算的派生量**，不落表。
# 由装配点（`causual_core.cli._load_series`）按 "表名:列名" 塞进 series，
# 和 `etf_shares` 那几列同一个套路——面板只负责对齐，不负责取数。
DIVIDEND_YIELD_KEY = "dividend_yield:yield_mcap_weighted"

# 耦合层给的 ETF **分组份额**列（组名/成员见 `shared/adapter` 的 _ETF_GROUPS）。
# 派生列从这里算，不再自己剥代码前缀、也不再自己维护 GROUPS 清单。
ETF_GROUP_COLS = ("etf_sse50", "etf_csi300", "etf_csi1000")

# 转出口：外部脚本 / 测试还在 `from ...panel import bare_code`（归一逻辑已上移到 adapter）
bare_code = _bare_code


def _warn(msg: str) -> None:
    """派生列降级时**要响**：不抛异常（下游还能跑），但必须说出来。"""
    print(f"[面板] {msg}", file=sys.stderr)


def _finite(s: pd.Series) -> pd.Series:
    """±inf → NaN。

    ETF 在**上市前份额是 0**，`pct_change()` 会给出 `inf`（首日 0 → 正数）。
    实测 `SIZE_FLOW` 有 42 个 inf，全在 2016 年初。inf 本身不报错，但一旦进
    z-score / 分位 / 相关系数就会把整列污染成 inf 或 nan——所以在这里掐掉。
    """
    return s.replace([np.inf, -np.inf], np.nan)


def _derived(idx: pd.DatetimeIndex, base: pd.DataFrame) -> dict[str, pd.Series]:
    """宽基 ETF 的派生列，输入是耦合层给的**分组份额**（不读 series）。

    2026-11 的坑：数据层把 ETF 代码从裸码改成带前缀（`sh510300`），消费方各自
    按裸码匹配 → 四个派生列**整体消失**，`sum([])` 抛的异常又被吞，全程无报错。
    现在代码归一与分组都在 `shared/adapter` 里做，这里只吃 `etf_sse50/csi300/csi1000`
    三列——**数据层的代码格式再变也伤不到这里**。

    分组份额是「原生观测日有值、其余 NaN」，所以先在 `.dropna()` 上算变化率，
    再把结果 ffill 回交易日（与派生口径一致）。
    """
    out: dict[str, pd.Series] = {}
    groups = {c: base[c].dropna() for c in ETF_GROUP_COLS
              if c in base.columns and base[c].notna().any()}
    if len(groups) < 2:
        _warn(f"ETF 分组份额只有 {len(groups)} 组可用（耦合层没给全？），"
              "SIZE_FLOW / ETF_DISPERSION / NATIONAL_ETF 全部缺席")
        return out
    try:
        small, large = groups.get("etf_csi1000"), groups.get("etf_csi300")
        if small is not None and large is not None:
            out["SIZE_FLOW"] = _finite(
                (small.pct_change() - large.pct_change()).reindex(
                    idx, method="ffill"))

        parts = [groups[c].pct_change() for c in ETF_GROUP_COLS if c in groups]
        if len(parts) >= 2:
            out["ETF_DISPERSION"] = _finite(
                pd.concat(parts, axis=1).std(axis=1).reindex(idx, method="ffill"))

        wide = [groups[c] for c in ("etf_sse50", "etf_csi300") if c in groups]
        if not wide:
            _warn("etf_sse50 / etf_csi300 一个都没匹配上，NATIONAL_ETF 缺席")
            return out
        nat = sum(wide).diff()
        out["NATIONAL_ETF"] = _finite(nat.reindex(idx, method="ffill"))
        q95 = nat.rolling(60, min_periods=40).quantile(0.95)
        out["NATIONAL_ETF_EVENT"] = (nat > q95).astype(float).reindex(
            idx, method="ffill").fillna(0)
    except Exception as exc:  # noqa: BLE001
        _warn(f"派生列计算失败（{type(exc).__name__}: {exc}），本次只保留已算出的 "
              f"{sorted(out)}")
        return out
    return out


def _dividend_yield(idx: pd.DatetimeIndex, series) -> pd.Series:
    """年度股息率（全市场，市值加权，%）→ 日频。

    装配点已经把它对齐到**次年 1 月 1 日**（年 Y 的除息日都落在 Y 内，数据到
    Y+1 才完整），所以这里只做 ffill：**Y 年看到的是 Y-1 的值，无前视**。

    缺数据时给整列 NaN 并告警——它是供需层的展示项，不该拖垮整张面板；
    但要出声，否则又变成"列悄悄没了"（`tests/test_causual_panel_derived.py`
    盯着它有没有值）。
    """
    s = series.get(DIVIDEND_YIELD_KEY)
    if s is None or len(s) == 0:
        _warn(f"series 里没有 {DIVIDEND_YIELD_KEY!r}（datahub 的派生量没装配上？）"
              "，DIVIDEND_YIELD 置 NaN")
        return pd.Series(index=idx, dtype=float)
    return common.to_daily(s, idx, 0)


def panel(series=None, meta=None) -> pd.DataFrame:
    """基础列 + ETF 派生列来自耦合层；`series` 只用于**股息率**（第二批就搬走）。

    `meta` 已不需要——对齐要的 `lag_days` 由耦合层从 datahub 取。
    `series` 为 None 时**必须出声**：否则 `DIVIDEND_YIELD` 会静默整列 NaN，
    正是这两天在杀的那类 bug（列还在、只是没值）。
    """
    base = get_data(Panel.CAUSUAL_ASHARE)
    df = pd.DataFrame({out: base[column_of(spec)]
                       for out, spec in {**SHARED, **ASHARE}.items()},
                      index=base.index)
    for col, s in _derived(df.index, base).items():
        df[col] = s
    if series is None:
        _warn("panel() 没收到 series：DIVIDEND_YIELD 会整列 NaN（过渡期必须由装配点传）")
    df["DIVIDEND_YIELD"] = _dividend_yield(df.index, series or {})
    for col in STUB_COLS:
        if col not in df.columns:
            df[col] = float("nan")
    return df


