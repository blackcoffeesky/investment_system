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
from causual.causual_assets.ashare.label import ORDER, RegimeLabel, VALUES

# 资产专用节点的传输特性：决定估计器（slow → 分布滞后，fast → 当期 DML）
LOCAL_NODES: dict[str, Node] = {
    "SHIBOR3M": Node("SHIBOR3M", "D", "asset", "fast", 1, "Shibor 3M"),
    "SOCIAL_FINANCING": Node("SOCIAL_FINANCING", "M", "natural", "slow", 6, "社融存量同比"),
    "PMI": Node("PMI", "M", "natural", "slow", 3, "制造业 PMI"),
    "MARGIN_BALANCE": Node("MARGIN_BALANCE", "D", "asset", "fast", 1, "两融余额"),
    "INSIDER_NET": Node("INSIDER_NET", "M", "natural", "slow", 3, "内部人净增持"),
    "NATIONAL_ETF": Node("NATIONAL_ETF", "D", "asset", "fast", 0, "国家队 ETF 份额"),
    "EQUITY_SUPPLY": Node("EQUITY_SUPPLY", "M", "natural", "slow", 6, "月度净融资额"),
    "AMOUNT_SHOCK": Node("AMOUNT_SHOCK", "D", "asset", "fast", 0, "放量冲击"),
}

ASSET = "ashare"
TITLE = "中证全指因果状态卡"
TARGET = "CSI_ALL"

from regime_engine.extreme import ExtremeParams

# 极端行情判定（资产级覆盖）。abs_cap 是兜底上限：分位在样本极少时会失效。
EXTREME_PARAMS = ExtremeParams(
    ret_col="log_return",
    vol_col="realized_vol_20",
    sigma=3.0,
    pct=0.99,
    exit_days=1,
)

ALL = tuple(l.value for l in ORDER)
# 极端日不走常规 regime（regime_valid=False）
RISK_OFF = (RegimeLabel.TREND_DOWN.value,)
BULL = (RegimeLabel.TREND_UP.value,)

TREATMENT_COL: dict[str, str] = {
    N.FED_RATE: "FED_CHG_BP",
    N.USD_INDEX: "USDX_CHG",
    N.REAL_YIELD: "RY_CHG_BP",
    N.VIX: "VIX_CHG",
    N.USDCNY: "USDCNY_RET",
    N.CPI_SURPRISE: "CPI_SURPRISE",
    "SHIBOR3M": "SHIBOR_CHG_BP",
    "SOCIAL_FINANCING": "SF_YOY_CHG",
    "PMI": "PMI_CHG",
    "MARGIN_BALANCE": "MARGIN_NET",
    "INSIDER_NET": "INSIDER_NET",
    "NATIONAL_ETF": "NATIONAL_ETF_EVENT",
    "EQUITY_SUPPLY": "SUPPLY_Z",
    "AMOUNT_SHOCK": "AMOUNT_SHOCK",
}

SOURCE_COL: dict[str, str] = {
    N.FED_RATE: "FED_RATE",
    N.USD_INDEX: "USDX_CHG",
    N.REAL_YIELD: "RY_CHG_BP",
    N.VIX: "VIX_CHG",
    N.USDCNY: "USDCNY_RET",
    N.CPI_SURPRISE: "CPI_SURPRISE",
    "SHIBOR3M": "SHIBOR_CHG_BP",
    "SOCIAL_FINANCING": "SOCIAL_FINANCING",
    "PMI": "PMI",
    "MARGIN_BALANCE": "MARGIN_NET",
    "INSIDER_NET": "INSIDER_NET",
    "NATIONAL_ETF": "NATIONAL_ETF",
    "EQUITY_SUPPLY": "EQUITY_SUPPLY",
    "AMOUNT_SHOCK": "VOLUME_Z",
}

EDGES: list[Edge] = [
    Edge(source=N.REAL_YIELD, target=TARGET,
         mechanism="全球无风险利率（估值折现）", direction=-1, magnitude=0.70,
         latency="1-2 日", regime_valid=ALL, estimator="causal_forest",
         reading_mode="diff", reading_desc="美债 10Y 实际利率日变化（bp）",
         confounders=(N.FED_RATE, N.USD_INDEX),
         falsifier="内资定价主导时，美债利率对 A 股解释力下降"),
    Edge(source=N.USDCNY, target=TARGET,
         mechanism="汇率→外资流向", direction=-1, magnitude=0.55,
         latency="当日", regime_valid=ALL, estimator=None,   # fast 节点 → 自动用 DML
         reading_mode="return", reading_desc="美元兑人民币日变化（%）",
         # 仅在审计里标注：趋势下行时贬值是利空，震荡市出口链逻辑可能占优
         direction_by_label={
             "trend_down": -1,
             "range": +1, "trend_up": +1, "distribution": -1,
         },
         confounders=(N.USD_INDEX,),
         falsifier="央行逆周期调节，汇率与股市脱钩"),
    Edge(source=N.USD_INDEX, target=TARGET,
         mechanism="美元走强压制新兴市场风险偏好", direction=-1, magnitude=0.45,
         latency="1-2 日", regime_valid=ALL, estimator="causal_forest",
         reading_mode="return", reading_desc="美元指数日变化",
         confounders=(N.FED_RATE,),
         falsifier="人民币同步走强时，美元指数影响被抵消"),
    Edge(source=N.VIX, target=TARGET,
         mechanism="全球风险偏好（恐慌外溢）", direction=-1, magnitude=0.55,
         latency="当日-隔夜", regime_valid=RISK_OFF, estimator="causal_forest",
         reading_mode="diff", reading_desc="VIX 日变化",
         confounders=(N.USD_INDEX,),
         falsifier="国内政策对冲时，外盘恐慌不传导"),
    Edge(source="SHIBOR3M", target=TARGET,
         mechanism="银行间流动性（资金成本）", direction=-1, magnitude=0.65,
         latency="1-2 日", regime_valid=ALL, estimator="causal_forest",
         reading_mode="diff", reading_desc="Shibor 3M 日变化（bp）",
         confounders=(N.REAL_YIELD,),
         falsifier="流动性宽松但信用未扩张，资金淤积在银行间"),
    # PMI 已移除（v5.2）：符号与理论相反、n=159 且不显著。
    # 未来只作为混杂/效应修饰，不再单独成边。见 PROBLEM P25。
    Edge(source="MARGIN_BALANCE", target=TARGET,
         mechanism="两融净流入（杠杆资金）", direction=+1, magnitude=0.45,
         latency="1-2 日", regime_valid=BULL, estimator="causal_forest",
         reading_mode="net_flow", reading_desc="当日融资净买入（亿元）",
         confounders=(N.VIX,), layer="associative",
         falsifier="去杠杆阶段，两融下降反而加速下跌"),
    Edge(source="INSIDER_NET", target=TARGET,
         mechanism="内部人净增持（股东+高管，亿元/月）", direction=+1, magnitude=0.50,
         latency="1-3 月", regime_valid=ALL, estimator=None,   # slow 节点 → 自动用分布滞后
         reading_mode="level_z", reading_desc="净增持金额（亿元/月）",
         confounders=(N.VIX,),
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
         confounders=(N.VIX,), layer="associative",
         falsifier="份额增加来自套利盘而非国家队，后续无支撑"),
    Edge(source="EQUITY_SUPPLY", target=TARGET,
         mechanism="股票供给（月度净融资额）", direction=-1, magnitude=0.50,
         latency="1-6 月", regime_valid=ALL, estimator="distributed_lag",
         reading_mode="level_z", reading_desc="IPO+定增−回购（亿元/月）",
         confounders=("SOCIAL_FINANCING",),
         freq="ME", max_lag=6, detrend=True, split_contemp=True,
         tier="supply", use_in_alpha=False,   # 股票供给 = 供需层，不进 α
         falsifier="融资顺周期：牛市里融资多，同期反而正相关"),
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
