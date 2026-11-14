"""数据源的基础件：`Source` 契约 + 跨源共享的小工具。

为什么单独一层：`Source` 被几个取数模块引用，放 `__init__` 会绕成循环 import；
`_clip` / `_num` / `_with_prefix` / `_existing_keys` 是各源共用的清洗，也只该有一份。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd


@dataclass(frozen=True)
class Source:
    table: str
    func: Callable
    params: dict = field(default_factory=dict)
    kind: str = "auto"        # auto / manual
    shape: str = "auto"       # auto / long / static（决定写库方式）
    enabled: bool = True
    note: str = ""


def _clip(s: pd.Series, start: str, end: str) -> pd.Series:
    s = s.dropna()
    return s.loc[(s.index >= pd.Timestamp(start)) & (s.index <= pd.Timestamp(end))]


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _with_prefix(code: object) -> str:
    """裸 6 位 → 带市场前缀；已带前缀的原样返回。

    全库统一口径（2026-11-14）：`92xxxx / 8xxxxx / 4xxxxx` → `bj`，
    `6xxxxx / 5xxxxx`（含 ETF）→ `sh`，其余（`0/1/3` 开头）→ `sz`。
    """
    c = str(code).strip()
    if c[:2] in ("sh", "sz", "bj"):
        return c
    if c.startswith(("92", "8", "4")):
        return "bj" + c
    if c.startswith(("6", "5")):
        return "sh" + c
    return "sz" + c


def _read_cached(table: str) -> pd.DataFrame:
    """读库里已有的表（没有/读失败就返回空表）。"只补缺的"各源共用。

    返回 **long** 时是 `(code,date,…)` 的 DataFrame（`date` 是列），
    `multi` 时是 `index=date` 的 DataFrame——`_existing_keys` 两种都能吃。
    """
    try:
        import datahub.fin as fin
        if table not in fin.tables():
            return pd.DataFrame()
        df = fin.read(table)
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()
    except Exception:  # noqa: BLE001
        return pd.DataFrame()


def _existing_keys(table: str, key_of: Callable[[Any], Any]) -> set:
    """库里这张表**已经有哪些 key**（"只补缺的"缓存逻辑，各源共用）。

    `key_of` 把一行的日期转成 key：`etf_shares` 用日期字符串，`dividend_annual` 用年份。
    long / multi 两种表都处理；表不存在或读失败就当没有。
    """
    df = _read_cached(table)
    if df.empty:
        return set()
    dates = df["date"] if "date" in df.columns else df.index
    return {key_of(x) for x in dates}
