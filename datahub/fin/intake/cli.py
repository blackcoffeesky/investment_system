"""手工数据接入 CLI。

    python -m datahub.fin.intake wgc D:\\cache\\EdgeDownload\\1793522742.xlsx
    python -m datahub.fin.intake wgc <file> --load      # 解析后写入数据库

流程
----
1. `registry.SOURCES[key].parser(raw)` 解析原始文件
2. 与库里现有值比较，取「新增 + 有变化」的行（源的修订会自动覆盖）
3. 写出 `intake/clean/<table>.csv`（列契约与库表一致）
4. `--load` 时 upsert 入库并刷新 metadata

解析失败会把原始异常包成一条**给人看的中文提示**，说明可能的原因
（表名改了、列名变了、篮子里的国家消失），因为这类错误本质是"源结构变了"。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd

from datahub import DB_PATH
from datahub.core.conn import connect as _connect
from datahub.fin.intake.registry import SOURCES
from datahub.core.schema import DATA_DDL
from datahub.fin.update import _ensure_meta, refresh_metadata

CLEAN_DIR = Path(__file__).resolve().parent / "clean"


def _existing(table: str) -> pd.Series:
    try:
        import datahub.fin as fin
        if table not in fin.tables():
            return pd.Series(dtype=float)
        return fin.read(table)
    except Exception:  # noqa: BLE001
        return pd.Series(dtype=float)


def diff_to_write(new: pd.Series, old: pd.Series, tol: float = 1e-9) -> pd.Series:
    """只保留要写的行：库里没有的，或值变了（源的修订）。"""
    if old is None or len(old) == 0:
        return new
    old = old.reindex(new.index)
    changed = new.index[
        old.isna().to_numpy() | ((new.to_numpy() - old.to_numpy()) > tol) |
        ((old.to_numpy() - new.to_numpy()) > tol)
    ]
    return new.loc[changed]


def run(key: str, raw: str, load: bool = False, dry_run: bool = False) -> int:
    src = SOURCES.get(key)
    if src is None:
        print(f"未知数据源：{key}；可用：{', '.join(SOURCES)}", file=sys.stderr)
        return 2

    raw_path = Path(raw)
    if not raw_path.exists():
        print(f"文件不存在：{raw_path}", file=sys.stderr)
        return 2

    try:
        series = src.parser(raw_path)
    except Exception as e:  # noqa: BLE001
        print("\n解析失败。", file=sys.stderr)
        print(f"  {type(e).__name__}: {e}", file=sys.stderr)
        print("\n** raw 的结构可能变了 **：请对着 `intake/parsers/` 里的函数检查"
              "表名、表头、国家名是否还对得上。", file=sys.stderr)
        return 1

    if not isinstance(series, pd.Series) or not len(series):
        print("解析结果为空，检查 raw 是否选错了工作表/文件。", file=sys.stderr)
        return 1

    old = _existing(src.table)
    to_write = diff_to_write(series, old)

    print(f"源 {key} → 表 {src.table}（{src.freq}）")
    print(f"  解析出 {len(series)} 行：{series.index[0].date()} ~ {series.index[-1].date()}")
    print(f"  新增/修订 {len(to_write)} 行" + ("" if len(to_write) else "（库已是最新）"))
    for d, v in to_write.tail(6).items():
        print(f"    {d.date()}  {float(v):,.2f}")

    if dry_run or not len(to_write):
        return 0

    CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    out = to_write.rename("value").rename_axis("date").reset_index()
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    clean_path = CLEAN_DIR / f"{src.table}.csv"
    out.to_csv(clean_path, index=False, encoding="utf-8-sig")
    print(f"  写出 {clean_path}")

    if not load:
        print("  （未入库；加 --load 写入数据库）")
        return 0

    con = _connect()
    con.executescript(DATA_DDL)
    con.execute(f'CREATE TABLE IF NOT EXISTS "{src.table}" (date TEXT PRIMARY KEY, value REAL)')
    con.executemany(f'INSERT OR REPLACE INTO "{src.table}"(date, value) VALUES (?,?)',
                    [(str(d.date()), float(v)) for d, v in to_write.items()])
    con.execute(
        "INSERT OR IGNORE INTO metadata(name, display, freq, unit, start_date, end_date,"
        " n_rows, note, kind, columns, market, lag_days)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (src.table, src.table, src.freq, "吨", "", "", 0, src.note, "single", "", "NONE", 45))
    con.execute("UPDATE metadata SET freq=?, note=? WHERE name=?",
                (src.freq, src.note, src.table))
    con.commit()
    _ensure_meta(con)
    refresh_metadata(con)
    con.close()
    print(f"  已写入 {src.table}（{len(to_write)} 行）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m datahub.fin.intake")
    ap.add_argument("source", help="数据源标识，如 wgc")
    ap.add_argument("raw", help="原始文件路径")
    ap.add_argument("--load", action="store_true", help="解析后写入数据库")
    ap.add_argument("--dry-run", action="store_true", help="只解析、不写 clean")
    a = ap.parse_args(argv)
    return run(a.source, a.raw, load=a.load, dry_run=a.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
