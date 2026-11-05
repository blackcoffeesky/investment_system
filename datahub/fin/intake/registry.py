"""手工数据源注册表：源标识 → (parser, 目标表, 频率)。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from datahub.fin.intake.parsers.wgc_etf import parse as parse_wgc_etf
from datahub.fin.intake.parsers.wgc_holdings import parse as parse_wgc


@dataclass(frozen=True)
class IntakeSource:
    key: str                # CLI 用的标识
    parser: Callable        # (raw 路径) -> pd.Series / pd.DataFrame
    table: str              # 目标表
    freq: str               # D / W / M / Q / Y
    note: str = ""


SOURCES: dict[str, IntakeSource] = {
    s.key: s for s in [
        IntakeSource("wgc", parse_wgc, "cb_holdings", "Q",
                     note="WGC 季报：87 国篮子合计持仓（吨）；增持=差分"),
        IntakeSource("wgc_etf", parse_wgc_etf, "gold_etf_holdings", "M",
                     note="WGC ETF 月报：全球黄金 ETF 持仓（吨）；净流入=差分"),
    ]
}
