"""intake/shares.py 改为复用 sources/share_events 的规范化规则（数据只在一处定义）。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd

from datahub.core.conn import connect
from datahub.fin.sources.share_events import normalize_events
from datahub.fin.update import write_long


def read_events(csv_path: Path) -> tuple[pd.DataFrame, dict]:
    """csv → 待写入的 event 行 + 统计（**不写库**）。规则全在 `normalize_events`。"""
    raw = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    return normalize_events(raw)


def load_events(csv_path: Path | None = None, *, db: Path | None = None,
                dry_run: bool = False) -> dict:
    """导入事件行；返回统计（含库内行数）。"""
    csv_path = csv_path or Path("data/股本数据/股本变动历史明细.csv")
    df, stats = read_events(csv_path)
    if dry_run:
        stats["dry_run"] = True
        return stats

    con: sqlite3.Connection = connect(db)
    try:
        base_dates = {r[0] for r in con.execute(
            "SELECT DISTINCT date FROM shares_history WHERE kind='base'")}
        stats["与 base 快照同日的事件行"] = int(df["date"].isin(base_dates).sum())
        n = write_long(con, "shares_history", df)
        con.commit()
        total = con.execute("SELECT COUNT(*) FROM shares_history").fetchone()[0]
        by_kind = dict(con.execute(
            "SELECT kind, COUNT(*) FROM shares_history GROUP BY kind").fetchall())
        rng = con.execute("SELECT MIN(date), MAX(date) FROM shares_history").fetchone()
    finally:
        con.close()
    stats.update({"写入行数": n, "库内总行数": total, "按 kind": by_kind,
                  "库内日期范围": f"{rng[0]} ~ {rng[1]}"})
    return stats


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="股本变动事件 → shares_history")
    ap.add_argument("csv", nargs="?", default="data/股本数据/股本变动历史明细.csv")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=None)
    a = ap.parse_args(argv)
    stats = load_events(Path(a.csv), db=Path(a.db) if a.db else None, dry_run=a.dry_run)
    print("=" * 56)
    for k, v in stats.items():
        print(f"  {k:22s} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
