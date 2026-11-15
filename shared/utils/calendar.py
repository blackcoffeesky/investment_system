"""日期对齐引擎。

规则只有一条：**先把所有外部数据对齐到资产自己的交易日历，再合并**。
绝不能让 A 股的节假日出现在黄金的面板里，反之亦然。

实现上就是 `reindex(calendar, method="ffill")`，但把"哪些列是自然日、
哪些列本来就是资产日历"这件事显式记下来，并在对齐时报告丢弃了多少行——
静默对齐是事故的温床（见 doc/PROBLEM.md P3）。
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class AlignReport:
    calendar_len: int
    input_len: int
    dropped: int          # 不在资产日历里的原始日期数
    filled: int           # 被 ffill 补上的交易日数
    extra: int            # 日历里、但原始数据没有（早于起始日）的交易日数

    def __str__(self) -> str:
        return (
            f"日历 {self.calendar_len} 天；原始 {self.input_len} 行，"
            f"丢弃 {self.dropped} 行（非交易日），前向填充 {self.filled} 行"
        )


def trading_calendar(asset_frame: pd.DataFrame) -> pd.DatetimeIndex:
    """资产日历 = 资产自身面板的索引（它只在该资产开市的日子有行）。"""
    cal = pd.DatetimeIndex(asset_frame.index).sort_values()
    if cal.has_duplicates:
        raise ValueError("资产面板索引有重复日期")
    return cal


def align_to_trading_calendar(
    source: pd.DataFrame | pd.Series,
    calendar: pd.DatetimeIndex,
    *,
    name: str = "",
    allow_empty: bool = False,
) -> tuple[pd.DataFrame | pd.Series, AlignReport]:
    """把外部数据对齐到 `calendar`：只保留日历内的日期，其余前向填充。

    不会引入日历之外的日期——这是"不污染"的关键。
    """
    obj = source.copy()
    obj.index = pd.DatetimeIndex(obj.index)
    obj = obj[~obj.index.duplicated(keep="last")].sort_index()

    report = AlignReport(
        calendar_len=len(calendar),
        input_len=len(obj),
        dropped=int((~obj.index.isin(calendar)).sum()),
        filled=0,
        extra=0,
    )

    out = obj.reindex(calendar, method="ffill")

    # 统计填充量。
    # 注意：不能拿"原始索引上的非空行数"去减"日历索引上的非空行数"——两个索引不同，
    # 差值是两件不相干的事的差。必须在**同一个日历**上比较"原生有值"与"填充后有值"。
    native = obj.reindex(calendar)          # 只对齐、不填充：原生有值的交易日
    if isinstance(out, pd.DataFrame):
        native_days = native.notna().any(axis=1).to_numpy()
        out_days = out.notna().any(axis=1).to_numpy()
    else:
        native_days = native.notna().to_numpy()
        out_days = out.notna().to_numpy()

    report.filled = int((out_days & ~native_days).sum())   # 只靠 ffill 才有的交易日
    first_valid = int(out_days.argmax()) if out_days.any() else len(calendar)
    report.extra = int(first_valid)                        # 早于首个观测、无法填充的交易日

    empty = (
        bool(out.dropna(how="all").empty)
        if isinstance(out, pd.DataFrame)
        else bool(out.dropna().empty)
    )
    if not allow_empty and empty:
        raise ValueError(f"{name or '输入'} 对齐后全为空，检查日期格式或时区")

    return out, report


def merge_shared_nodes(
    panel: pd.DataFrame,
    shared: pd.DataFrame,
    *,
    verbose: bool = True,
) -> tuple[pd.DataFrame, dict[str, AlignReport]]:
    """把共享节点合并进资产面板，逐列对齐并返回对齐报告。"""
    cal = trading_calendar(panel)
    aligned, reports = {}, {}
    for col in shared.columns:
        series, rep = align_to_trading_calendar(shared[col], cal, name=col)
        aligned[col] = series
        reports[col] = rep
        if verbose and rep.dropped:
            print(f"  [align] {col}: {rep}")

    out = panel.copy()
    for col, series in aligned.items():
        out[col] = series
    out.index = cal
    return out, reports
