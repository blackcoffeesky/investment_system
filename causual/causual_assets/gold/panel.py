"""黄金面板：从 datahub 拼出因果/状态层要用的日频宽表。"""

from __future__ import annotations

import pandas as pd

from shared import panel_align as common

SHARED = {
    "VIX": "vix",
    "USDCNY": "usdcny",
    "USD_INDEX": "usd_index",
    "FED_RATE": "fed_rate",
    "REAL_YIELD_10Y": "real_yield_10y",
    "GEOPOLITIC_RISK": "geopolitic_risk",
}

GOLD = {
    "OPEN": ("gold_cny", "open"),
    "HIGH": ("gold_cny", "high"),
    "LOW": ("gold_cny", "low"),
    "CLOSE": ("gold_cny", "close"),
    "GOLD_CNY": ("gold_cny", "close"),
    "VOLUME": ("gold_cny", "volume"),
    "XAU_USD": "xau_usd",
    "CB_HOLDINGS": "cb_holdings",
    "ETF_HOLDINGS": "gold_etf_holdings",
    "OPEN_INTEREST": "gold_open_interest",
    "COT_NET_LONG": "gold_cot_net_long",
    "CPI_SURPRISE": "gold_cpi_surprise",
}


def panel(series, meta=None) -> pd.DataFrame:
    """拼面板。

    **流量列由库里的持仓水平差分得到**，不要拿面板里的日频 ffill 列去 resample 再 diff：
    月/季末的落点依赖交易日，那样会错位一到两期（实测 ETF 流入相关系数只有 0.32，
    还会在尾部多出一个"未来季度"）。
    """
    df = common.build({**SHARED, **GOLD}, index_table="gold_cny",
                      series=series, meta=meta)

    def flow_from(table: str) -> pd.Series | None:
        """持仓 → 差分，且**只落在真实观测日对应的交易日上**。

        不能直接 ffill 到日频：pair_view 会再按季/月 resample，ffill 出来的
        当前季会被当成"已经观测到的季度"，凭空多出一个未来期（尾巴重复）。
        """
        try:
            h = series[table].dropna()
        except Exception:  # noqa: BLE001
            return None
        if len(h) < 2:
            return None
        flow = h.diff().dropna()
        out = pd.Series(index=df.index, dtype=float)
        for d, v in flow.items():
            pos = df.index.searchsorted(d, side="left")   # 第一个 >= 观测日的交易日
            if pos < len(df.index):
                out.iloc[pos] = float(v)
        return out

    buy = flow_from("cb_holdings")
    if buy is not None:
        df["CB_GOLD_BUY"] = buy
    etf = flow_from("gold_etf_holdings")
    if etf is not None:
        df["ETF_FLOW"] = etf
    return df


