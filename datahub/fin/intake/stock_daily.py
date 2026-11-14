"""把 parquet 里的 A 股日 K / 复权事件导进 datahub 的 long 宽表。

    python -m datahub.fin.intake.stock_daily

数据来源：同花顺 Fuyao 导出的两个 parquet（放在 `data/`）。
- `a_share_daily_k_*.parquet`      原始价日 K（adjusted=none）
- `a_share_adjustment_factors_*.parquet`  除权除息事件

写入 `stock_daily` / `adj_factor` 两张 long 表（schema 见 `datahub/fin/read.py`）。
日期用 **Asia/Shanghai** 解毫秒时间戳——UTC 解会整体偏一天。
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

import pandas as pd

from datahub import DB_PATH
from datahub.core.schema import DATA_DDL
# 注意：`from datahub.fin import read` 拿到的是**函数**不是模块（包属性被同名函数占了），
# 所以这里按函数名 import，不能写成 `import ... as L` 再用 L.long_stats。
from datahub.fin.read import _long_columns, long_stats
from datahub.fin.sources import _with_prefix
from datahub.fin.update import write_long

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data"

K_FILE = DATA / "a_share_daily_k_1d_none_10y_20261113.parquet"
ADJ_FILE = DATA / "a_share_adjustment_factors_event_none_all_20261113.parquet"

EX_PREFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj"}


def _split_code(thscode: pd.Series) -> pd.Series:
    """`000001.SZ` → `sz000001`。

    交易所后缀是**权威**的市场归属（优先用它）；万一没有后缀，按号码段推
    （`_with_prefix`）。全库 code 自 2026-11-14 起统一带前缀。
    """
    c = thscode.astype(str).str.strip()
    parts = c.str.partition(".")
    num, ex = parts[0], parts[2].str.upper().map(EX_PREFIX).fillna("")
    return (ex + num).where(ex != "", num.map(_with_prefix))


def _ms_to_date(ms: pd.Series) -> pd.Series:
    """毫秒时间戳 → Asia/Shanghai 的日期字符串。"""
    return (pd.to_datetime(ms, unit="ms", utc=True)
            .dt.tz_convert("Asia/Shanghai")
            .dt.strftime("%Y-%m-%d"))


def ingest_k(con: sqlite3.Connection, path: Path = K_FILE) -> int:
    df = pd.read_parquet(path)
    out = pd.DataFrame({
        "code": _split_code(df["thscode"]),
        "date": _ms_to_date(df["date_ms"]),
        "open": df["open_price"],
        "high": df["high_price"],
        "low": df["low_price"],
        "close": df["close_price"],
        "volume": df["volume"],
        "turnover": df["turnover"],
    })
    t0 = time.time()
    n = write_long(con, "stock_daily", out)
    print(f"stock_daily: {n} 行，{out.code.nunique()} 只，"
          f"{out.date.min()} ~ {out.date.max()}，{time.time()-t0:.1f}s")
    return n


def ingest_adj(con: sqlite3.Connection, path: Path = ADJ_FILE) -> int:
    df = pd.read_parquet(path)
    out = pd.DataFrame({
        "code": _split_code(df["thscode"]),
        "date": _ms_to_date(df["ex_date_ms"]),
        "dividend": df["dividend_per_share"],
        "bonus": df["per_share_bonus"],
        "allot_ratio": df["allotment_ratio"],
        "allot_price": df["allotment_price"],
    })
    # 同一天同一只股票可能有多条事件，合并成一条
    out = out.groupby(["code", "date"], as_index=False).agg(
        dividend=("dividend", "sum"), bonus=("bonus", "sum"),
        allot_ratio=("allot_ratio", "sum"), allot_price=("allot_price", "max"))
    n = write_long(con, "adj_factor", out)
    print(f"adj_factor: {n} 条事件，{out.code.nunique()} 只")
    return n


def register(con: sqlite3.Connection, table: str, freq: str = "D",
             market: str = "CN", note: str = "") -> None:
    st = long_stats(table)
    con.execute("DELETE FROM metadata WHERE name=?", (table,))
    con.execute(
        "INSERT INTO metadata(name, display, freq, unit, start_date, end_date,"
        " n_rows, note, kind, columns, market, lag_days, source)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (table, table, freq, "", st["start_date"], st["end_date"], st["n_rows"],
         note or f"long 表，{st['codes']} 个标的", "long",
         ",".join(_long_columns(table)), market, 0, "raw"))
    con.commit()
    print(f"metadata: {table} n_rows={st['n_rows']} codes={st['codes']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k-file", default=str(K_FILE))
    ap.add_argument("--adj-file", default=str(ADJ_FILE))
    a = ap.parse_args(argv)

    con = sqlite3.connect(str(DB_PATH), timeout=60)
    con.executescript(DATA_DDL)
    ingest_k(con, Path(a.k_file))
    ingest_adj(con, Path(a.adj_file))
    register(con, "stock_daily", note="A 股日 K（未复权）")
    register(con, "adj_factor", freq="D", note="除权除息事件（复权因子来源）")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
