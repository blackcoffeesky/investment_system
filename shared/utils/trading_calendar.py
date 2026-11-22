"""交易日历：全库唯一的"哪几天开市 / 某个频率应该有哪些日期"来源。

2026-11-22 从 `datahub/fin/check.py` 搬到这里（用户要求）：
交易日历不是数据层的私有知识，`shared/` 是它该待的地方——
`datahub` 算缺口要用，将来别的模块要对齐日期也要用。

## 只放日历，不放对齐

**这个模块只回答"哪些日期应该是有的"**，不做 `reindex` / `ffill`。
日期对齐是 `shared/utils/time_align.py` 的事。

（历史上 `shared/utils/calendar.py` 曾经是"日期对齐"模块，2026-11-22 因为
生产代码零引用被删除；这里换了语义和文件名，不叫 `calendar.py` 就是为了不混淆，
也让 `tests/cross/test_import_all.py` 里"旧模块不许回流"的断言继续有效。）

## 市场别名

`pandas_market_calendars` 的日历名不是 ISO 市场代码，所以要映射：

```
CN → SSE（上交所日程；A 股与中金所一致）
US → NYSE
```

拿不到日历（没装该依赖 / 市场没注册）→ **退回工作日**（`bdate_range`）。
退回只保证"不含周末"，不保证"不含节假日"，调用方需要知道这个降级。
"""

from __future__ import annotations

import pandas as pd

#: 市场代码 → `pandas_market_calendars` 的日历名
_MARKET_ALIAS = {"CN": "SSE", "US": "NYSE"}

_PERIOD_RULE = {"W": "W", "M": "ME", "Q": "QE", "Y": "YE"}


def trading_days(start: str, end: str, market: str = "CN") -> pd.DatetimeIndex:
    """某市场的交易日（不含周末与节假日）。

    ⚠️ **全库只有这一个 `trading_days`**：2026-11-22 之前 `check.py` 里存在第二个
    同名函数（返回 `list[str]`），把本函数**覆盖**掉了，于是 `expected_dates`
    按老签名调用变成 `trading_days("CN", ...)` → `DateParseError`
    → `check.run()` 里 17 张日频表全报"日历失败"，逐日缺口检查整体失效。
    **加函数前先 `grep "def <名字>"`。**

    拿不到日历 → 退回工作日（`bdate_range`，只保证不含周末）。
    """
    try:
        import pandas_market_calendars as mcal

        name = _MARKET_ALIAS.get(str(market).upper(), str(market))
        cal = mcal.get_calendar(name)
        sched = cal.schedule(start_date=pd.Timestamp(start), end_date=pd.Timestamp(end))
        return pd.DatetimeIndex(sched.index).normalize()
    except Exception:  # noqa: BLE001
        return pd.bdate_range(start, end)


def expected_periods(freq: str, market: str, start: str, end: str) -> pd.DatetimeIndex:
    """按频率 + 市场推出"应该有哪些日期"。

    - `D`：该市场每个交易日
    - `W` / `M` / `Q` / `Y`：每个周期的**最后一个交易日**
    - `market="NONE"`：不挂交易日历，只用自然周期末

    原名 `expected_dates`（在 `check.py` 里），2026-11-22 改名并搬来此处。
    """
    freq = str(freq).upper()
    no_cal = str(market).upper() == "NONE"
    if no_cal and freq == "D":
        return pd.DatetimeIndex(pd.date_range(start, end, freq="D")).normalize()
    if freq == "D":
        return trading_days(start, end, market=market)
    if no_cal:
        rule = _PERIOD_RULE.get(freq, "ME")
        return pd.DatetimeIndex(pd.date_range(start, end, freq=rule)).normalize()

    days = trading_days(start, end, market=market)
    if len(days) == 0:
        return days
    s = pd.Series(1, index=days)
    if freq == "W":
        last = s.resample("W").apply(lambda x: x.index[-1] if len(x) else pd.NaT).dropna()
        return pd.DatetimeIndex(last).normalize()
    per = {"M": "M", "Q": "Q", "Y": "Y"}.get(freq, "M")
    last = s.groupby(days.to_period(per)).apply(lambda x: x.index[-1]).dropna()
    return pd.DatetimeIndex(last).normalize()


def is_trading_day(day: str, market: str = "CN") -> bool:
    """这天是不是交易日。拿不到日历就退回"工作日"（周六周日一定不是）。"""
    d = trading_days(str(day)[:10], str(day)[:10], market)
    return len(d) == 1


def last_closed_trading_day(now: pd.Timestamp | None = None,
                            market: str = "CN") -> str:
    """**最后一个已经收盘的交易日**（`YYYY-MM-DD`）。

    收盘（15:30）之前 → 上一个交易日（今天的数据还不完整）；
    今天不是交易日（周末/节假日）→ 上一个交易日。
    """
    now = now or pd.Timestamp.now(tz="Asia/Shanghai")
    today = now.strftime("%Y-%m-%d")
    days = trading_days(str((now - pd.Timedelta(days=30)).date()), today, market)
    if len(days) == 0:
        return today
    closed = now.hour * 60 + now.minute >= 15 * 60 + 30
    last = str(days[-1].date())
    # 只有"今天开市、但还没收盘"才退回上一个交易日。
    # ⚠️ 2026-11-22 修：原实现把"不是交易日"也一起退回了 `days[-2]`，
    # 于是周日调用返回周五的**前一天**（11-19），而不是周五（11-20）。
    if last == today and not closed:
        return str(days[-2].date()) if len(days) >= 2 else today
    return last
