"""SQLite 表结构。两个命名空间分开定义。

- `DATA_DDL`：金融数据（metadata / missing / 各序列表）
- `OPS_DDL` ：运维状态（ops_state / ops_messages）
- `METADATA_MIGRATIONS` / `migrate_metadata`：老库补列 + 空串日期转 NULL（改表也只能有这一处）

它们共用一个 sqlite 文件（为了"状态 + 消息"的单事务原子写），但**语义隔离**：
`fin.tables()` 只认 metadata 里登记过的表，ops_* 永不进 metadata。
"""

from __future__ import annotations

import sqlite3

# ---------------------------------------------------------------- 金融数据
#: **非标准形态的表在这里登记**，不要靠列名猜。
#: 猜错过：`finances` 是 EAV（`thscode, report, abilities, index_id, value`），
#: 因为列不等于 `["date","value"]` 被当成 `multi`，于是 `check` 去 `read` 它 →
#: `DatabaseError` → **整条 validate 流水线被 block**（2026-11-17 服务器事故）。
#:
#: 加新表时：形态不是 single/multi/long/static 的，**先在这里登记**，
#: 否则默认路径一定假设它可 `read`。
EAV_TABLES: dict[str, dict[str, object]] = {
    "finances": {
        "pk": ("thscode", "report", "abilities", "index_id"),
        "entity": "thscode",     # 覆盖度：有多少只
        "period": "report",      # 覆盖度/新鲜度：报表期（"2026-3"）
    },
}

# 老库补列（新库由 DATA_DDL 直接建好）。
# 2026-11-16：`source` / `depends` 已**废弃**（派生量不进 metadata），改成"删列"，
# 见 `METADATA_DROPS`。这里不再新增列。
METADATA_MIGRATIONS: dict[str, str] = {}

#: 要从老库里**删掉**的 metadata 列（幂等：列不在就跳过）。
#: SQLite 3.35+ 支持 `ALTER TABLE ... DROP COLUMN`（本机 3.53）。
METADATA_DROPS: tuple[str, ...] = ("source", "depends")


DATA_DDL = """
CREATE TABLE IF NOT EXISTS metadata (
    name        TEXT PRIMARY KEY,
    display     TEXT,
    freq        TEXT,
    unit        TEXT,
    start_date  TEXT,
    end_date    TEXT,
    n_rows      INTEGER,
    note        TEXT,
    kind        TEXT DEFAULT 'single',
    columns     TEXT,
    market      TEXT DEFAULT 'CN',    -- CN / US / NONE：判断"哪些日子本该有数据"
    lag_days    INTEGER DEFAULT 0     -- 公布滞后天数：数据期结束后多少天才可见
    -- 2026-11-16 删掉 source / depends：派生量不进 metadata（它们不落表、没有 sync_log），
    -- metadata 只管库里的表。派生量的依赖写在 `fin/derive.DERIVED` 的 depends/depends_note 里。
);

-- 上次成功抓取时间：事件型表（分红/股本）没有"当期"概念，只能按时间间隔节流
CREATE TABLE IF NOT EXISTS sync_log (
    name     TEXT PRIMARY KEY,
    last_at  TEXT
);

CREATE TABLE IF NOT EXISTS missing (
    name      TEXT NOT NULL,
    date      TEXT NOT NULL,
    reason    TEXT,
    noted_at  TEXT,
    PRIMARY KEY (name, date)
);
"""

SERIES_DDL = """
CREATE TABLE IF NOT EXISTS "{name}" (
    date  TEXT PRIMARY KEY,
    value REAL
);
"""


# 数据表补列（列定义在 `fin/read.py` 的 LONG_SCHEMAS，这里只放"给老库补"的 DDL）
DATA_MIGRATIONS: dict[str, dict[str, str]] = {
    # 2026-11-14：dividend 并入 adj_factor，只借它这两列（其余列 adj_factor 本来就有）
    "adj_factor": {
        "report_end": "ALTER TABLE adj_factor ADD COLUMN report_end TEXT",
        "plan": "ALTER TABLE adj_factor ADD COLUMN plan TEXT",
    },
}


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _add_columns(con: sqlite3.Connection, table: str, wanted: dict[str, str]) -> list[str]:
    """给已存在的表补缺列（幂等）。表还不存在就跳过——建表由 DATA_DDL / LONG_SCHEMAS 负责。"""
    if not _table_exists(con, table):
        return []
    have = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
    done = []
    for col, ddl in wanted.items():
        if col not in have:
            con.execute(ddl)
            done.append(f"{table}.{col}")
    return done


def _drop_columns(con: sqlite3.Connection, table: str,
                  unwanted: tuple[str, ...]) -> list[str]:
    """从已存在的表里删掉废弃列（幂等：列不在就跳过）。

    SQLite 3.35+ 才支持 `ALTER TABLE ... DROP COLUMN`；更低版本只能整表重建，
    这里直接报错而不是静默跳过——否则老库会带着废弃列一直跑，谁也发现不了。
    """
    if not _table_exists(con, table):
        return []
    have = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
    done = []
    for col in unwanted:
        if col in have:
            con.execute(f'ALTER TABLE "{table}" DROP COLUMN "{col}"')
            done.append(f"drop {table}.{col}")
    return done


def migrate_metadata(con: sqlite3.Connection) -> list[str]:
    """给已有库补 metadata 的新列、删掉废弃列、并把空串日期统一成 NULL。幂等。

    为什么空串要转 NULL：`''` 会让下游的 `isna()` 漏过去（static 表的
    start_date/end_date 就是空串），排序时 `''` 又会排在最前，都是暗坑。

    返回这次做了哪些改动，供调用方打日志。
    """
    if not _table_exists(con, "metadata"):
        return []                     # 全新的库：DATA_DDL 会直接建好，不用迁移
    done = _add_columns(con, "metadata", METADATA_MIGRATIONS)
    done += _drop_columns(con, "metadata", METADATA_DROPS)
    for col in ("start_date", "end_date"):
        cur = con.execute(f"UPDATE metadata SET {col}=NULL WHERE {col}=''")
        if cur.rowcount:
            done.append(f"{col}: '' → NULL（{cur.rowcount} 行）")
    if done:
        con.commit()
    return done


def migrate_data_tables(con: sqlite3.Connection) -> list[str]:
    """给已有的**数据表**补列（`DATA_MIGRATIONS`）。幂等。"""
    done: list[str] = []
    for table, wanted in DATA_MIGRATIONS.items():
        done += _add_columns(con, table, wanted)
    if done:
        con.commit()
    return done


INDEX_DDL = 'CREATE INDEX IF NOT EXISTS "idx_{name}_date" ON "{name}"(date);'


# ---------------------------------------------------------------- 运维状态
OPS_DDL = """
CREATE TABLE IF NOT EXISTS ops_state (
    state           TEXT PRIMARY KEY,
    status          TEXT NOT NULL,
    last_run_at     TEXT,
    attempts        INTEGER DEFAULT 0,
    last_attempts   INTEGER DEFAULT 0,   -- 上一轮的尝试次数（重跑会清零 attempts）
    blocking_msg_id TEXT,
    payload         TEXT
);

CREATE TABLE IF NOT EXISTS ops_messages (
    id          TEXT PRIMARY KEY,
    run_id      TEXT,
    state       TEXT,
    level       TEXT NOT NULL,
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    acked_at    TEXT,
    ack_by      TEXT,
    ack_action  TEXT,
    key         TEXT,      -- 静音标识（如 cb_holdings）
    period      TEXT       -- 该警告对应的数据期（如 2026-06-30）
);

-- 每次运行一条：看门狗用它判断"状态机还在不在跑"
CREATE TABLE IF NOT EXISTS ops_run_log (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT,
    summary     TEXT
);

-- 消息静音：按 (state, key) 记，不依附于某条消息
CREATE TABLE IF NOT EXISTS ops_snooze (
    state       TEXT NOT NULL,
    key         TEXT NOT NULL,
    mode        TEXT NOT NULL,   -- days / period
    remind_at   TEXT,            -- days 模式：到点恢复
    period      TEXT,            -- period 模式：数据期变了才恢复
    created_at  TEXT NOT NULL,
    PRIMARY KEY (state, key)
);

-- 财务指标（Fuyao 同花顺，EAV 形态）：一行 = 一只股票 × 一个报告期 × 一个指标。
-- ⚠️ **不走 `read` / `read_long`**：没有 `code`/`date` 列，读它要按
-- (thscode, report) 或 (abilities, index_id) 直接查。`metadata.kind` 是自动猜的
-- （会显示 multi），**不代表能 read**。
-- 主键 4 列而不是 (thscode, report, index_id)：同一指标名可能出现在两个 ability 下。
CREATE TABLE IF NOT EXISTS finances (
    thscode    TEXT    NOT NULL,
    report     TEXT    NOT NULL,
    abilities  TEXT    NOT NULL,
    index_id   TEXT    NOT NULL,
    value      TEXT    DEFAULT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (thscode, report, abilities, index_id)
);
CREATE INDEX IF NOT EXISTS idx_finances_stock_report ON finances (thscode, report);
CREATE INDEX IF NOT EXISTS idx_finances_ability_index ON finances (abilities, index_id);
"""
