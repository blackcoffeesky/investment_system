"""数据新鲜度：这个资产用到的表，是不是都已经更新到"应该有的最新一期"。

为什么单独一层
--------------
卡片日期原先直接取 `csi_all` 的最后一天，但信号层用的是 VIX、汇率、两融——
它们可能比指数晚一天。只看指数会让人误以为"整张卡都是新的"。

判定方法
--------
对每张表，用 `metadata.freq` + `lag_days` 推算**期望的最新期**：

    期望期 = 最近一个已结束的周期（日=上一交易日，月=上月末，季=上季末）
    可用日 = 期望期 + lag_days
    若 今天 >= 可用日 且 实际 end_date < 期望期  →  过期

只对**日频**严格（差一个交易日就算过期）；月/季频按 lag 放宽；年频不检查。
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# 日频：卡片/信号真正依赖的表
WATCH = (
    "csi_all", "csi300", "csi1000", "gold_cny",
    "vix", "usdcny", "usd_index", "fed_rate", "real_yield_10y",
    "shibor3m", "margin_balance",
)

# 月/季频：宏观层依赖的表
WATCH_MACRO = (
    "cn_cpi", "cn_ppi", "cn_m2", "cn_exports", "cn_social_fin",
    "cb_holdings", "gold_etf_holdings", "dividend_annual",
)


@dataclass
class Freshness:
    table: str
    freq: str
    expect: str            # 期望的最新期
    actual: str            # 实际的 end_date
    lag_days: int
    stale: bool
    days_behind: int

    @property
    def text(self) -> str:
        if not self.stale:
            return ""
        return f"{self.table} 停在 {self.actual}，期望至少 {self.expect}"


def _period_end(freq: str, today: pd.Timestamp) -> pd.Timestamp | None:
    if freq == "D":
        # 上一交易日：用工作日近似（周末不判过期）
        d = today
        while d.weekday() >= 5:
            d -= pd.Timedelta(days=1)
        return d
    if freq == "M":
        return (today.replace(day=1) - pd.Timedelta(days=1)).normalize()
    if freq == "Q":
        q_end = today.to_period("Q").start_time - pd.Timedelta(days=1)
        return pd.Timestamp(q_end).normalize()
    return None


def check(meta: pd.DataFrame, tables: tuple[str, ...] = WATCH,
          today: pd.Timestamp | None = None) -> list[Freshness]:
    """返回所有"该有数据但没到"的表。"""
    if meta is None or not len(meta):
        return []
    today = (today or pd.Timestamp.today()).normalize()
    idx = meta.set_index("name")

    out: list[Freshness] = []
    for t in tables:
        if t not in idx.index:
            continue
        row = idx.loc[t]
        freq = str(row.get("freq") or "D")
        lag = int(row.get("lag_days") or 0)
        actual_s = row.get("end_date")
        if not actual_s:
            continue
        actual = pd.Timestamp(actual_s)
        expect = _period_end(freq, today)
        if expect is None:
            continue

        # 还没到"该发布"的时间，不算过期
        if today < expect + pd.Timedelta(days=lag):
            continue

        stale = actual < expect
        out.append(Freshness(
            table=t, freq=freq,
            expect=str(expect.date()), actual=str(actual.date()),
            lag_days=lag, stale=stale,
            days_behind=max(0, (expect - actual).days),
        ))
    return out


def stale_only(meta: pd.DataFrame, tables: tuple[str, ...] = WATCH,
               today: pd.Timestamp | None = None) -> list[Freshness]:
    return [f for f in check(meta, tables, today) if f.stale]


def summary(items: list[Freshness], limit: int = 4) -> str:
    if not items:
        return ""
    head = "；".join(f.text for f in sorted(items, key=lambda x: -x.days_behind)[:limit])
    if len(items) > limit:
        head += f"；等 {len(items)} 张表"
    return head
