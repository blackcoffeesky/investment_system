"""state_layer 对外的能力（其他模块只能通过这里访问状态层）。

目前只暴露"算一次诊断"/"读状态标签"，不暴露模型内部。
causual 将来要拿状态标签，就走 `labels()`。
"""

from __future__ import annotations

import pandas as pd

from state_layer import inference, observations, stats

# 诊断层需要哪些数据表（调用方按此装配 series）
REQUIRED_TABLES = (
    "csi_all", "csi_all_amount", "csi_all_turnover",
    "breadth_adv", "breadth_dec", "breadth_limitup",
    "csi1000", "csi300",
)


class StatePort:
    def required_tables(self) -> tuple[str, ...]:
        return REQUIRED_TABLES

    def labels(self, asset: str = "ashare") -> pd.Series:
        """状态标签（date → label）。因果层要的就是这个。"""
        return inference.load_labels(asset)

    def states(self, asset: str = "ashare", start: str | None = None,
               end: str | None = None) -> pd.DataFrame:
        """批量输出（date / label / label_name）。"""
        return inference.states(asset, start, end)

    def current_state(self, asset: str = "ashare") -> dict:
        """单点输出，含 model.version 与 state_probabilities。"""
        return inference.current(asset)

    def current(self, asset: str = "ashare") -> dict:
        return stats.current(asset)

    def panel(self, series: dict) -> pd.DataFrame:
        return observations.panel(series)

    def profile(self, asset: str, series: dict) -> pd.DataFrame:
        return stats.profile(asset, observations.panel(series))

    def timeline(self, asset: str, series: dict, days: int = 250) -> list[dict]:
        return stats.timeline(asset, observations.panel(series), days)

    def availability(self, series: dict) -> list[dict]:
        return observations.availability(series)


_INSTANCE: StatePort | None = None


def get_port() -> StatePort:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = StatePort()
    return _INSTANCE
