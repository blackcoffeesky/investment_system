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
    lag_days    INTEGER DEFAULT 0,    -- 公布滞后天数：数据期结束后多少天才可见
    source      TEXT DEFAULT 'raw',   -- raw / derived：原始数据，还是由别的表算出来的
    depends     TEXT                  -- 派生表的依赖（原生表名，逗号分隔）；raw 留空
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


# 老库补列（新库由 METADATA_DDL 直接建好）。SQLite 的 ADD COLUMN 带 DEFAULT 时
# 会把已有行填成默认值，所以 source 老行自然是 'raw'——它们本来就是原始表。
METADATA_MIGRATIONS: dict[str, str] = {
    "source": "ALTER TABLE metadata ADD COLUMN source TEXT DEFAULT 'raw'",
    "depends": "ALTER TABLE metadata ADD COLUMN depends TEXT",
}

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


def migrate_metadata(con: sqlite3.Connection) -> list[str]:
    """给已有库补 metadata 的新列、并把空串日期统一成 NULL。幂等：没有要改的就返回 [].

    为什么空串要转 NULL：`''` 会让下游的 `isna()` 漏过去（static 表的
    start_date/end_date 就是空串），排序时 `''` 又会排在最前，都是暗坑。

    返回这次做了哪些改动，供调用方打日志。
    """
    if not _table_exists(con, "metadata"):
        return []                     # 全新的库：DATA_DDL 会直接建好，不用迁移
    done = _add_columns(con, "metadata", METADATA_MIGRATIONS)
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
"""
