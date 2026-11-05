"""全局节点池：跨资产共享的变量名常量 + **传输速度标签**。

只放真的通用的节点（美元、美债、美联储、全球风险）。
资产专用节点（黄金的 CB_GOLD_BUY、A 股的 INSIDER_NET）在各自 `config.py` 里
用 `LOCAL_NODES` 声明，不进这个池子。

`transmission` 决定估计器：
  * ``fast`` —— 当期定价（汇率、VIX、美元指数），只估同日/隔夜，不加滞后项
  * ``slow`` —— 慢传导（社融、增持、股票供给），需要分布滞后

`max_lag` 是**该频率下**的最大滞后阶数（日频=天，月频=月）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Transmission = Literal["fast", "slow"]


@dataclass(frozen=True)
class Node:
    name: str
    freq: str          # D / W / M / Q
    calendar: str      # natural（自然日）/ asset（跟随资产交易日）
    transmission: Transmission = "fast"
    max_lag: int = 0
    desc: str = ""


# ---------------------------------------------------------------- 全局共享
FED_RATE = "FED_RATE"
CPI_SURPRISE = "CPI_SURPRISE"
USD_INDEX = "USD_INDEX"
REAL_YIELD = "REAL_YIELD_10Y"
VIX = "VIX"
GEOPOLITIC_RISK = "GEOPOLITIC_RISK"
USDCNY = "USDCNY"
XAU_USD = "XAU_USD"

SHARED: dict[str, Node] = {
    # 快变量：当天/隔夜就反映完，不需要滞后项
    FED_RATE: Node(FED_RATE, "D", "natural", "slow", 6, "美联储政策利率（%）"),
    CPI_SURPRISE: Node(CPI_SURPRISE, "M", "natural", "slow", 3, "CPI 同比超预期（%）"),
    USD_INDEX: Node(USD_INDEX, "D", "natural", "fast", 0, "美元指数"),
    REAL_YIELD: Node(REAL_YIELD, "D", "natural", "fast", 1, "美国 10 年期实际收益率（%）"),
    VIX: Node(VIX, "D", "natural", "fast", 1, "VIX 波动率指数"),
    GEOPOLITIC_RISK: Node(GEOPOLITIC_RISK, "D", "natural", "fast", 0, "地缘政治风险指数（GPR）"),
    USDCNY: Node(USDCNY, "D", "asset", "fast", 0, "美元兑人民币"),
    XAU_USD: Node(XAU_USD, "D", "natural", "fast", 0, "COMEX 金价（美元/盎司）"),
}

# 只作控制变量、不作为 DAG 节点的共享列
CONTROL_ONLY: tuple[str, ...] = (XAU_USD,)


def lookup(name: str, local: dict[str, Node] | None = None) -> Node | None:
    """先查全局池，再查资产自己的 LOCAL_NODES。"""
    if name in SHARED:
        return SHARED[name]
    if local and name in local:
        return local[name]
    return None


def transmission_of(name: str, local: dict[str, Node] | None = None) -> Transmission:
    n = lookup(name, local)
    return n.transmission if n else "fast"


def max_lag_of(name: str, local: dict[str, Node] | None = None, default: int = 0) -> int:
    n = lookup(name, local)
    return n.max_lag if n else default


def freq_of(name: str, local: dict[str, Node] | None = None, default: str = "D") -> str:
    n = lookup(name, local)
    return n.freq if n else default


def is_shared(name: str) -> bool:
    return name in SHARED


def natural_day_nodes() -> list[str]:
    return [n.name for n in SHARED.values() if n.calendar == "natural"]
