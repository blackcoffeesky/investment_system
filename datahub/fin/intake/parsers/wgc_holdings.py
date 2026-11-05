"""WGC 央行黄金储备季报解析。

输入：WGC 的 `Data` 表（一行一个国家，一列一个季度，单位吨），
输出：**篮子国家合计**的季度持仓序列，date = 季末最后一天。

为什么要篮子
------------
WGC 覆盖 123 个国家，但只有 87 国是固定篮子（见 `cb_basket.txt`）。
不固定篮子的话，某个国家新纳入统计会造成"持仓跳增"的假信号。

修订
----
WGC 会回溯修订最近两个季度（`AWAITED` 表示尚未收到）。
所以每次都重算全表：数据库按 date upsert，天然覆盖修订值。
缺失季度按**上一季度**补（AWAITED 当持平处理）。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

BASKET_FILE = Path(__file__).parent / "cb_basket.txt"


def _basket() -> list[str]:
    return [l.strip() for l in BASKET_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]


def _quarter_end(label: str) -> pd.Timestamp:
    """'Q2 26' → 2026-06-30"""
    q, yy = label.upper().split()
    qn = int(q[1:])
    year = 2000 + int(yy)
    month = qn * 3
    return pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(0)


def parse(path: str | Path) -> pd.Series:
    """→ Series（index=季末，值=篮子国家持仓合计，吨）。"""
    p = Path(path)
    try:
        raw = pd.read_excel(p, sheet_name="Data", header=0)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"读不了 {p.name}：{e}\n"
            "raw 结构可能变了（WGC 改过表名/表头）。请检查是否还有 'Data' 工作表、"
            "第一列是否仍是 Country。") from e

    if "Country" not in raw.columns:
        raise RuntimeError(
            f"{p.name} 的 Data 表里没有 'Country' 列，实际列：{list(raw.columns)[:6]}…\n"
            "WGC 可能改了表结构，请更新 parsers/wgc_holdings.py。")

    basket = _basket()
    missing = [b for b in basket if b not in set(raw["Country"].astype(str))]
    if missing:
        raise RuntimeError(
            f"篮子里有 {len(missing)} 个国家在表中找不到：{missing[:5]}…\n"
            "可能是国家改名或从统计中移除，请更新 cb_basket.txt。")

    qcols = [c for c in raw.columns if str(c).upper().startswith("Q") and len(str(c)) <= 6]
    if not qcols:
        raise RuntimeError(
            f"{p.name} 里找不到形如 'Q1 26' 的季度列。"
            "WGC 可能改了列名格式，请更新 parser。")

    sub = raw[raw["Country"].astype(str).isin(basket)].set_index("Country")[qcols]
    num = sub.apply(pd.to_numeric, errors="coerce")
    # AWAITED / 空 → 按上季度补（持仓不变），第一列缺失才留空
    num = num.ffill(axis=1)

    total = num.sum(axis=0, min_count=1).dropna()
    total.index = [_quarter_end(c) for c in total.index]
    total = total.sort_index()
    total.name = "cb_holdings"
    return total
