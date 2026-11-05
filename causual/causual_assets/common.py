"""资产包共用的「面板组装」工具。

数据层（`datahub`）只给原始表；把它变成资产要用的日频宽表，是**消费方的事**，
所以放在 assets 层，不进 `causual_core`。

两个约定在这里实现：
1. 列名映射：数据层按数据本身命名（`csi_all`、`m1_level`），业务层用大写旧名
2. 可观测日：月频值按 `metadata.lag_days` 推后，再 ffill 到交易日——避免前视
"""

from __future__ import annotations

import pandas as pd


def _ds(ds=None):
    """取 DataService：外部传入优先，否则用进程内单例。"""
    if ds is not None:
        return ds

    from service.data_service import get_data_service

    return get_data_service()


def get(spec, ds=None) -> pd.Series:
    """spec 是表名，或 (表名, 列名)。"""
    svc = _ds(ds)
    if isinstance(spec, tuple):
        table, col = spec
        return svc.read(table, column=col)
    return svc.read(spec)


def lag_of(spec, ds=None) -> int:
    table = spec[0] if isinstance(spec, tuple) else spec
    try:
        v = _ds(ds).metadata().set_index("name").loc[table, "lag_days"]
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


def daily_index(table: str, ds=None) -> pd.DatetimeIndex:
    """以该资产的主价格表作为交易日主轴。"""
    return _ds(ds).read(table).index


def build(mapping: dict, index_table: str, ds=None) -> pd.DataFrame:
    """按 {面板列名: 数据层表名/元组} 拼出日频宽表。"""
    svc = _ds(ds)
    idx = daily_index(index_table, svc)
    data = {col: to_daily(get(spec, svc), idx, lag_of(spec, svc))
            for col, spec in mapping.items()}
    return pd.DataFrame(data, index=idx).sort_index()


