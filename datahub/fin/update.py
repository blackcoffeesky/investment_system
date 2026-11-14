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
from datahub.fin.read import (LONG_SCHEMAS, STATIC_SCHEMAS,
                              _long_columns, _static_columns)
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
    "csi_all": ("D", "CN", 0),
    "breadth_adv": ("D", "CN", 0),
    "breadth_dec": ("D", "CN", 0),
    "breadth_limitup": ("D", "CN", 0),
    # 公司三表：低频 + 事件型
    "company": ("M", "CN", 0),          # 静态档案，每月刷
    "shares_history": ("Q", "CN", 0),   # 股本快照，季度刷
    "financials": ("Q", "CN", 45),       # 财报披露滞后约 45 天
    "dividend": ("SPARSE", "NONE", 0),   # 事件表，不查逐日缺口
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
    # 先纠正**已有表**的 market / lag：发布滞后和市场归属会变，
    # 但这段逻辑以前只在新表分支里跑，老表永远修不到（周末被当缺口的根因）。
    for name, (freq, market, lag) in FREQ_HINT.items():
        if name in have:
            con.execute("UPDATE metadata SET lag_days=?, market=?, freq=?"
                        " WHERE name=?", (lag, market, freq, name))

    n = 0
    for name in names:
        if name in have:
            continue
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{name}")')]
        from datahub.fin.read import LONG_SCHEMAS
        from datahub.fin.read import STATIC_SCHEMAS

        if name in LONG_SCHEMAS:
            kind = "long"
        elif name in STATIC_SCHEMAS:
            kind = "static"
        elif cols == ["date", "value"]:
            kind = "single"
        else:
            kind = "multi"
        freq, market, lag = FREQ_HINT.get(name, ("D", "NONE", 0))
        con.execute(
            "INSERT OR IGNORE INTO metadata(name, display, freq, unit, start_date,"
            " end_date, n_rows, note, kind, columns, market, lag_days)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, name, freq, "", "", "", 0, "自动创建", kind,
             ",".join(c for c in cols if c != "date"), market, lag))
        n += 1
    con.commit()
    return n


# ---------------------------------------------------------------- 该不该跑
def _period_end(freq: str, today: pd.Timestamp, lag: int) -> pd.Timestamp | None:
    """按频率与公布滞后，算出"现在应该已经有的最新一期"是哪天。"""
    t = today - pd.Timedelta(days=max(int(lag), 0))
    f = str(freq or "D").upper()
    if f == "D":
        return t.normalize()
    if f == "W":
        return (t - pd.Timedelta(days=t.weekday() + 1)).normalize()
    if f == "M":
        return (t.replace(day=1) - pd.Timedelta(days=1)).normalize()
    if f == "Q":
        q_start = t.replace(month=((t.month - 1) // 3) * 3 + 1, day=1)
        return (q_start - pd.Timedelta(days=1)).normalize()
    if f == "Y":
        return pd.Timestamp(year=t.year - 1, month=12, day=31)
    return None


# 事件型表的刷新间隔（天）：没有"当期"概念，只能按时间节流
REFRESH_DAYS = {"dividend": 7, "shares_history": 90, "adj_factor": 365}


def _last_synced(con: sqlite3.Connection, table: str) -> pd.Timestamp | None:
    try:
        row = con.execute("SELECT last_at FROM sync_log WHERE name=?", (table,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return pd.Timestamp(row[0]) if row and row[0] else None


def _mark_synced(con: sqlite3.Connection, table: str) -> None:
    con.execute("INSERT OR REPLACE INTO sync_log(name, last_at) VALUES (?,?)",
                (table, pd.Timestamp.now().isoformat(timespec="seconds")))
    con.commit()


def _due(con: sqlite3.Connection, table: str, today: pd.Timestamp,
         sparse_days: int = 30) -> tuple[bool, str]:
    """这张表现在该不该更新。

    低频表（月/季/年）如果已有当期数据就不跑——省时间也省 API 额度。
    """
    row = con.execute("SELECT freq, lag_days, end_date FROM metadata WHERE name=?",
                      (table,)).fetchone()
    if row is None:                 # 还没有元数据（第一次跑）
        return True, "首次"
    freq, lag, end = str(row[0] or "D"), int(row[1] or 0), row[2]

    if freq.upper() == "D":
        return True, ""
    if freq.upper() in ("SPARSE", "EVENT"):
        # 事件表：按"上次抓取时间"节流，不看数据截止日
        days = REFRESH_DAYS.get(table, sparse_days)
        last = _last_synced(con, table)
        if last is None:
            return True, "首次"
        age = (today - last.normalize()).days
        return (age >= days), (f"{age} 天前抓过（间隔 {days} 天）" if age < days else "")

    exp = _period_end(freq, today, lag)
    if exp is None:
        return True, ""
    if end and pd.Timestamp(str(end)) >= exp:
        return False, f"已覆盖 {end}（应到 {exp.date()}）"
    return True, ""

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
        only: list[str] | None = None, dry_run: bool = False,
        force: bool = False) -> pd.DataFrame:
    end = end or str(pd.Timestamp.today().date())
    picked = [s for s in SOURCES if s.enabled]
    if only:
        picked = [s for s in picked if any(t in only for t in s.table.split("|"))]

    from datahub.core.conn import connect as _connect
    con = None if dry_run else _connect()
    if con is not None:
        con.executescript(DATA_DDL)

    results = []
    today = pd.Timestamp.today().normalize()
    for s in picked:
        tables = [t.strip() for t in s.table.split("|")]

        # 低频表已经有当期数据就跳过：省时间，也省 ashareapi 的额度
        if con is not None and not force:
            due, why = _due(con, tables[0], today)
            if not due:
                print(f"  {tables[0]:22s} 跳过  {why}")
                results.append({"table": s.table, "status": "跳过", "rows": 0, "msg": why})
                continue

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

            if s.shape in ("long", "static"):
                writer = write_long if s.shape == "long" else write_static
                if dry_run:
                    print(f"  [dry] {tables[0]:22s} {s.shape:6s} {len(obj):5d} 行  {s.func.__name__}")
                    results.append({"table": s.table, "status": "dry", "rows": len(obj), "msg": ""})
                    continue
                n = writer(con, tables[0], obj)
                con.commit()
                _mark_synced(con, tables[0])
                _apply_note(con, s, tables)
                print(f"  {tables[0]:22s} {s.shape:6s} +{n:5d} 行  {s.func.__name__}")
                results.append({"table": s.table, "status": "ok", "rows": n, "msg": ""})
                continue

            kind = "multi" if isinstance(obj, pd.DataFrame) else "single"
            if dry_run:
                print(f"  [dry] {tables[0]:22s} {kind:6s} {len(obj):5d} 行  {s.func.__name__}")
                results.append({"table": s.table, "status": "dry", "rows": len(obj), "msg": ""})
                continue
            n = _write(con, tables[0], obj, end)
            con.commit()          # 立刻释放写锁：源函数内部可能还要读库
            _mark_synced(con, tables[0])
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
    ap.add_argument("--force", action="store_true", help="忽略频率判断，强制更新")
    a = ap.parse_args()
    df = run(start=a.start, end=a.end,
             only=[x.strip() for x in a.only.split(",")] if a.only else None,
             dry_run=a.dry_run, force=a.force)
    print("\n汇总：")
    print(df.groupby("status").size().to_string())

# ======================================================================
# 写库：long / static。表结构（列名+类型）在 read.py，那里是唯一出处。
# ======================================================================
def _ddl_long(table: str) -> str:
    cols = ", ".join(f"{c} {t}" for c, t in LONG_SCHEMAS[table].items())
    return (f'CREATE TABLE IF NOT EXISTS "{table}" (\n'
            f'    code TEXT NOT NULL,\n    date TEXT NOT NULL,\n'
            f'    {cols},\n    PRIMARY KEY (code, date))\n')


def _ddl_static(table: str) -> str:
    cols = ", ".join(f"{c} {t}" for c, t in STATIC_SCHEMAS[table].items())
    return (f'CREATE TABLE IF NOT EXISTS "{table}" (\n'
            f'    code TEXT PRIMARY KEY,\n    {cols}\n)')


def write_long(con: sqlite3.Connection, table: str, df: pd.DataFrame) -> int:
    """写 long 表（upsert）。df 需含 code / date 及已登记字段。"""
    con.executescript(_ddl_long(table))
    con.execute(f'CREATE INDEX IF NOT EXISTS "idx_{table}_date" ON "{table}"(date)')
    cols = _long_columns(table)
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


def write_static(con: sqlite3.Connection, table: str, df: pd.DataFrame) -> int:
    """写 static 表（按 code upsert）。"""
    con.executescript(_ddl_static(table))
    cols = _static_columns(table)
    use = ["code"] + [c for c in cols if c in df.columns]
    frame = df[use].copy()
    sql = (f'INSERT INTO "{table}" ({", ".join(use)}) '
           f'VALUES ({", ".join("?" * len(use))}) '
           f'ON CONFLICT(code) DO UPDATE SET '
           + ", ".join(f"{c}=excluded.{c}" for c in use[1:]))
    con.executemany(sql, frame.itertuples(index=False, name=None))
    con.commit()
    return len(frame)
