"""A 股面板：从 datahub 拼出因果/状态层要用的日频宽表。

调用方（`pipeline.load_raw`）只需要 `panel()`；其余是内部映射与派生。
"""

from __future__ import annotations

import pandas as pd

from datahub import panel as common

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

# 分组用于派生：按宽基规模排序
GROUPS = {
    "SSE50": ["510050", "510710", "510100", "510190"],
    "CSI300": ["510300", "510310", "510330"],
    "CSI1000": ["512100", "560010", "510580", "510510"],
}


def _multi(table: str, series: dict) -> pd.DataFrame:
    """把 series 里 "表名:列名" 的若干列拼回宽表。"""
    cols = {k.split(":", 1)[1]: v for k, v in series.items()
            if k.startswith(f"{table}:")}
    return pd.DataFrame(cols)


def _derived(idx: pd.DatetimeIndex, series) -> dict[str, pd.Series]:
    """由 `etf_shares` **现算**的派生列（数据层不存派生量）。"""
    out: dict[str, pd.Series] = {}
    try:
        w = _multi("etf_shares", series)
        if not isinstance(w, pd.DataFrame) or w.shape[1] < 3:
            return out
        w = w.rename(columns=lambda c: str(c)[1:] if str(c).startswith("c") else str(c))

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
        nat = sum(wide).diff()
        out["NATIONAL_ETF"] = nat.reindex(idx, method="ffill")
        q95 = nat.rolling(60, min_periods=40).quantile(0.95)
        out["NATIONAL_ETF_EVENT"] = (nat > q95).astype(float).reindex(
            idx, method="ffill").fillna(0)
    except Exception:  # noqa: BLE001
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


