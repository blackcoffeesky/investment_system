"""A 股（中证全指 000985）的因果图。

设计原则（与用户讨论后确定）
--------------------------
1. **只把相对外生的变量当因果边**：利率、汇率、美债、股票供给。
   成交量/换手/两融/风格比价这些内生于价格的，只做状态与修饰，不做因果边。
2. **状态枚举是 A 股自己的一套**，与黄金不共享。
3. `latency` 与 `regime_valid` 必须独立配置：同样是 FED_RATE，黄金是"1-3 月"，
   A 股是"1-2 日"。
"""

from __future__ import annotations

from causual.causual_core import nodes as N
from causual.causual_core.edge import Edge
from causual.causual_core.nodes import Node

# 资产专用节点的传输特性：决定估计器（slow → 分布滞后，fast → 当期 DML）
LOCAL_NODES: dict[str, Node] = {
    "SHIBOR3M": Node("SHIBOR3M", "D", "asset", "fast", 1, "Shibor 3M"),
    "PMI": Node("PMI", "M", "natural", "slow", 3, "制造业 PMI"),
    "MARGIN_BALANCE": Node("MARGIN_BALANCE", "D", "asset", "fast", 1, "两融余额"),
    "INSIDER_NET": Node("INSIDER_NET", "M", "natural", "slow", 3, "内部人净增持"),
    "NATIONAL_ETF": Node("NATIONAL_ETF", "D", "asset", "fast", 0, "国家队 ETF 份额"),
    "EQUITY_SUPPLY": Node("EQUITY_SUPPLY", "M", "natural", "slow", 6, "月度净融资额"),
    "AMOUNT_SHOCK": Node("AMOUNT_SHOCK", "D", "asset", "fast", 0, "放量冲击"),
}
# 已删：`SOCIAL_FINANCING`（v5.44）。它没做过任何边的 source，只当
# `EQUITY_SUPPLY` 的 confounder，而那一列是恒 NaN 的桩——见 EDGES 里那条注释。
# 社融数据接入时，节点 + `SF_YOY_CHG` 一整套再加回来。

ASSET = "ashare"
TITLE = "中证全指因果状态卡"
TARGET = "CSI_ALL"



# 状态标签：因果层只用到这几个字符串做**分组**（哪些边在什么行情下有效）。
# 完整词表在状态层；这里不 import 它，靠 test_label_vocab_matches_state 守着一致。
#
# 2026-11-22：A 股的状态模型是 K=3（`short_price` 组），**没有 `distribution`**——
# 那是黄金 K=4 才有的标签。这里跟着收成三个；`ALL` 因此也不会再含不存在的行情。
RANGE = "range"
TREND_UP = "trend_up"
TREND_DOWN = "trend_down"
ORDER = (RANGE, TREND_UP, TREND_DOWN)
ALL = ORDER
# 极端日不走常规 regime（regime_valid=False）
RISK_OFF = (TREND_DOWN,)
BULL = (TREND_UP,)

TREATMENT_COL: dict[str, str] = {
    N.FED_RATE: "FED_CHG_BP",
    N.USD_INDEX: "USDX_CHG",
    N.REAL_YIELD: "RY_CHG_BP",
    N.VIX: "VIX_CHG",
    N.USDCNY: "USDCNY_RET",
    "SHIBOR3M": "SHIBOR_CHG_BP",
    "PMI": "PMI_CHG",
    "MARGIN_BALANCE": "MARGIN_NET",
    "INSIDER_NET": "insider_net",
    "NATIONAL_ETF": "national_etf_event",
    "EQUITY_SUPPLY": "SUPPLY_Z",
    "AMOUNT_SHOCK": "AMOUNT_SHOCK",
}

SOURCE_COL: dict[str, str] = {
    N.FED_RATE: "fed_rate",
    N.USD_INDEX: "USDX_CHG",
    N.REAL_YIELD: "RY_CHG_BP",
    N.VIX: "VIX_CHG",
    N.USDCNY: "USDCNY_RET",
    "SHIBOR3M": "SHIBOR_CHG_BP",
    "PMI": "pmi",
    "MARGIN_BALANCE": "MARGIN_NET",
    "INSIDER_NET": "insider_net",
    "NATIONAL_ETF": "national_etf",
    "EQUITY_SUPPLY": "equity_fin_monthly",
    "AMOUNT_SHOCK": "VOLUME_Z",
}
# 已删的两条（v5.44，宁缺毋滥）：
#   `N.CPI_SURPRISE` —— A 股面板里根本没有这一列，也没有任何边用它。
#                      留着就是"配置指着一个不存在的名字"，最坏情况是有人
#                      照着它写新边，然后在估计里静默 continue。
#   `SOCIAL_FINANCING` —— 桩列，见 EDGES 里 EQUITY_SUPPLY 那段注释。
# `PMI` 这两条**保留**：PMI 列有数据、供需层还在用，只是当前没有 PMI 边
# （v5.2 删边时留的话：将来可能作混杂/效应修饰）。

EDGES: list[Edge] = [
    Edge(source=N.REAL_YIELD, target=TARGET,
         mechanism="全球无风险利率（估值折现）", direction=-1, magnitude=0.70,
         latency="1-2 日", regime_valid=ALL, estimator="causal_forest",
         reading_mode="diff", reading_desc="美债 10Y 实际利率日变化（bp）",
         confounders=("fed_rate", "usd_index"),
         falsifier="内资定价主导时，美债利率对 A 股解释力下降"),
    Edge(source=N.USDCNY, target=TARGET,
         mechanism="汇率→外资流向", direction=-1, magnitude=0.55,
         latency="当日", regime_valid=ALL, estimator=None,   # fast 节点 → 自动用 DML
         reading_mode="return", reading_desc="美元兑人民币日变化（%）",
         # 仅在审计里标注：趋势下行时贬值是利空，震荡市出口链逻辑可能占优
         direction_by_label={
             "trend_down": -1,
             "range": +1, "trend_up": +1,
         },
         confounders=("usd_index",),
         falsifier="央行逆周期调节，汇率与股市脱钩"),
    Edge(source=N.USD_INDEX, target=TARGET,
         mechanism="美元走强压制新兴市场风险偏好", direction=-1, magnitude=0.45,
         latency="1-2 日", regime_valid=ALL, estimator="causal_forest",
         reading_mode="return", reading_desc="美元指数日变化",
         confounders=("fed_rate",),
         falsifier="人民币同步走强时，美元指数影响被抵消"),
    Edge(source=N.VIX, target=TARGET,
         mechanism="全球风险偏好（恐慌外溢）", direction=-1, magnitude=0.55,
         latency="当日-隔夜", regime_valid=RISK_OFF, estimator="causal_forest",
         reading_mode="diff", reading_desc="VIX 日变化",
         confounders=("usd_index",),
         falsifier="国内政策对冲时，外盘恐慌不传导"),
    Edge(source="SHIBOR3M", target=TARGET,
         mechanism="银行间流动性（资金成本）", direction=-1, magnitude=0.65,
         latency="1-2 日", regime_valid=ALL, estimator="causal_forest",
         reading_mode="diff", reading_desc="Shibor 3M 日变化（bp）",
         confounders=("real_yield_10y",),
         falsifier="流动性宽松但信用未扩张，资金淤积在银行间"),
    # PMI 已移除（v5.2）：符号与理论相反、n=159 且不显著。
    # 未来只作为混杂/效应修饰，不再单独成边。见 PROBLEM P25。
    Edge(source="MARGIN_BALANCE", target=TARGET,
         mechanism="两融净流入（杠杆资金）", direction=+1, magnitude=0.45,
         latency="1-2 日", regime_valid=BULL, estimator="causal_forest",
         reading_mode="net_flow", reading_desc="当日融资净买入（亿元）",
         confounders=("vix",), layer="associative",
         falsifier="去杠杆阶段，两融下降反而加速下跌"),
    Edge(source="INSIDER_NET", target=TARGET,
         mechanism="内部人净增持（股东+高管，亿元/月）", direction=+1, magnitude=0.50,
         latency="1-3 月", regime_valid=ALL, estimator=None,   # slow 节点 → 自动用分布滞后
         reading_mode="level_z", reading_desc="净增持金额（亿元/月）",
         confounders=("vix",),
         freq="ME", max_lag=3, split_contemp=True,
         tier="supply", use_in_alpha=False,   # 大股东态度 = 供需层，不进 α
         falsifier="增持是护盘行为，同期正相关但后续无支撑"),
    # SIZE_FLOW 已移除（v5.4）：份额差测的是"资金分歧/避险偏好"，不是涨跌方向，
    # 改作 HMM 特征 ETF_DISPERSION，不再当因果边。
    Edge(source="NATIONAL_ETF", target=TARGET,
         mechanism="国家队增持（300ETF+50ETF 份额单日超 60 日 95 分位）", direction=+1, magnitude=0.45,
         latency="当日-5 日", regime_valid=ALL, estimator="event_study",
         reading_mode="event", reading_desc="份额变动 > 滚动 60 日 95% 分位",
         event_from_flag=True, event_window=(-1, 5),
         confounders=("vix",), layer="associative",
         falsifier="份额增加来自套利盘而非国家队，后续无支撑"),
    # 混杂 `SOCIAL_FINANCING`（社融存量同比）**已删除**（v5.44）。
    # 数据层没有社融总量序列，那一列是恒 NaN 的桩；而两个估计器都会**静默跳过**
    # 全 NaN/零方差的控制变量（`estimators.fit_dml_edge` 的 `notna().any()`、
    # `fit_distributed_lag` 的 `sd 有限`），于是配置上写着"控制了社融"、
    # 实际根本没控——这比不写更糟，因为没人会去查。
    # 社融数据接入后再加回来（连同 `SF_YOY_CHG` 与 `SOCIAL_FINANCING` 节点）。
    Edge(source="EQUITY_SUPPLY", target=TARGET,
         mechanism="股票供给（月度净融资额）", direction=-1, magnitude=0.50,
         latency="1-6 月", regime_valid=ALL, estimator="distributed_lag",
         reading_mode="level_z", reading_desc="IPO+定增−回购（亿元/月）",
         confounders=(),
         freq="ME", max_lag=6, detrend=True, split_contemp=True,
         tier="supply", use_in_alpha=False,   # 股票供给 = 供需层，不进 α
         falsifier="融资顺周期：牛市里融资多，同期反而正相关；"
                   "社融总量数据未接入，本轮未控制该混杂"),
    Edge(source="AMOUNT_SHOCK", target=TARGET,
         mechanism="放量冲击（z>3）", direction=-1, magnitude=0.30,
         latency="1-5 日", regime_valid=ALL, estimator="event_study",
         reading_mode="event", reading_desc="成交量 60 日 z 值",
         event_threshold=3.0, event_window=(1, 5), event_sign=+1,
         layer="associative",
         # 放量多伴随大涨，direction=-1 不成立；关联型边只报符号，不判"方向冲突"
         direction_unclear=True,
         falsifier="放量突破时，高换手是确认而非反转"),
]

# 只被派生列用到、不在映射里的表
EXTRA_TABLES = ("etf_shares",)

# 面板里**已知没有数据**的列：{列名: 原因}。
#
# 写在这里 = "我知道它是空的，并且打算接数据"。**没写在这里却整列 NaN，测试当场红**
# （`tests/causual/test_causual_dead_columns.py`）。
# 数据接上之后**必须删掉对应条目**——测试反向检查这件事（声明了却已经有值 → 红），
# 否则这张表会烂成"永久豁免清单"，跟没有测试一样。
#
# 2026-11-15：`EQUITY_SUPPLY` 接上了真数据（面板改成映射 `equity_fin_monthly`），
# 两条声明**当场过期**，测试反向检查立刻抓到 → 已清空。
# 这个 dict 现在是空的，但**留着机制**：下一个没接的列必须在这里登记。
NOT_CONNECTED: dict[str, str] = {}
