"""按 `SOURCES` 注册表更新各表。

    python -m datahub.fin.update                    # 全部启用项，更新到今天
    python -m datahub.fin.update --start 2024-01-01 --end 2026-10-01
    python -m datahub.fin.update --only vix,csi_all
    python -m datahub.fin.update --dry-run          # 只抓不写

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
import sys
from pathlib import Path

import pandas as pd

from datahub import DB_PATH, ROOT
from datahub.core.conn import connect
from datahub.core.schema import (DATA_DDL, EAV_TABLES, migrate_data_tables,
                                migrate_metadata)
from datahub.fin.read import (LONG_SCHEMAS, STATIC_SCHEMAS,
                              _long_columns, _static_columns)
from datahub.fin.sources import SOURCES, Source
from datahub.fin.sources.ashare import QuotaExhausted

MISSING_DIR = ROOT / "data" / "missing"

# 基础设施表：不是数据表，永远不该进 metadata。
# 漏掉一个，`fin.tables()` 就会多出一张假表（sync_log 就是这么混进来的）。
INFRA_TABLES = ("metadata", "missing", "sync_log", "sqlite_sequence")

# 自动建表时的元信息提示（不写的话会按日频猜测，过期判断会误报）
FREQ_HINT = {
    "stock_daily": ("D", "CN", 0),
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
    # 2026-11-10 新增：成交额/换手率都是 A 股日频，必须挂 SSE 日历，
    # 否则会把周末当成缺口（历史缺口 1840 天就是这么来的）
    "csi_all": ("D", "CN", 0),
    # 公司三表：低频 + 事件型
    "company": ("M", "CN", 0),          # 静态档案，每月刷
    "shares_history": ("Q", "CN", 0),   # 股本快照，季度刷
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
    # ⚠️ **不能**用 `INSERT OR REPLACE`：REPLACE = 删旧行 + 插新行，**本次没返回的列会变成
    # NULL**。2026-11-16 就是这么把 `csi_all.amount` / `turnover` 从 3503 清成 0 的
    # （`index_tx` 只回 5 列）。改成 `ON CONFLICT(date) DO UPDATE SET`：只更新本次返回的列。
    before = _column_counts(con, table, cols)
    con.executemany(
        f'INSERT INTO "{table}" (date, {col_sql}) VALUES ({ph}) '
        f'ON CONFLICT(date) DO UPDATE SET '
        + ", ".join(f'"{c}"=excluded."{c}"' for c in cols), rows)
    _warn_column_loss(con, table, before)
    return len(rows)


def write_eav(con: sqlite3.Connection, table: str, df: pd.DataFrame) -> int:
    """写 **EAV** 表（如 `finances`）：主键 `(thscode, report, abilities, index_id)`。

    为什么不复用 `write_long`：long 的主键是 `(code, date)`，而 EAV 没有日期轴、
    主键是 4 列；`value` 是 **TEXT**（接口返回的是字符串 `"89.12000000"`，
    保持原样入库，避免浮点往返丢精度）。
    """
    cols = ["thscode", "report", "abilities", "index_id", "value"]
    use = [c for c in cols if c in df.columns]
    if "value" in use:
        df = df.copy()
        df["value"] = df["value"].astype(str)
    sql = (f'INSERT INTO "{table}" ({", ".join(use)}) '
           f'VALUES ({", ".join("?" * len(use))}) '
           f'ON CONFLICT(thscode, report, abilities, index_id) DO UPDATE SET '
           + ", ".join(f"{c}=excluded.{c}" for c in use[3:]))
    con.executemany(sql, df[use].itertuples(index=False, name=None))
    con.commit()
    return len(df)


def _column_counts(con: sqlite3.Connection, table: str,
                   cols: list[str]) -> dict[str, int]:
    """各列当前的非空行数（列不存在时记 0）。写前调用，用于"列熔断"。"""
    out: dict[str, int] = {}
    for c in cols:
        try:
            out[c] = int(con.execute(f'SELECT COUNT("{c}") FROM "{table}"').fetchone()[0])
        except sqlite3.OperationalError:
            out[c] = 0
    return out


def _warn_column_loss(con: sqlite3.Connection, table: str,
                      before: dict[str, int]) -> list[str]:
    """**列熔断**：本次写入把某列从"大量有值"变成"全空"时，大声报警。

    2026-11-16：`csi_all.amount` / `turnover` 从 3503 掉到 0，**没有任何测试或 check 响过**
    （是 `state-layer-agent` 偶然看 `/state` 显示「缺」才发现的）。这种"整列被清空"
    不可能是正常业务，只可能是源缺列 / 写路径错——所以宁可吵。
    """
    n = int(con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    if n == 0:
        return []
    lost: list[str] = []
    for c, old in before.items():
        if old <= 0 or old <= n * 0.5:
            continue
        now = _column_counts(con, table, [c])[c]
        if now == 0:
            lost.append(c)
            print(f"[列熔断] {table}.{c} 非空 {old} → 0（本次写入把这一列清空了！"
                  f"源是不是没返回这一列？）", file=sys.stderr)
    return lost


def _ensure_meta(con: sqlite3.Connection) -> int:
    """给"有表但没有 metadata 行"的表补一条最小元数据。

    顺手两件事：老库补列（`migrate_metadata`）、把误登进 metadata 的基础设施表清掉。
    后者是自愈：上一次的 bug 把 `sync_log` 登记成了一张数据表，光改排除名单不够，
    已经落库的那一行得删掉，否则 `fin.tables()` / `check` 会一直带着它。
    """
    migrate_metadata(con)
    migrate_data_tables(con)
    ph = ",".join("?" * len(INFRA_TABLES))
    con.execute(f"DELETE FROM metadata WHERE name IN ({ph})", INFRA_TABLES)

    have = {r[0] for r in con.execute("SELECT name FROM metadata")}
    names = [r[0] for r in con.execute(
        f"SELECT name FROM sqlite_master WHERE type='table'"
        f" AND name NOT IN ({ph})"
        f" AND name NOT LIKE 'ops_%' AND name NOT LIKE 'sqlite_%'", INFRA_TABLES)]
    # 先纠正**已有表**的 market / lag：发布滞后和市场归属会变，
    # 但这段逻辑以前只在新表分支里跑，老表永远修不到（周末被当缺口的根因）。
    # 登记为 EAV 的表：老库可能已被猜成 multi → 就地纠正（幂等）
    for name in EAV_TABLES:
        con.execute("UPDATE metadata SET kind='eav' WHERE name=? AND kind!='eav'", (name,))

    for name, (freq, market, lag) in FREQ_HINT.items():
        if name in have:
            con.execute("UPDATE metadata SET lag_days=?, market=?, freq=?"
                        " WHERE name=?", (lag, market, freq, name))

    n = 0
    for name in names:
        if name in have:
            continue
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{name}")')]
        if name in EAV_TABLES:
            kind = "eav"
        elif name in LONG_SCHEMAS:
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
REFRESH_DAYS = {"shares_history": 90, "adj_factor": 365}


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
    """按实际表内容重写 metadata 的起止日期与行数。

    `static` 表（如 `company`）没有日期轴：只统计行数，起止日期留空。
    以前统一跑 `MIN(date)`，static 表会抛 OperationalError 被静默跳过，
    于是 `company` 的 `n_rows` 永远停在 0。
    """
    n = 0
    for name, _kind in con.execute("SELECT name, kind FROM metadata").fetchall():
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{name}")')]
        if not cols:
            continue
        try:
            if "date" in cols:
                cur = con.execute(
                    f'SELECT MIN(date), MAX(date), COUNT(*) FROM "{name}"').fetchone()
                if not cur or cur[2] == 0:
                    continue
                con.execute(
                    "UPDATE metadata SET start_date=?, end_date=?, n_rows=? WHERE name=?",
                    (cur[0], cur[1], int(cur[2]), name))
            else:
                cur = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()
                if not cur or cur[0] == 0:
                    continue
                con.execute("UPDATE metadata SET n_rows=? WHERE name=?",
                            (int(cur[0]), name))
        except sqlite3.OperationalError:
            continue
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

            # 源合法地"今天没数据"（未收盘 / 落后太多不补 / 已是最新）会返回空表。
            # 这里必须兜住：往下走 `write_long` 会拿空 DataFrame 去要 code/date 列 → KeyError。
            # **不写 sync_log**：留着重试。比如盘中跳过，收盘后再跑要能真的跑。
            if obj is None or (hasattr(obj, "__len__") and len(obj) == 0):
                print(f"  {tables[0]:22s} 跳过  本次无新数据  {s.func.__name__}")
                results.append({"table": s.table, "status": "跳过", "rows": 0,
                                "msg": "源返回空（未收盘 / 无新数据 / 落后太多不补）"})
                continue

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
                writer = (write_long if s.shape == "long"
                          else write_eav if s.shape == "eav" else write_static)
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


def backfill(table: str, *, chunk_calls: int = 10, **kw) -> dict:
    """**逐批落库**地跑一个源（只对"一次抓一批"的源有意义）。

    和 `run()` 的区别：`run()` 是"源函数全部返回后写一次"，中途被杀就颗粒无收
    （2026-11-14 一次 100 次调用、1000 只的尝试全白抓）。这里每落一批就
    `write_long` + `commit`，中断只丢最后不足一批的部分。

    续跑不靠状态：源函数本身只抓"库里还没有的 code"，重跑自然接着补。
    `kw` 透传给源函数（如 `max_calls=` / `max_codes=` / `chunk_codes=`）。
    """
    src = next((s for s in SOURCES if s.table == table), None)
    if src is None:
        raise KeyError(f"没有注册表 {table!r}；可用：{[s.table for s in SOURCES]}")

    con = connect()
    con.executescript(DATA_DDL)
    _ensure_meta(con)                    # flush 里要写 sync_log，先保证表在
    written = [0]
    chunks = [0]

    def flush(df: pd.DataFrame) -> None:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return
        written[0] += write_long(con, table, df)
        chunks[0] += 1
        # **每次落库都刷 sync_log + metadata**：中断后要能看出"跑到哪了"，
        # /data 页的行数也不能停在旧值（2026-11-15：显示 84、实际 900）。
        try:
            _mark_synced(con, table)
            refresh_metadata(con)
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] 落库后刷新元数据失败：{type(exc).__name__}: {exc}")
        print(f"  落库第 {chunks[0]} 批：累计 {written[0]} 行")

    try:
        try:
            rest = src.func(table=table, on_chunk=flush,
                            chunk_calls=chunk_calls, **src.params, **kw)
            flush(rest)                      # 收尾：最后不足一批的行
        except QuotaExhausted as exc:
            # 额度用完是确定性的：不再重试、也不让异常冒出去，
            # 把已经落库的部分留下，明天续跑（续跑只看"库里缺哪些 code"）。
            print(f"  {table}：{exc}")
            print(f"  {table}：今日额度用完，本次已落库 {written[0]} 行；明天续跑")
        if not written[0]:
            print(f"  {table}：这次没有可抓的（库里已齐 / 上游无数据 / 额度用完）")
            return {"table": table, "rows": 0, "chunks": 0}
        _apply_note(con, src, [table])
        return {"table": table, "rows": written[0], "chunks": chunks[0]}
    finally:
        # 收尾必须无条件做：数据可能已经变了（部分落库后被打断/额度用完），
        # 而 `metadata.n_rows` 和 `sync_log.last_at` 留在旧值就是"看起来有、其实是旧的"
        # （2026-11-15：/data 页显示 84 行，实际库里 900 行）。
        try:
            _ensure_meta(con)
            refresh_metadata(con)
            if written[0]:
                _mark_synced(con, table)
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] 收尾刷新失败：{type(exc).__name__}: {exc}")
        con.close()


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


# ======================================================================
# 命令行。**必须放在文件最后**：`python -m datahub.fin.update` 会在这里
# 立刻执行 `backfill()`，而 `backfill` 的 `flush` 要用上面定义的 `write_long`
# ——主块放在定义之前会 NameError（2026-11-15 踩过：`--backfill` 100 次调用
# 全白跑，因为 `write_long` 还"不存在"）。
# ======================================================================
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2000-01-01", help="起始日期")
    ap.add_argument("--end", default=None, help="截止日期（默认今天）")
    ap.add_argument("--only", default=None, help="只更新这些表，逗号分隔")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="忽略频率判断，强制更新")
    ap.add_argument("--backfill", default=None,
                    help="逐批落库地回填这张表；配合 --max-calls")
    ap.add_argument("--max-calls", type=int, default=100,
                    help="--backfill 时本轮最多打几次接口（默认 100 = 专业版日额度）")
    ap.add_argument("--chunk-calls", type=int, default=10,
                    help="--backfill 时每几次接口落一次库（默认 10）")
    a = ap.parse_args()
    if a.backfill:
        print(backfill(a.backfill, chunk_calls=a.chunk_calls, max_calls=a.max_calls))
        raise SystemExit(0)
    df = run(start=a.start, end=a.end,
             only=[x.strip() for x in a.only.split(",")] if a.only else None,
             dry_run=a.dry_run, force=a.force)
    print("\n汇总：")
    print(df.groupby("status").size().to_string())
