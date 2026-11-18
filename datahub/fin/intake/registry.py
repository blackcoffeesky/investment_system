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


#: 表名 → intake 源标识（一张表只该有一个导入入口）
TABLE_TO_KEY: dict[str, str] = {s.table: s.key for s in SOURCES.values()}


def hint_for(table: str) -> str | None:
    """某张**手工导入表**该怎么更新（给 `update --only` 与新鲜度告警用）。

    返回 `None` 表示不是手工表。返回的字符串是**可直接执行的命令**——
    告警要能被执行才有用，否则用户只能去翻代码。
    """
    key = TABLE_TO_KEY.get(table)
    if key is None:
        return None
    src = SOURCES[key]
    return (f"{src.note}；需手工导入："
            f"python -m datahub.fin.intake {key} <xlsx> --load")
