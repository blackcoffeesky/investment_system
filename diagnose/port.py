"""diagnose 对外的能力（其他模块只能通过这里访问诊断层）。

目前只暴露"算一次诊断"/"读状态标签"，不暴露模型内部。
causual 将来要拿状态标签，就走 `labels()`。
"""

from __future__ import annotations

import pandas as pd

from diagnose import observations, stats

# 诊断层需要哪些数据表（调用方按此装配 series）
REQUIRED_TABLES = (
    "csi_all", "csi_all_amount", "csi_all_turnover",
    "breadth_adv", "breadth_dec", "breadth_limitup",
    "csi1000", "csi300",
)


class DiagnosisPort:
    def required_tables(self) -> tuple[str, ...]:
        return REQUIRED_TABLES

    def labels(self, asset: str = "ashare") -> pd.Series:
        """现有状态标签（date → label）。将来换成资金结构状态也走这里。"""
        return stats.load_labels(asset)

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


_INSTANCE: DiagnosisPort | None = None


def get_port() -> DiagnosisPort:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = DiagnosisPort()
    return _INSTANCE
