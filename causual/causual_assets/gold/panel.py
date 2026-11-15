"""黄金面板：基础列来自耦合层，流量列由原始持仓差分得到。

**基础列走 `shared.adapter.get_data`**：数据层内部怎么变都不用改这里。

⚠️ 流量列（`CB_GOLD_BUY` / `ETF_FLOW`）**必须用原始观测序列**，不能用面板里
ffill 过的列：日频 ffill 之后 `diff()` 大多为 0，真实的月度/季度变化会被抹平。
所以这两列这轮仍从 `series` 取（`series[表名]` 是未对齐的原始序列），
第二批把「按观测日差分」做成 `shared/utils/features.py` 里的纯函数后再收进耦合层。
"""

from __future__ import annotations

import sys

import pandas as pd

from shared.adapter import Panel, column_of, get_data

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

# 流量列依赖的原始表（差分要用未对齐的观测序列）
FLOW_TABLES = {"CB_GOLD_BUY": "cb_holdings", "ETF_FLOW": "gold_etf_holdings"}


def _warn(msg: str) -> None:
    print(f"[面板] {msg}", file=sys.stderr)


def panel(series=None, meta=None) -> pd.DataFrame:
    """基础列来自耦合层；`series` 只用于两条流量列（第二批收编）。

    `meta` 已不需要——对齐要的 `lag_days` 由耦合层从 datahub 取。
    `series` 为 None 时**出声**：否则两条流量列会静默缺席。
    """
    base = get_data(Panel.CAUSUAL_GOLD)
    df = pd.DataFrame({out: base[column_of(spec)]
                       for out, spec in {**SHARED, **GOLD}.items()},
                      index=base.index)

    if series is None:
        _warn("panel() 没收到 series：CB_GOLD_BUY / ETF_FLOW 会整列缺席"
              "（流量必须按原始观测日差分，不能用 ffill 后的面板列）")
        return df

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

    for col, table in FLOW_TABLES.items():
        flow = flow_from(table)
        if flow is not None:
            df[col] = flow
    return df
