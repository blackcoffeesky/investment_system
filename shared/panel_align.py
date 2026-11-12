"""面板对齐：把 {表名: Series} 拼成日频宽表。

**纯工具**：不读库、不碰文件，调用方把 series/meta 传进来。
原先叫 `panel.py`，容易被误认为"取数据的面板接口"，改名 `align.py`。

数据层只给原始表；把它变成日频宽表这一步是纯数据操作，不含任何资产语义，
所以放在 `datahub`（而不是某个资产包里）。状态层和因果层都从这里取，
避免两套对齐逻辑各自演化。

这里的函数都**只吃数据、不取数据**：调用方把 `series`（{表名: Series}）
和 `meta`（metadata 表）传进来。causual 因此不知道数据从哪来——
CLI 传 datahub 的库，service 传端口读出来的东西，测试传假的 Series。

两个约定在这里实现：
1. 列名映射：数据层按数据本身命名（`csi_all`、`m1_level`），业务层用大写旧名
2. 可观测日：月频值按 `metadata.lag_days` 推后，再 ffill 到交易日——避免前视
"""

from __future__ import annotations

import pandas as pd


def get(spec, series: dict[str, pd.Series]) -> pd.Series:
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


def to_daily(s: pd.Series, idx: pd.DatetimeIndex, lag_days: int = 0,
             ffill: bool = True) -> pd.Series:
    """低频序列 → 日频，并按公布滞后推后可观测日。

    数据层的月频值记在**所属月末**（2026-09-30 是 9 月 CPI），但 9 月 CPI 要到
    10 月 9 日才公布。按 `lag_days` 推后，才不会在回测里用到"未来才知道"的数字。
    """
    if s is None or len(s) == 0:
        return pd.Series(index=idx, dtype=float)
    if lag_days:
        s = s.copy()
        s.index = s.index + pd.Timedelta(days=int(lag_days))
    return s.reindex(idx, method="ffill" if ffill else None)


def daily_index(table: str, series: dict[str, pd.Series]) -> pd.DatetimeIndex:
    """以该资产的主价格表作为交易日主轴。"""
    s = get(table, series)
    if s is None or not len(s):
        raise RuntimeError(f"主价格表 {table} 为空")
    return s.index


def build(mapping: dict, index_table: str,
          series: dict[str, pd.Series],
          meta: pd.DataFrame | None = None) -> pd.DataFrame:
    """按 {面板列名: 表名/元组} 拼出日频宽表。"""
    idx = daily_index(index_table, series)
    data = {col: to_daily(get(spec, series), idx, lag_of(spec, meta))
            for col, spec in mapping.items()}
    return pd.DataFrame(data, index=idx).sort_index()
