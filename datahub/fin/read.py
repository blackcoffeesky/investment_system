"""金融数据的读接口。

    from datahub.fin import read

    read("vix")                        # single → Series
    read("csi_all")                    # multi  → DataFrame
    read("csi_all", column="close")    # multi  → Series
    read_many(["vix", "usdcny"])       # 多个 single → DataFrame
    metadata()                         # 元数据（含 kind / columns / source / depends）
    run_check()                        # 完整性检查
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
        return read_long(name, start=start, end=end, path=path)
    if k == "static":
        return read_static(name, path=path)

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


# ======================================================================
# 四种表结构的解析
# ======================================================================
# single : date, value
# multi  : date, 列…（列名是写死的，如 csi_all 的 open/close）
# long   : code, date, 字段…（标的是数据不是结构，股票五千只不能一列一只）
# static : code, 字段…（一个标的一行，没有日期轴）
#
# 为什么还留这两份 schema：`metadata` 里只有列名、没有**类型**，
# 而建表需要类型（REAL / TEXT）。所以类型表跟读接口放一起，仍然只有一处。

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
    # 股本变动：每次增发/回购注销/送转插一行，市值按日期向前填
    "shares_history": {
        "kind": "TEXT",          # issue / buyback_cancel / bonus / allot / base
        "delta_total": "REAL",
        "total_after": "REAL",
        "float_after": "REAL",
    },
    # 分红送转（原 adj_factor 升级：多了报告期，口径统一到"除权日"）
    "dividend": {
        "report_end": "TEXT",     # 所属报告期 YYYY-MM-DD
        "cash": "REAL",           # 每股现金分红（税前）
        "bonus": "REAL",          # 每股送股比例
        "allot_ratio": "REAL",    # 配股比例
        "allot_price": "REAL",    # 配股价格
        "plan": "TEXT",           # 原始方案文案，如「10派280.242元」
    },
    # 季频财报：报告期 + 披露日（用披露日算 lag，避免前视）
    "financials": {
        "publ_date": "TEXT",      # 披露日 YYYY-MM-DD
        "revenue": "REAL",
        "net_profit": "REAL",
        "equity": "REAL",
        "eps": "REAL",
        "roe": "REAL",
    },
}

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


def _long_columns(table: str) -> list[str]:
    if table not in LONG_SCHEMAS:
        raise KeyError(f"没有登记 long 表 {table!r}；可用：{sorted(LONG_SCHEMAS)}")
    return list(LONG_SCHEMAS[table])


def _static_columns(table: str) -> list[str]:
    if table not in STATIC_SCHEMAS:
        raise KeyError(f"没有登记 static 表 {table!r}；可用：{sorted(STATIC_SCHEMAS)}")
    return list(STATIC_SCHEMAS[table])


def read_long(table: str, *, start: str | None = None, end: str | None = None,
              codes: list[str] | None = None, columns: list[str] | None = None,
              path: Path | None = None) -> pd.DataFrame:
    """按条件读 long 表（不 pivot）。列：code, date, <字段…>。"""
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

    cols = ["code", "date"] + list(columns or _long_columns(table))
    with sqlite3.connect(str(p), timeout=30) as con:
        df = pd.read_sql(
            f'SELECT {", ".join(cols)} FROM "{table}"{where} ORDER BY date, code',
            con, params=args)
    if len(df):
        df["date"] = pd.to_datetime(df["date"])
    return df


def read_static(table: str, codes: list[str] | None = None,
                path: Path | None = None) -> pd.DataFrame:
    """读 static 表（一个标的一行）。"""
    p = Path(path or DB_PATH)
    cond, args = "", []
    if codes:
        cond = f" WHERE code IN ({','.join('?' * len(codes))})"
        args = list(codes)
    with sqlite3.connect(str(p), timeout=30) as con:
        return pd.read_sql(f'SELECT * FROM "{table}"{cond} ORDER BY code', con,
                           params=args)


def pivot(df: pd.DataFrame, value: str = "close") -> pd.DataFrame:
    """long → 宽表（日期 × 代码）。五千只全量 pivot 很占内存，先 codes= 过滤。"""
    if df.empty:
        return pd.DataFrame()
    return df.pivot_table(index="date", columns="code", values=value,
                          aggfunc="last").sort_index()


def long_stats(table: str, path: Path | None = None) -> dict:
    """行数 / 标的数 / 日期范围——供 metadata 登记与 check 用。"""
    p = Path(path or DB_PATH)
    with sqlite3.connect(str(p), timeout=30) as con:
        row = con.execute(
            f'SELECT COUNT(*) n, COUNT(DISTINCT code) c, '
            f'MIN(date) s, MAX(date) e FROM "{table}"').fetchone()
    return {"n_rows": int(row[0]), "codes": int(row[1]),
            "start_date": row[2], "end_date": row[3]}


def static_stats(table: str, path: Path | None = None) -> dict:
    p = Path(path or DB_PATH)
    with sqlite3.connect(str(p), timeout=30) as con:
        n = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    return {"n_rows": int(n)}
