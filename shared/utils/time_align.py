"""时间对齐：lag 推后、日频对齐、低频聚合。**纯工具**，不读库、不碰文件。

三条约定（2026-11-15 定，取代旧的 `panel_align.build` 默认 ffill）
-----------------------------------------------------------------
1. **`raw(s, lag)` 原样返回**：只按公布滞后推后日期，不做重采样——低频还是低频。
2. **`to_daily(s, idx, ffill=False)` 才对齐到日频**，且 `ffill` **默认 False**：
   要不要把低频值铺满每一天是**下游的决定**，不是数据层的默认行为。
3. **`build(..., policy=)` 默认 `"raw"`**：没声明策略的列就是原始频率。
   忘记声明会表现为"这列没对齐（非观测日为 NaN）"并被测试抓住，而不是被悄悄 ffill。

`raw` 列怎么放进日频面板
------------------------
观测日**不在交易日历**上时（月末/季末经常是周末：实测 33%~60%），
把它落到**之后第一个交易日**——那是这条数据"可见"的第一天，语义上等价于
"发布后可见"，且**一个观测只占一行、不向其他交易日复制**。这不是 ffill。
（若直接 `reindex(idx)` 会丢掉一半观测，是本轮实测发现的坑。）
"""

from __future__ import annotations

import sys
from typing import Any, Mapping

import pandas as pd

# OHLC 的默认聚合口径（`to_low_freq` 用）
_OHLC_HOW = {
    "open": "first", "high": "max", "low": "min", "close": "last",
    "volume": "sum", "amount": "sum", "turnover": "sum",
}

_FREQ = {"W": "W", "M": "ME", "ME": "ME", "Q": "QE", "QE": "QE", "Y": "YE", "YE": "YE"}

#: 允许的列策略
POLICIES = ("daily", "raw")


def _warn(msg: str) -> None:
    print(f"[time_align] {msg}", file=sys.stderr)


def get(spec, series: Mapping[str, pd.Series]) -> pd.Series:
    """spec 是表名，或 (表名, 列名)。

    `series` 里按"表名"或"表名:列名"存；后者用于多列表。
    """
    if isinstance(spec, tuple):
        table, col = spec
        key = f"{table}:{col}"
    else:
        key = spec
    s = series.get(key)
    if s is None:
        # 多列表：没指定列时按 close → 第一列的顺序兜底
        if not isinstance(spec, tuple):
            for cand in (f"{key}:close", *sorted(k for k in series if k.startswith(f"{key}:"))):
                if cand in series:
                    return series[cand]
        raise KeyError(f"数据包里没有 {key}（可用：{sorted(series)[:6]}…）")
    return s


def lag_of(spec, meta: pd.DataFrame | None) -> int:
    """按 metadata 的 lag_days 取公布滞后；缺了就按 0。"""
    if meta is None or not len(meta):
        return 0
    table = spec[0] if isinstance(spec, tuple) else spec
    try:
        idx = meta.set_index("name")
        v = idx.loc[table, "lag_days"]
        return 0 if pd.isna(v) else int(v)
    except Exception:  # noqa: BLE001
        return 0


def raw(s: pd.Series | None, lag_days: int = 0) -> pd.Series:
    """**原样返回**：只按公布滞后推后日期，不重采样、不填充。

    推后之后索引可能落在周末——那是数据源的发布日，调用方自己决定怎么对齐。
    """
    if s is None or len(s) == 0:
        return pd.Series(dtype=float)
    out = s.copy()
    out.index = pd.DatetimeIndex(out.index)
    if lag_days:
        out.index = out.index + pd.Timedelta(days=int(lag_days))
    return out


def to_daily(s: pd.Series | None, idx: pd.DatetimeIndex, lag_days: int = 0,
             ffill: bool = False) -> pd.Series:
    """把序列对齐到日频 `idx`（闭区间内的交易日）。

    `ffill=False`（**默认**）只做日历对齐：非观测日为 NaN。
    要"每天看到最新值"必须显式 `ffill=True`——这是下游的决定。
    """
    s = raw(s, lag_days)
    if len(s) == 0:
        return pd.Series(index=idx, dtype=float)
    return s.reindex(idx, method="ffill" if ffill else None)


def to_low_freq(df: pd.DataFrame, freq: str = "W",
                how: str | Mapping[str, str] = "last") -> pd.DataFrame:
    """高频 → 低频聚合（周 `W` / 月 `ME` / 年 `YE`）。

    `how`：
    - `"last"`（默认）= **按列给默认口径**：`open→first`、`high→max`、`low→min`、
      `close→last`、`volume/amount/turnover→sum`，其余列 `last`；
    - 其它字符串（`mean` / `sum` / `max` …）= 所有列都用它；
    - 字典 = 按列指定（键是列名）。
    """
    rule = _FREQ.get(str(freq).upper(), str(freq))

    def agg_of(col: Any) -> str:
        if isinstance(how, Mapping):
            return str(how.get(col, "last"))
        if str(how) == "last":
            return _OHLC_HOW.get(str(col).lower(), "last")
        return str(how)

    return df.resample(rule).agg({c: agg_of(c) for c in df.columns})


def daily_index(table: str, series: Mapping[str, pd.Series]) -> pd.DatetimeIndex:
    """以该资产的主价格表作为交易日主轴。"""
    s = get(table, series)
    if s is None or not len(s):
        raise RuntimeError(f"主价格表 {table} 为空")
    return pd.DatetimeIndex(s.index)


def _align_raw(s: pd.Series, idx: pd.DatetimeIndex, lag_days: int = 0,
               name: str = "") -> pd.Series:
    """raw 列放进日频面板：观测日 → 之后第一个交易日（不复制到其他交易日）。

    月末/季末经常是周末（实测 33%~60% 的观测不在交易日历上）。直接 `reindex`
    会把它们丢掉，所以显式映射到"可见的第一天"。

    三种"落不到独立交易日"的情况分开处理：
    - 早于面板第一天的观测：面板覆盖不到，**静默丢弃**（不是数据丢了，是这段历史
      本来就不在面板区间内）；
    - 晚于最后一天的观测：还没"可见"，告警后丢弃；
    - 同一交易日上的多个观测：**值相同**（同月的月初/月末两行）静默保留最后一个，
      **值不同**才告警。
    """
    s = raw(s, lag_days)
    out = pd.Series(index=idx, dtype=float)
    if len(s) == 0 or len(idx) == 0:
        return out

    after = s.index > idx[-1]
    if bool(after.any()):
        _warn(f"{name or 'raw 列'}: {int(after.sum())} 个观测晚于面板最后一个交易日 "
              f"{idx[-1].date()}（尚未可见），已丢弃")
        s = s[~after]
    s = s[s.index >= idx[0]]                    # 面板起点之前：静默丢弃
    if len(s) == 0:
        return out

    pos = idx.searchsorted(pd.DatetimeIndex(s.index), side="left")
    vals = s.to_numpy()
    if len(set(pos.tolist())) != len(pos):
        conflicts = (pd.DataFrame({"pos": pos, "v": vals})
                     .groupby("pos")["v"].nunique(dropna=True))
        bad = int((conflicts > 1).sum())
        if bad:
            _warn(f"{name or 'raw 列'}: {bad} 组观测落到同一交易日但数值不同，"
                  f"保留最后一个（源头多为「同月两行」的月初/月末重复）")
    out.iloc[pos] = vals
    return out


def build(mapping: Mapping[str, Any], index_table: str,
          series: Mapping[str, pd.Series], meta: pd.DataFrame | None = None,
          policy: Mapping[str, str] | None = None) -> pd.DataFrame:
    """按 `{面板列名: 表名/元组}` 拼日频宽表，按 `policy` 决定每列怎么对齐。

    `policy`：`{列名: "daily" | "raw"}`，**默认 `"raw"`**。
    - `"daily"`：按 `lag_days` 推后 + **ffill 到每个交易日**（下游要最新值）
    - `"raw"`：观测日 → 之后第一个交易日，其余交易日 NaN（下游要做 diff / 按观测频率聚合）

    没声明策略 = `"raw"`：忘记声明会表现为"这列没对齐"，被测试抓住，
    而不是被悄悄 ffill 成日频。
    """
    idx = daily_index(index_table, series)
    policy = policy or {}
    data: dict[str, pd.Series] = {}
    for col, spec in mapping.items():
        how = str(policy.get(col, "raw"))
        if how not in POLICIES:
            raise ValueError(f"列 {col!r} 的策略 {how!r} 不认识；可用：{POLICIES}")
        s = get(spec, series)
        lag = lag_of(spec, meta)
        data[col] = to_daily(s, idx, lag, ffill=True) if how == "daily" \
            else _align_raw(s, idx, lag, col)
    return pd.DataFrame(data, index=idx).sort_index()
