"""long 型宽表：一张表存"多个标的 × 多个字段 × 日期"。

为什么需要这一种
----------------
`multi` 是"日期 × 列"，列名是写死的（`csi_all` 的 open/close…）。
但股票有五千多只，一只一列就是五千列——建表、改表、补列都不现实。

`long` 反过来：**一行一个 (标的, 日期)**，标的是数据不是结构。

    CREATE TABLE stock_daily (
        code TEXT NOT NULL,
        date TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL, volume REAL, turnover REAL,
        PRIMARY KEY (code, date)
    )

代价是读取时要 pivot，所以这里同时提供 `pivot()`。

schema 登记在 `LONG_SCHEMAS`，建表按登记来，不用手写 DDL。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from datahub import DB_PATH

# 表名 → {字段名: SQLite 类型}；`code` / `date` 是固定主键，不用重复声明
LONG_SCHEMAS: dict[str, dict[str, str]] = {
    # A 股日 K（未复权，复权因子在 adj_factor 里）
    "stock_daily": {
        "open": "REAL", "high": "REAL", "low": "REAL", "close": "REAL",
        "volume": "REAL",       # 成交量（股）
        "turnover": "REAL",     # 成交额（元）
    },
    # 除权除息事件：每股分红/送股/配股
    "adj_factor": {
        "dividend": "REAL",     # 每股现金分红（税前）
        "bonus": "REAL",        # 每股送股比例
        "allot_ratio": "REAL",  # 配股比例
        "allot_price": "REAL",  # 配股价格
    },
    # ETF 份额（原 multi 表，改 long 以便随时加基金）
    "etf_shares": {"shares": "REAL"},
}


def columns_of(table: str) -> list[str]:
    """除主键外的字段名。"""
    if table not in LONG_SCHEMAS:
        raise KeyError(f"没有登记 long 表 {table!r}；可用：{sorted(LONG_SCHEMAS)}")
    return list(LONG_SCHEMAS[table])


def ddl(table: str) -> str:
    cols = ", ".join(f"{c} {t}" for c, t in LONG_SCHEMAS[table].items())
    return (f'CREATE TABLE IF NOT EXISTS "{table}" (\n'
            f'    code TEXT NOT NULL,\n    date TEXT NOT NULL,\n'
            f'    {cols},\n    PRIMARY KEY (code, date))\n')


def ensure(con: sqlite3.Connection, table: str) -> None:
    """建表 + 日期索引（按日期查比按代码查更常见）。"""
    con.executescript(ddl(table))
    con.execute(f'CREATE INDEX IF NOT EXISTS "idx_{table}_date" ON "{table}"(date)')
    con.commit()


def write(con: sqlite3.Connection, table: str, df: pd.DataFrame) -> int:
    """写入 long 宽表（upsert）。df 需含 code / date 及已登记字段。"""
    ensure(con, table)
    cols = columns_of(table)
    use = ["code", "date"] + [c for c in cols if c in df.columns]
    frame = df[use].copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d")
    sql = (f'INSERT INTO "{table}" ({", ".join(use)}) '
           f'VALUES ({", ".join("?" * len(use))}) '
           f'ON CONFLICT(code, date) DO UPDATE SET '
           + ", ".join(f"{c}=excluded.{c}" for c in use[2:]))
    con.executemany(sql, frame.itertuples(index=False, name=None))
    con.commit()
    return len(frame)


def read(table: str, *, start: str | None = None, end: str | None = None,
         codes: list[str] | None = None, columns: list[str] | None = None,
         path: Path | None = None) -> pd.DataFrame:
    """按条件读 long 表（不 pivot）。

    返回列：code, date, <字段…>；date 已转成 datetime。
    """
    p = Path(path or DB_PATH)
    if not p.exists():
        raise FileNotFoundError(f"没有 datahub 数据库：{p}")
    cond, args = [], []
    if start:
        cond.append("date >= ?")
        args.append(str(start))
    if end:
        cond.append("date <= ?")
        args.append(str(end))
    if codes:
        cond.append(f"code IN ({','.join('?' * len(codes))})")
        args.extend(codes)
    where = (" WHERE " + " AND ".join(cond)) if cond else ""

    cols = ["code", "date"] + list(columns or columns_of(table))
    with sqlite3.connect(str(p), timeout=30) as con:
        df = pd.read_sql(
            f'SELECT {", ".join(cols)} FROM "{table}"{where} ORDER BY date, code',
            con, params=args)
    if len(df):
        df["date"] = pd.to_datetime(df["date"])
    return df


def pivot(df: pd.DataFrame, value: str = "close") -> pd.DataFrame:
    """long → 宽表（日期 × 代码）。

    五千只股票全量 pivot 会很占内存（约 5000 × 2500 个格子），
    调用方应该先用 `codes=` 过滤到真正要用的那一小撮。
    """
    if df.empty:
        return pd.DataFrame()
    return df.pivot_table(index="date", columns="code", values=value,
                          aggfunc="last").sort_index()


def stats(table: str, path: Path | None = None) -> dict:
    """行数 / 标的数 / 日期范围——供 metadata 登记与 check 用。"""
    p = Path(path or DB_PATH)
    with sqlite3.connect(str(p), timeout=30) as con:
        row = con.execute(
            f'SELECT COUNT(*) n, COUNT(DISTINCT code) c, '
            f'MIN(date) s, MAX(date) e FROM "{table}"').fetchone()
    return {"n_rows": int(row[0]), "codes": int(row[1]),
            "start_date": row[2], "end_date": row[3]}
