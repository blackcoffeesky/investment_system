"""黄金的因果图：11 条边。

与 A 股**不共享任何 Edge 实例**——同一个 `FED_RATE->金价` 与 `FED_RATE->沪深300`
的 latency、regime_valid 都不同，必须各写各的。
"""

from __future__ import annotations

from causual.causual_core import nodes as N
from causual.causual_core.edge import Edge

ASSET = "gold"
TITLE = "黄金因果状态卡"
TARGET = "GOLD_CNY"

# 状态标签：因果层只用到这几个字符串做**分组**（哪些边在什么行情下有效）。
# 完整词表在状态层；这里不 import 它，靠 test_label_vocab_matches_state 守着一致。
RANGE = "range"
TREND_UP = "trend_up"
TREND_DOWN = "trend_down"
DISTRIBUTION = "distribution"
ORDER = (RANGE, TREND_UP, TREND_DOWN, DISTRIBUTION)
ALL = ORDER
# 极端日不走常规 regime（regime_valid=False），所以这里只有四个常规标签。
# 原“只在 stress 有效”的 GEO/VIX 两条边改为全常规有效——stress 已交给极端规则。
STRESS = ORDER

# 列名映射：DAG 节点 -> 面板列
TREATMENT_COL: dict[str, str] = {
    "FED_RATE": "FED_CHG_BP",
    "CPI_SURPRISE": "CPI_SURPRISE",
    "USD_INDEX": "USDX_CHG",
    "REAL_YIELD_10Y": "RY_CHG_BP",
    "VIX": "VIX",
    "USDCNY": "USDCNY_RET",
    "GEOPOLITIC_RISK": "GEOPOLITIC_RISK",
    "CB_GOLD_BUY": "CB_GOLD_BUY",
    "ETF_FLOW": "ETF_FLOW",
    "OPEN_INTEREST": "OI_CHG_PCT",
    "COT_NET_LONG": "COT_NET_LONG",
}

SOURCE_COL: dict[str, str] = {
    "FED_RATE": "FED_RATE",
    "CPI_SURPRISE": "CPI_SURPRISE",
    "USD_INDEX": "USD_INDEX",
    "REAL_YIELD_10Y": "REAL_YIELD_10Y",
    "VIX": "VIX",
    "USDCNY": "USDCNY",
    "GEOPOLITIC_RISK": "GEOPOLITIC_RISK",
    "CB_GOLD_BUY": "CB_GOLD_BUY",
    "ETF_FLOW": "ETF_FLOW",
    "OPEN_INTEREST": "OPEN_INTEREST",
    "COT_NET_LONG": "COT_NET_LONG",
}

EDGES: list[Edge] = [
    Edge(source=N.FED_RATE, target=TARGET,
         mechanism="政策利率推高实际收益率", direction=+1, magnitude=0.85,
         latency="1-3 月", regime_valid=ALL, estimator="distributed_lag",
         reading_mode="diff", reading_desc="联邦基金利率变化",
         freq="ME", max_lag=6,
         falsifier="降息周期中金价不涨（避险需求主导）"),
    Edge(source=N.CPI_SURPRISE, target=TARGET,
         mechanism="通胀超预期", direction=+1, magnitude=0.50,
         latency="1 月", regime_valid=ALL, estimator="distributed_lag",
         reading_mode="diff", reading_desc="CPI 同比超预期",
         freq="ME", max_lag=4,
         falsifier="通胀上行但实际利率同步上行"),
    Edge(source=N.REAL_YIELD, target=TARGET,
         mechanism="持有黄金的机会成本", direction=-1, magnitude=0.85,
         latency="同日", regime_valid=ALL, estimator="causal_forest",
         reading_mode="diff", reading_desc="10 年期实际收益率日变化（bp）",
         confounders=(N.USD_INDEX,), falsifier="实际利率上行但金价同涨（去美元化买盘）"),
    Edge(source=N.USD_INDEX, target=N.USDCNY,
         mechanism="美元向人民币传导", direction=+1, magnitude=0.70,
         latency="同日", regime_valid=ALL, estimator="causal_forest",
         reading_mode="return", reading_desc="美元指数日变化",
         confounders=(N.FED_RATE,), falsifier="人民币逆周期调节，汇率与美元脱钩"),
    Edge(source=N.USDCNY, target=TARGET,
         mechanism="人民币计价渠道", direction=+1, magnitude=0.80,
         latency="同日-隔夜", regime_valid=ALL, estimator="causal_forest",
         reading_mode="return", reading_desc="美元兑人民币日变化（%）",
         confounders=(N.XAU_USD, N.USD_INDEX),
         falsifier="贬值期间金价下跌（内外价差收敛）"),
    Edge(source=N.GEOPOLITIC_RISK, target=TARGET,
         mechanism="避险资本流", direction=+1, magnitude=0.35,
         latency="即时-3 日", regime_valid=STRESS, estimator="event_study",
         reading_mode="event", reading_desc="地缘风险指数跳升",
         confounders=(N.VIX,), falsifier="风险事件钝化，市场已免疫"),
    Edge(source=N.VIX, target=TARGET,
         mechanism="避险需求", direction=+1, magnitude=0.40,
         latency="即时", regime_valid=STRESS, estimator="causal_forest",
         reading_mode="diff", reading_desc="VIX 日变化",
         confounders=(N.USD_INDEX,), falsifier="流动性危机中黄金被抛售换现金"),
    Edge(source="CB_GOLD_BUY", target=TARGET,
         mechanism="央行购金的结构性需求", direction=+1, magnitude=0.55,
         latency="1-2 月", regime_valid=ALL, estimator="distributed_lag",
         reading_mode="level_z", reading_desc="主要央行月度净购金（吨/月）",
         confounders=(N.REAL_YIELD, N.USDCNY, "CB_HOLDINGS"),
         freq="ME", max_lag=6,
         tier="supply", use_in_alpha=False,   # 央行购金 = 供需层（长期），不进 α
         falsifier="购金量创高但金价滞涨"),
    Edge(source="ETF_FLOW", target=TARGET,
         mechanism="投资需求：黄金 ETF 净流入", direction=+1, magnitude=0.50,
         latency="1 月", regime_valid=ALL, estimator="distributed_lag",
         reading_mode="level_z", reading_desc="全球黄金 ETF 净流量（吨/月）",
         confounders=(N.USDCNY, N.XAU_USD),
         layer="associative", freq="ME", max_lag=6, use_in_alpha=False,
         falsifier="ETF 流入但金价不涨"),
    Edge(source="OPEN_INTEREST", target=TARGET,
         mechanism="沪金持仓拥挤度", direction=-1, magnitude=0.25,
         direction_by_label={"trend_up": +1,
                             "range": -1, "distribution": -1,
                             "trend_down": -1},
         latency="同日-2 日", regime_valid=ALL, estimator="causal_forest",
         reading_mode="diff", reading_desc="沪金持仓量日变化（%）",
         confounders=(N.USDCNY, N.XAU_USD), layer="associative",
         falsifier="增仓伴随趋势加速"),
    Edge(source="COT_NET_LONG", target=TARGET,
         mechanism="CFTC 非商业净多头", direction=-1, magnitude=0.35,
         direction_by_label={"trend_up": +1,
                             "range": -1, "distribution": -1,
                             "trend_down": -1},
         latency="1-2 周", regime_valid=ALL, estimator="causal_forest",
         reading_mode="level_z", reading_desc="非商业净多头占持仓比（%）",
         confounders=(N.XAU_USD, N.USD_INDEX), layer="associative",
         falsifier="趋势行情中净多头持续走高"),
]

# 只被派生列用到、不在映射里的表
EXTRA_TABLES = ("gold_etf_holdings", "cb_holdings")
