"""Edge：一条有经济机制支撑的因果路径。

与资产无关——同一套字段在黄金和 A 股上都用，只是**取值不同**。
`latency` 与 `regime_valid` 必须由各资产的 config.py 独立配置：
美联储降息对黄金是"同日-隔夜"，对 A 股可能是"1-2 日"，甚至在某些状态下无效。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Estimator = Literal["causal_forest", "distributed_lag", "event_study"]
ReadingMode = Literal["diff", "level_z", "event", "return"]


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    mechanism: str
    direction: int                          # 理论符号，只用于审计
    magnitude: float                        # 先验权重 [0,1]
    latency: str                            # 人类可读的传导时滞
    regime_valid: tuple[str, ...]           # 机制成立的状态标签（资产自己的枚举值）
    estimator: Estimator | None             # None = 由节点的 transmission 自动决定
    reading_mode: ReadingMode
    reading_desc: str
    confounders: tuple[str, ...] = ()
    effect_modifiers: tuple[str, ...] = ("regime",)
    falsifier: str = ""
    layer: str = "causal"                   # causal / associative
    # 时间尺度层：structure（制度/政策，定性）/ supply（供需与价值）/ signal（日周频）
    # 只有 tier="signal" 的边进 α，另两层是长期背景
    tier: str = "signal"
    # 方向不定：关联型边（如放量）不判"方向冲突"，只如实报告估计符号
    direction_unclear: bool = False
    freq: str | None = None                 # None = 跟随源节点的频率
    max_lag: int = 6
    # 分组变量：None=按时间尺度自动（慢变量→长期状态，快变量→日频 regime）；
    # "none"=不分层（treatment 若在长期状态下游，分层属过度控制）
    group_by: str | None = None
    detrend: bool = False
    # 把 lag0（同期）与 lag1..max 分开报告：同期项混着反向因果，只应作诊断；进 α 的只用滞后部分。见 CHANGES v5.1。
    split_contemp: bool = False
    use_in_alpha: bool = True
    event_threshold: float = 2.0
    event_window: tuple[int, int] = (-1, 1)
    event_sign: int = 0
    # True 表示处理变量本身已是 0/1 事件标记，不再做 z 标准化
    event_from_flag: bool = False
    direction_by_label: dict[str, int] = field(default_factory=dict)

    def resolve_estimator(self, local=None) -> str:
        """显式指定优先；否则按节点的 transmission 自动决定。"""
        if self.estimator:
            return self.estimator
        from causual.causual_core import nodes as N

        return "distributed_lag" if N.transmission_of(self.source, local) == "slow" else "causal_forest"

    def lag_span(self, local=None) -> int:
        """该边实际需要几阶滞后（快变量为 0）。"""
        if self.estimator == "distributed_lag":
            return self.max_lag
        from causual.causual_core import nodes as N

        return N.max_lag_of(self.source, local, 0)

    def resolved_freq(self, local=None) -> str:
        """边自己的频率；没显式指定就跟随源节点（日频节点不会被当成月频）。"""
        if self.freq:
            return str(self.freq)
        from causual.causual_core import nodes as N

        f = N.freq_of(self.source, local, "D")
        return "ME" if str(f).upper() == "M" else str(f)

    def resolved_group_by(self) -> str:
        """该边用哪个分组变量：显式设置优先，否则按时间尺度推。"""
        if self.group_by:
            return str(self.group_by).lower()
        return "long" if self.resolved_freq() != "D" else "daily"

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.target)

    def dir_for(self, label) -> int:
        """该状态下的理论方向；未单列则回退到全局 direction。"""
        return int(self.direction_by_label.get(str(label), self.direction))

    def __str__(self) -> str:
        return f"{self.source}->{self.target}"


def index(edges: list[Edge]) -> dict[tuple[str, str], Edge]:
    return {e.key: e for e in edges}


def validate(edges: list[Edge], target: str, allowed_labels: tuple[str, ...]) -> None:
    """装配期自检：标签拼错会静默失效，必须在这里拦住。"""
    allowed = set(allowed_labels)
    for e in edges:
        for lbl in e.regime_valid:
            if lbl not in allowed:
                raise ValueError(f"{e} 的 regime_valid 含未知标签 {lbl!r}；可选：{sorted(allowed)}")
        for lbl in e.direction_by_label:
            if lbl not in allowed:
                raise ValueError(f"{e} 的 direction_by_label 含未知标签 {lbl!r}")
        # 上游边的 target 不是资产主标的（如 USD_INDEX→USDCNY），所以不校验 target
