"""A 股面板：从 datahub 拼出因果/状态层要用的日频宽表。

调用方（`pipeline.load_raw`）只需要 `panel()`；其余是内部映射与派生。
"""

from __future__ import annotations

import sys

import pandas as pd

from shared import panel_align as common

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
    # 年度分红率：payout_ratio 只存在于 dividend_annual 的 payout_ratio 列
    "PAYOUT": ("dividend_annual", "payout_ratio"),
}

# 旧面板有、数据层没有的列：补空列，避免下游 KeyError
STUB_COLS = ["SOCIAL_FINANCING", "EQUITY_SUPPLY", "AFRE_STOCK", "EQUITY_FIN_STOCK"]

# 分组用于派生：按宽基规模排序（裸 6 位码；比对时两侧都归一化）
GROUPS = {
    "SSE50": ["510050", "510710", "510100", "510190"],
    "CSI300": ["510300", "510310", "510330"],
    "CSI1000": ["512100", "560010", "510580", "510510"],
}


def bare_code(name: object) -> str:
    """把各种写法的 ETF 代码归一成裸 6 位数字。

    数据层 2026-11 起给代码加了市场前缀（`etf_shares:sh510300`），之前是
    `c510300` / `510300` 之类的写法。统一取**最后 6 位数字**：
    `sh510300` / `sz159919` / `c510300` / `510300` / `510300.SH` → `510300`。

    不认得的形状原样返回（不足 6 位数字）——匹配不上就自然跳过，
    但「派生列整体消失」会被 `tests/test_causual_panel_derived.py` 抓住。
    """
    digits = "".join(ch for ch in str(name) if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else str(name)


def _warn(msg: str) -> None:
    """派生列降级时**要响**：不抛异常（下游还能跑），但必须说出来。"""
    print(f"[面板] {msg}", file=sys.stderr)


def _multi(table: str, series: dict) -> pd.DataFrame:
    """把 series 里 "表名:列名" 的若干列拼回宽表。"""
    cols = {k.split(":", 1)[1]: v for k, v in series.items()
            if k.startswith(f"{table}:")}
    return pd.DataFrame(cols)


def _derived(idx: pd.DatetimeIndex, series) -> dict[str, pd.Series]:
    """由 `etf_shares` **现算**的派生列（数据层不存派生量）。

    2026-11 的坑：数据层把 ETF 代码从裸码改成带前缀（`sh510300`），这里还按
    裸码和 `GROUPS` 比对，一个都匹配不上 → 四个派生列**整体消失**，
    而且 `sum([])` 抛的 `AttributeError` 被外层 except 吞掉，全程无报错。
    所以现在：① 列名与 `GROUPS` 两侧都过 `bare_code`；② 降级时打印原因。
    """
    out: dict[str, pd.Series] = {}
    try:
        raw = _multi("etf_shares", series)
        if not isinstance(raw, pd.DataFrame) or raw.shape[1] < 3:
            n = raw.shape[1] if isinstance(raw, pd.DataFrame) else 0
            _warn(f"etf_shares 可用列只有 {n} 个，"
                  "SIZE_FLOW / ETF_DISPERSION / NATIONAL_ETF 全部缺席")
            return out

        # 代码前缀怎么变都不怕：两侧统一按裸 6 位比
        norm: dict[str, pd.Series] = {}
        for c in raw.columns:
            b = bare_code(c)
            if b in norm:
                _warn(f"etf_shares 归一后代码重复：{b}（多列），只保留最后一列")
            norm[b] = raw[c]
        w = pd.DataFrame(norm)

        def group(g: str):
            cols = [c for c in GROUPS.get(g, []) if c in w.columns]
            return w[cols].sum(axis=1) if cols else None

        small, large = group("CSI1000"), group("CSI300")
        if small is not None and large is not None:
            out["SIZE_FLOW"] = (small.pct_change() - large.pct_change()).reindex(
                idx, method="ffill")

        parts = [g.pct_change() for g in
                 (group("SSE50"), group("CSI300"), group("CSI1000")) if g is not None]
        if len(parts) >= 2:
            out["ETF_DISPERSION"] = pd.concat(parts, axis=1).std(axis=1).reindex(
                idx, method="ffill")

        wide = [g for g in (group("SSE50"), group("CSI300")) if g is not None]
        if not wide:
            _warn("SSE50 / CSI300 一个都没匹配上，NATIONAL_ETF 缺席")
            return out
        nat = sum(wide).diff()
        out["NATIONAL_ETF"] = nat.reindex(idx, method="ffill")
        q95 = nat.rolling(60, min_periods=40).quantile(0.95)
        out["NATIONAL_ETF_EVENT"] = (nat > q95).astype(float).reindex(
            idx, method="ffill").fillna(0)
    except Exception as exc:  # noqa: BLE001
        _warn(f"派生列计算失败（{type(exc).__name__}: {exc}），本次只保留已算出的 "
              f"{sorted(out)}")
        return out
    return out


def panel(series, meta=None) -> pd.DataFrame:
    df = common.build({**SHARED, **ASHARE}, index_table="csi_all",
                  series=series, meta=meta)
    for col, s in _derived(df.index, series).items():
        df[col] = s
    for col in STUB_COLS:
        if col not in df.columns:
            df[col] = float("nan")
    return df


