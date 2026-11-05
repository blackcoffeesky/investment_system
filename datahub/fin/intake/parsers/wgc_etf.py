"""WGC 黄金 ETF 月报解析 → 全球黄金 ETF **持仓量**（吨，月末）。

输入：WGC 的 `ETF_Flows_*.xlsx`，工作表 `Fund flows by month`。
结构：第 6 行是表头（Date / Gold US$/oz / Ounces / Tonnes / Value），
前 5 列是**全部基金合计**，右侧各列是单只基金。

为什么存持仓而不是流入
----------------------
WGC 这张表给的是**持仓水平**，"净流入"是它的一阶差分。和央行持仓一样：
只存水平，流量由消费层 diff 得到——避免同一件事存两份、还容易对不上。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

SHEET = "Fund flows by month"
HEADER_ROW = 5          # 0-based，第 6 行
AGG_COLS = 5            # 前 5 列是合计：Date/Gold/Ounces/Tonnes/Value


def parse(path: str | Path) -> pd.Series:
    """→ Series（index=月末，值=全球黄金 ETF 持仓，吨）。"""
    p = Path(path)
    try:
        d = pd.read_excel(p, sheet_name=SHEET, header=HEADER_ROW)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"读不了 {p.name} 的 '{SHEET}' 工作表：{e}\n"
            "WGC 可能改了工作表名或表头位置，请更新 parsers/wgc_etf.py。") from e

    if d.shape[1] < 4:
        raise RuntimeError(f"{p.name} 的 '{SHEET}' 列数异常：{d.shape[1]}")

    # 前 5 列固定是合计列；列名可能带空格/换行，用位置取
    agg = d.iloc[:, :AGG_COLS].copy()
    agg.columns = ["date", "gold", "ounces", "tonnes", "value"]

    agg["date"] = pd.to_datetime(agg["date"], errors="coerce")
    agg = agg.dropna(subset=["date"]).set_index("date").sort_index()
    tonnes = pd.to_numeric(agg["tonnes"], errors="coerce").dropna()

    if tonnes.empty:
        raise RuntimeError(f"{p.name} 的 Tonnes 列解析为空，列结构可能变了。")

    tonnes.index = tonnes.index + pd.offsets.MonthEnd(0)
    tonnes = tonnes[~tonnes.index.duplicated(keep="last")].sort_index()
    tonnes.name = "gold_etf_holdings"
    return tonnes
