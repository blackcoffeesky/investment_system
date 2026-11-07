"""datahub 的写入口：给服务层（数据上传页）用。

为什么单独一层
--------------
service 不该拼 SQL 写库。这里提供两个受控写入：
- `upsert_single`：单值（手动数据大多是单列），带类型与频率校验
- `write_frame`：解析后的整表（走 intake parser 的产物）

两者都会更新 metadata 的 start/end_date，并保留原有 kind/market/freq。
"""

from __future__ import annotations

import pandas as pd

from pathlib import Path

from datahub.core.conn import transaction


def _table_exists(con, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return bool(row)


def _ensure_table(con, table: str) -> None:
    if not _table_exists(con, table):
        con.execute(f'CREATE TABLE "{table}"(date TEXT PRIMARY KEY, value REAL)')


def _bump_meta(table: str, db_path: Path | None = None) -> None:
    """把 metadata 的 start/end_date 同步成表里的实际范围。"""
    with transaction(db_path) as con:
        row = con.execute(
            f'SELECT MIN(date), MAX(date) FROM "{table}"').fetchone()
        if not row or not row[0]:
            return
        exists = con.execute("SELECT 1 FROM metadata WHERE name=?", (table,)).fetchone()
        if not exists:
            con.execute(
                "INSERT INTO metadata(name, kind, market, freq, lag_days,"
                " start_date, end_date) VALUES (?,?,?,?,?,?,?)",
                (table, "single", "CN", "M", 0, row[0], row[1]))
            return
        con.execute(
            "UPDATE metadata SET start_date=?, end_date=? WHERE name=?",
            (row[0], row[1], table))


def upsert_single(table: str, date: str, value: float | str,
                  *, overwrite: bool = True, db_path: Path | None = None) -> dict:
    """写一个 (date, value)。返回 {before, after, written}。"""
    from datetime import datetime

    try:
        d = pd.Timestamp(date).normalize()
    except (ValueError, TypeError) as e:
        raise ValueError(f"日期无法解析：{date}") from e
    if pd.isna(d):
        raise ValueError(f"日期无法解析：{date}")
    try:
        v = float(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"数值无法解析：{value}") from e

    with transaction(db_path) as con:
        _ensure_table(con, table)
        row = con.execute(
            f'SELECT value FROM "{table}" WHERE date=?', (str(d.date()),)).fetchone()
        before = None if row is None else float(row[0])
        if before is not None and not overwrite:
            raise ValueError(f"{table} 在 {d.date()} 已有值 {before}，不允许覆盖")

        con.execute(
            f'INSERT INTO "{table}"(date, value) VALUES (?,?) '
            f'ON CONFLICT(date) DO UPDATE SET value=excluded.value',
            (str(d.date()), v))

        # 元数据若缺这一行就补一个
        exists = con.execute("SELECT 1 FROM metadata WHERE name=?", (table,)).fetchone()
        if not exists:
            con.execute(
                "INSERT INTO metadata(name, kind, market, freq, lag_days, start_date, end_date)"
                " VALUES (?,?,?,?,?,?,?)",
                (table, "single", "CN", "M", 0, str(d.date()), str(d.date())))
        con.execute(
            "UPDATE metadata SET start_date=MIN(COALESCE(start_date,?), ?),"
            " end_date=MAX(COALESCE(end_date,?), ?) WHERE name=?",
            (str(d.date()), str(d.date()), str(d.date()), str(d.date()), table))
    _bump_meta(table, db_path)
    return {"table": table, "date": str(d.date()), "before": before,
            "after": v, "written": True}


def write_frame(table: str, frame: pd.DataFrame | pd.Series,
                *, db_path: Path | None = None) -> dict:
    """整表写入（intake parser 的产物）。date 做索引，value 为单列或首列。"""
    if isinstance(frame, pd.Series):
        s = frame
    else:
        if frame.empty:
            return {"table": table, "rows": 0}
        idx = frame.index if isinstance(frame.index, pd.DatetimeIndex) else frame.iloc[:, 0]
        col = frame.iloc[:, 0] if not isinstance(frame.index, pd.DatetimeIndex) else frame.iloc[:, 0]
        s = pd.Series(col.to_numpy(), index=pd.to_datetime(idx))

    rows = [(str(pd.Timestamp(str(i)).date()), float(v)) for i, v in s.dropna().items()]
    if not rows:
        return {"table": table, "rows": 0}

    with transaction(db_path) as con:
        _ensure_table(con, table)
        con.executemany(
            f'INSERT INTO "{table}"(date, value) VALUES (?,?) '
            f'ON CONFLICT(date) DO UPDATE SET value=excluded.value', rows)
    _bump_meta(table, db_path)
    return {"table": table, "rows": len(rows)}
