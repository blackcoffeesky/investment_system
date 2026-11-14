"""static 型表：**一个标的一行**、没有日期轴的档案表。

    CREATE TABLE company (
        code TEXT PRIMARY KEY,
        name TEXT, exchange TEXT, board TEXT,
        list_date TEXT, delist_date TEXT, industry TEXT,
        total_shares REAL, float_shares REAL
    )

为什么不塞进 long：long 的主键是 (code, date)，公司名称/行业这类"当前值"没有日期轴，
硬塞只能编一个假日期；而 single/multi 又要求 date 列。所以单列一种 kind。

"历史值"（股本变动）属于 `shares_history`，不要往这里覆盖——否则回测时
用今天的股本算昨天的市值，全错。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from datahub import DB_PATH

STATIC_SCHEMAS: dict[str, dict[str, str]] = {
    "company": {
        "name": "TEXT",
        "exchange": "TEXT",       # SH / SZ / BJ
        "board": "TEXT",          # 主板 / 创业板 / 科创板 / 北交所
        "list_date": "TEXT",
        "delist_date": "TEXT",
        "industry": "TEXT",
        "total_shares": "REAL",
        "float_shares": "REAL",
    },
}


def columns_of(table: str) -> list[str]:
    if table not in STATIC_SCHEMAS:
        raise KeyError(f"没有登记 static 表 {table!r}；可用：{sorted(STATIC_SCHEMAS)}")
    return list(STATIC_SCHEMAS[table])


def ddl(table: str) -> str:
    cols = ", ".join(f"{c} {t}" for c, t in STATIC_SCHEMAS[table].items())
    return (f'CREATE TABLE IF NOT EXISTS "{table}" (\n'
            f'    code TEXT PRIMARY KEY,\n    {cols}\n)')


def ensure(con: sqlite3.Connection, table: str) -> None:
    con.executescript(ddl(table))
    con.commit()


def write(con: sqlite3.Connection, table: str, df: pd.DataFrame) -> int:
    """按 code upsert。df 需含 code 及已登记字段。"""
    ensure(con, table)
    cols = columns_of(table)
    use = ["code"] + [c for c in cols if c in df.columns]
    frame = df[use].copy()
    sql = (f'INSERT INTO "{table}" ({", ".join(use)}) '
           f'VALUES ({", ".join("?" * len(use))}) '
           f'ON CONFLICT(code) DO UPDATE SET '
           + ", ".join(f"{c}=excluded.{c}" for c in use[1:]))
    con.executemany(sql, frame.itertuples(index=False, name=None))
    con.commit()
    return len(frame)


def read(table: str, codes: list[str] | None = None,
         path: Path | None = None) -> pd.DataFrame:
    p = Path(path or DB_PATH)
    cond, args = "", []
    if codes:
        cond = f" WHERE code IN ({','.join('?' * len(codes))})"
        args = list(codes)
    with sqlite3.connect(str(p), timeout=30) as con:
        return pd.read_sql(f'SELECT * FROM "{table}"{cond} ORDER BY code', con,
                           params=args)


def stats(table: str, path: Path | None = None) -> dict:
    p = Path(path or DB_PATH)
    with sqlite3.connect(str(p), timeout=30) as con:
        n = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    return {"n_rows": int(n)}
