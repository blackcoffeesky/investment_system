"""SQLite 表结构。两个命名空间分开定义。

- `DATA_DDL`：金融数据（metadata / missing / 各序列表）
- `OPS_DDL` ：运维状态（ops_state / ops_messages）

它们共用一个 sqlite 文件（为了"状态 + 消息"的单事务原子写），但**语义隔离**：
`fin.tables()` 只认 metadata 里登记过的表，ops_* 永不进 metadata。
"""

from __future__ import annotations

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
    lag_days    INTEGER DEFAULT 0     -- 公布滞后天数：数据期结束后多少天才可见
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
