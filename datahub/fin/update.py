"""按 `SOURCES` 注册表更新各表。

    python -m datahub.update                    # 全部启用项，更新到今天
    python -m datahub.update --start 2024-01-01 --end 2026-10-01
    python -m datahub.update --only vix,csi_all
    python -m datahub.update --dry-run          # 只抓不写

流程
----
1. 遍历 `SOURCES` 里 `enabled` 的项，调 `func(start=, end=, table=, **params)`
2. `table` 含 `|` 时按列名拆成多张单列表（拆表是这里的职责）
3. 只 upsert、不删历史；日期截断到 `end`
4. 写完补齐 metadata、刷新起止日期与行数
5. 失败记日志并继续，不影响其他表
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pandas as pd

from datahub import DB_PATH, ROOT
from datahub.core.schema import DATA_DDL
from datahub.fin.sources import SOURCES, Source

MISSING_DIR = ROOT / "data" / "missing"

# 自动建表时的元信息提示（不写的话会按日频猜测，过期判断会误报）
FREQ_HINT = {
    "retail_sales_mom": ("M", "NONE", 15),
    "world_tension": ("D", "US", 0),
    "gold_etf_holdings": ("M", "NONE", 40),
    "cb_holdings": ("Q", "NONE", 45),
    # 美联储 H.10 周报：汇率与广义美元指数按周发布，落后 7~10 天是常态
    "usdcny": ("D", "US", 8),
    "usd_index": ("D", "US", 8),
    "fed_rate": ("D", "US", 2),
    "real_yield_10y": ("D", "US", 2),
    "vix": ("D", "US", 2),
    # 2026-11-10 新增：成交额/换手率/宽度都是 A 股日频，必须挂 SSE 日历，
    # 否则会把周末当成缺口（历史缺口 1840 天就是这么来的）
    "csi_all_amount": ("D", "CN", 0),
    "csi_all_turnover": ("D", "CN", 0),
    "breadth_adv": ("D", "CN", 0),
    "breadth_dec": ("D", "CN", 0),
    "breadth_limitup": ("D", "CN", 0),
}


def _write(con: sqlite3.Connection, table: str, obj, end: str) -> int:
    if isinstance(obj, pd.Series):
        s = obj.dropna()
        s.index = pd.to_datetime(s.index)
        s = s[s.index <= pd.Timestamp(end)]
        con.execute(f'CREATE TABLE IF NOT EXISTS "{table}" (date TEXT PRIMARY KEY, value REAL)')
        rows = [(str(i.date()), float(v)) for i, v in s.items()]
        con.executemany(f'INSERT OR REPLACE INTO "{table}"(date, value) VALUES (?,?)', rows)
        return len(rows)

    df = obj.copy()
    df.index = pd.to_datetime(df.index)
    df = df[df.index <= pd.Timestamp(end)].dropna(how="all")
    cols = [str(c) for c in df.columns]
    ddl = ", ".join(f'"{c}" REAL' for c in cols)
    con.execute(f'CREATE TABLE IF NOT EXISTS "{table}" (date TEXT PRIMARY KEY, {ddl})')

    # 源返回的列可能变多或变少（ETF 名单会增删）——补齐缺列，按显式列名插入。
    existing = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
    for c in cols:
        if c not in existing:
            con.execute(f'ALTER TABLE "{table}" ADD COLUMN "{c}" REAL')
    col_sql = ", ".join(f'"{c}"' for c in cols)
    ph = ",".join("?" * (len(cols) + 1))
    rows = [(str(i.date()), *[None if pd.isna(v) else float(v) for v in r])
            for i, r in df.iterrows()]
    con.executemany(
        f'INSERT OR REPLACE INTO "{table}" (date, {col_sql}) VALUES ({ph})', rows)
    return len(rows)


def _ensure_meta(con: sqlite3.Connection) -> int:
    """给"有表但没有 metadata 行"的表补一条最小元数据。"""
    have = {r[0] for r in con.execute("SELECT name FROM metadata")}
    names = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT IN ('metadata','missing','sqlite_sequence')"
        " AND name NOT LIKE 'ops_%' AND name NOT LIKE 'sqlite_%'")]
    n = 0
    for name in names:
        if name in have:
            continue
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{name}")')]
        kind = "single" if cols == ["date", "value"] else "multi"
        freq, market, lag = FREQ_HINT.get(name, ("D", "NONE", 0))
        # 已存在的表也纠正 lag_days（发布滞后会变，别让旧值一直留着）
        if name in FREQ_HINT:
            con.execute("UPDATE metadata SET lag_days=?, market=? WHERE name=?",
                        (lag, market, name))
        con.execute(
            "INSERT OR IGNORE INTO metadata(name, display, freq, unit, start_date,"
            " end_date, n_rows, note, kind, columns, market, lag_days)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, name, freq, "", "", "", 0, "自动创建", kind,
             ",".join(c for c in cols if c != "date"), market, lag))
        n += 1
    con.commit()
    return n


def _apply_note(con: sqlite3.Connection, s: Source, tables: list[str]) -> None:
    """把 Source.note / kind 落到 metadata。"""
    for t in tables:
        row = con.execute("SELECT note FROM metadata WHERE name=?", (t,)).fetchone()
        if row is None:
            continue
        note = s.note or row[0] or ""
        if s.kind == "manual" and "手工" not in str(note):
            note = f"手工维护；{note}" if note else "手工维护"
        con.execute("UPDATE metadata SET note=? WHERE name=?", (note, t))


def refresh_metadata(con: sqlite3.Connection) -> int:
    """按实际表内容重写 metadata 的起止日期与行数。"""
    n = 0
    for name, _kind in con.execute("SELECT name, kind FROM metadata").fetchall():
        try:
            cur = con.execute(f'SELECT MIN(date), MAX(date), COUNT(*) FROM "{name}"').fetchone()
        except sqlite3.OperationalError:
            continue
        if not cur or cur[2] == 0:
            continue
        con.execute("UPDATE metadata SET start_date=?, end_date=?, n_rows=? WHERE name=?",
                    (cur[0], cur[1], int(cur[2]), name))
        n += 1
    con.commit()
    return n


def run(start: str = "2000-01-01", end: str | None = None,
        only: list[str] | None = None, dry_run: bool = False) -> pd.DataFrame:
    end = end or str(pd.Timestamp.today().date())
    picked = [s for s in SOURCES if s.enabled]
    if only:
        picked = [s for s in picked if any(t in only for t in s.table.split("|"))]

    from datahub.core.conn import connect as _connect
    con = None if dry_run else _connect()
    if con is not None:
        con.executescript(DATA_DDL)

    results = []
    for s in picked:
        tables = [t.strip() for t in s.table.split("|")]
        try:
            obj = s.func(start=start, end=end, table=tables[0], **s.params)

            if isinstance(obj, pd.DataFrame) and len(tables) > 1:
                lack = [t for t in tables if t not in obj.columns]
                if lack:
                    raise KeyError(f"{s.func.__name__} 返回的列里没有 {lack}；"
                                   f"实际列 {list(obj.columns)}")
                for t in tables:
                    if dry_run:
                        print(f"  [dry] {t:22s} single {len(obj[t].dropna()):5d} 行  "
                              f"{s.func.__name__}#{t}")
                    else:
                        n = _write(con, t, obj[t], end)
                        con.commit()
                        print(f"  {t:22s} single +{n:5d} 行  {s.func.__name__}#{t}")
                if not dry_run:
                    _apply_note(con, s, tables)
                results.append({"table": s.table, "status": "dry" if dry_run else "ok",
                                "rows": int(len(obj)), "msg": f"拆分为 {len(tables)} 表"})
                continue

            kind = "multi" if isinstance(obj, pd.DataFrame) else "single"
            if dry_run:
                print(f"  [dry] {tables[0]:22s} {kind:6s} {len(obj):5d} 行  {s.func.__name__}")
                results.append({"table": s.table, "status": "dry", "rows": len(obj), "msg": ""})
                continue
            n = _write(con, tables[0], obj, end)
            con.commit()          # 立刻释放写锁：源函数内部可能还要读库
            _apply_note(con, s, tables)
            print(f"  {tables[0]:22s} {kind:6s} +{n:5d} 行  {s.func.__name__}")
            results.append({"table": s.table, "status": "ok", "rows": n, "msg": ""})
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {str(e)[:120]}"
            results.append({"table": s.table, "status": "失败", "rows": 0, "msg": msg})
            print(f"  {s.table:22s} 失败  {msg}")
            if not dry_run:
                MISSING_DIR.mkdir(parents=True, exist_ok=True)
                with (MISSING_DIR / "update_failures.log").open("a", encoding="utf-8") as f:
                    f.write(f"[{pd.Timestamp.now():%Y-%m-%d %H:%M}] {s.table}\t{msg}\n")

    if con is not None:
        _ensure_meta(con)
        n = refresh_metadata(con)
        con.close()
        print(f"\n元数据刷新：{n} 张表")

    return pd.DataFrame(results)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2000-01-01", help="起始日期")
    ap.add_argument("--end", default=None, help="截止日期（默认今天）")
    ap.add_argument("--only", default=None, help="只更新这些表，逗号分隔")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    df = run(start=a.start, end=a.end,
             only=[x.strip() for x in a.only.split(",")] if a.only else None,
             dry_run=a.dry_run)
    print("\n汇总：")
    print(df.groupby("status").size().to_string())
