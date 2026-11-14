"""数据新鲜度：这个资产用到的表，是不是都已经更新到"应该有的最新一期"。

为什么单独一层
--------------
卡片日期原先直接取 `csi_all` 的最后一天，但信号层用的是 VIX、汇率、两融——
它们可能比指数晚一天。只看指数会让人误以为"整张卡都是新的"。

判定方法
--------
对每张表，用 `metadata.freq` + `lag_days` 推算**期望的最新期**：

    期望期 = 最近一个已结束的周期（日=上一交易日，月=上月末，季=上季末，年=上年末）
    可用日 = 期望期 + lag_days
    若 今天 >= 可用日 且 实际 end_date < 期望期  →  过期

只对 **D / M / Q / Y** 严格；`W`（COT 按周发布）与 `SPARSE`（不定期）的
"该有了"没有定义，**有意不判**——不是漏掉。

⚠ WATCH / WATCH_MACRO 里的名字**必须真实存在于 `DataPort.tables()`**，
否则 `check()` 里 `if t not in idx.index: continue` 会静默跳过整张表：
监控清单漂移 = 监控失效。`tests/test_freshness_watchlist.py` 守着这件事。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import pandas as pd

# 日频：卡片/信号真正依赖的表
WATCH = (
    "csi_all", "csi300", "csi1000", "gold_cny",
    "vix", "usdcny", "usd_index", "fed_rate", "real_yield_10y",
    "shibor3m", "margin_balance",
)

# 月/季频：宏观层（月频卡 + 供需层）依赖的表。
#
# 历史坑（2026-11-14 修）：这里原先写的是 `cn_cpi / cn_ppi / cn_m2 / cn_exports /
# cn_social_fin`——**数据层的表名早就改了**（`cpi_yoy / ppi_yoy / m1_yoy …`），
# 于是 8 个名字只命中 2 个，宏观表过期再也不会被报出来。名字必须与数据层一致，
# 而且只能放 `_period_end()` 认得的 freq（D/M/Q/Y）。
#
# 这**一份**清单供两处用：
#   1. `WATCH_MACRO`：宏观表新鲜度
#   2. `pipeline._month_of()`：月频卡的归档月（不能含年频，否则会把月份拖回上一年）
MONTH_TABLES = (
    # ---- A 股：供需层直接读的宏观表
    "cpi_yoy", "ppi_yoy", "pmi",
    "m1_level", "m1_yoy", "m2_level", "m2_yoy",
    "retail_sales", "retail_sales_mom",
    "industrial_yoy", "exports_yoy",
    "equity_fin_monthly", "insider_net",
    "total_mktcap", "total_shares",
    # ---- 黄金：季/月频供需
    "cb_holdings", "gold_etf_holdings", "gold_cpi_surprise",
)

# 年频另放：`_period_end("Y")` 判得了过期，但归档月不能用它。
#
# `dividend_annual` 已从这里删除（v5.42）：causual 不再读那张表，
# 股息率改由 `DataPort.dividend_yield()` 现算。数据层把它删了也不会有人疼。
WATCH_MACRO = MONTH_TABLES


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
    """最近一个"已结束的周期"的期末；不判过期的频率返回 None。"""
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
    if freq == "Y":
        # 年频：上年末才算"已结束的年度"。
        # 早前这里没这一支 → 返回 None → 年频表永远被跳过，等于没监控。
        return pd.Timestamp(year=today.year - 1, month=12, day=31)
    # 其余（W 周频、SPARSE 不定期）有意不判：见模块 docstring
    return None


def scoped(tables, required) -> tuple[str, ...]:
    """把全局清单收窄到**本资产真正用到**的表。

    `metadata()` 是全局的（两个资产共用一张表），所以 `WATCH_MACRO` 里既列了
    A 股的 CPI/PPI，也列了黄金的 `cb_holdings`。不收窄的话，A 股宏观卡会报
    「gold_cpi_surprise 停在 …」这种跟自己无关的告警。

    收窄后为空（资产包没声明 REQUIRED_TABLES）就退回全清单——
    宁可按全清单报，也不要悄悄不监控。
    """
    want = set(required)
    keep = tuple(t for t in tables if t in want)
    return keep or tuple(tables)


def check(meta: pd.DataFrame, tables: tuple[str, ...] = WATCH,
          today: pd.Timestamp | None = None) -> list[Freshness]:
    """返回所有"该有数据但没到"的表。

    监控清单里的名字在 `metadata` 里找不到时**打印告警**而不是静默跳过——
    2026-11 那次就是清单写着一批已改名的表，`continue` 之后整个宏观层
    假装"没有过期表"。
    """
    if meta is None or not len(meta):
        return []
    today = (today or pd.Timestamp.today()).normalize()
    idx = meta.set_index("name")

    out: list[Freshness] = []
    for t in tables:
        if t not in idx.index:
            print(f"[新鲜度] 监控清单里的 {t!r} 不在 metadata 里（改名/删表了？"
                  f"这张表的新鲜度没人看）", file=sys.stderr)
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
