"""金融数据的读接口。

    from datahub.fin import read

    read("vix")                     # single → Series
    db.read("csi_all")                 # multi  → DataFrame
    db.read("csi_all", column="close") # multi  → Series
    db.read_many(["vix", "usdcny"])    # 多个 single → DataFrame
    db.metadata()                      # 元数据（含 kind / columns）
    db.run_check()                     # 完整性检查
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from datahub import DB_PATH
from datahub.core.errors import TableNotFound


def _conn(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or DB_PATH)
    if not p.exists():
        raise FileNotFoundError(
            f"没有 datahub 数据库：{p}；先运行 python -m datahub.fin.update")
    con = sqlite3.connect(str(p), timeout=15)
    con.row_factory = sqlite3.Row
    return con


def tables(path: Path | None = None) -> list[str]:
    """金融表 = metadata 里登记过的表。

    不再扫 sqlite_master：ops_* 不进 metadata，自然不在这里出现，
    调用方（check / update）也不用维护排除名单。
    """
    with _conn(path) as con:
        rows = con.execute("SELECT name FROM metadata ORDER BY name").fetchall()
    return [r["name"] for r in rows]


def metadata(path: Path | None = None) -> pd.DataFrame:
    with _conn(path) as con:
        return pd.read_sql("SELECT * FROM metadata ORDER BY name", con)


def kind_of(name: str, path: Path | None = None) -> str:
    with _conn(path) as con:
        row = con.execute("SELECT kind FROM metadata WHERE name=?", (name,)).fetchone()
    return (row["kind"] if row else None) or "single"


def read(name: str, *, column: str | None = None,
         start: str | None = None, end: str | None = None,
         path: Path | None = None):
    """按 kind 分派：single → Series；multi → DataFrame（或指定 column 取 Series）。"""
    k = kind_of(name, path)
    if k == "long":
        from datahub.fin import long as L

        return L.read(name, start=start, end=end, path=path)

    cond, args = [], []
    if start:
        cond.append("date >= ?")
        args.append(str(start))
    if end:
        cond.append("date <= ?")
        args.append(str(end))
    where = (" WHERE " + " AND ".join(cond)) if cond else ""

    with _conn(path) as con:
        df = pd.read_sql(f'SELECT * FROM "{name}"{where} ORDER BY date', con, params=args)

    if df.empty:
        return pd.Series(dtype=float, name=name) if k == "single" else df

    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")

    if k == "single":
        return df["value"].astype(float).rename(name)
    if column is not None:
        if column not in df.columns:
            raise KeyError(f"{name} 没有列 {column!r}；可用：{list(df.columns)}")
        return df[column].astype(float).rename(f"{name}.{column}")
    return df.astype(float)


def read_many(names: list[str], start: str | None = None,
              end: str | None = None, path: Path | None = None) -> pd.DataFrame:
    """多个 single 序列 → 一个 DataFrame（按日期外连接）。"""
    cols = {}
    for n in names:
        s = read(n, start=start, end=end, path=path)
        if isinstance(s, pd.Series) and len(s):
            cols[n] = s
    return pd.DataFrame(cols).sort_index() if cols else pd.DataFrame()

