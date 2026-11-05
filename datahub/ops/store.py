"""运维状态存储。

**只碰 `ops_*` 表**，不 import service（消息以 dict 传入）。
与金融数据共用同一个 sqlite 文件，理由只有一个：状态和消息要能**一个事务落盘**。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from datahub import DB_PATH
from datahub.core.conn import connection, connect, transaction
from datahub.core.errors import OpsTableAccess
from datahub.core.schema import OPS_DDL

# 允许操作的运维表，其余一律拒绝（防止误把 ops 当金融表写）
_ALLOWED = {"ops_state", "ops_messages"}


def _guard(name: str) -> None:
    if name not in _ALLOWED:
        raise OpsTableAccess(f"{name} 不是运维表")


class OpsStore:
    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path or DB_PATH)
        # DDL 只执行一次（旧实现每次连接都 executescript，纯浪费）
        with connection(self.db_path) as con:
            con.executescript(OPS_DDL)
            con.commit()

    # ---------------- 读
    def load_all(self) -> dict[str, dict]:
        with connection(self.db_path) as con:
            rows = con.execute("SELECT * FROM ops_state").fetchall()
        return {r["state"]: dict(r) for r in rows}

    def state(self, name: str) -> dict | None:
        with connection(self.db_path) as con:
            row = con.execute("SELECT * FROM ops_state WHERE state=?", (name,)).fetchone()
        return dict(row) if row else None

    def messages(self, state: str | None = None, run_id: str | None = None,
                 unacked_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM ops_messages"
        cond, args = [], []
        if state:
            cond.append("state=?")
            args.append(state)
        if run_id:
            cond.append("run_id=?")
            args.append(run_id)
        if unacked_only:
            cond.append("acked_at IS NULL")
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY created_at"
        with connection(self.db_path) as con:
            return [dict(r) for r in con.execute(sql, args).fetchall()]

    # ---------------- 写：状态 + 消息，一个事务
    def save_result(self, state: str, status: str, *,
                    messages: Iterable[dict] = (),
                    attempts: int | None = None,
                    blocking_msg_id: str | None = None,
                    payload: dict | None = None,
                    touch: bool = True) -> None:
        """**单事务**写入：状态行 + 该次产生的所有消息。

        旧实现是 add_messages 和 upsert_state 两次 commit，中间崩溃会留下
        "消息有了、状态没更新"的孤儿数据。
        """
        now = datetime.now().isoformat(timespec="seconds")
        with transaction(self.db_path) as con:
            con.execute(
                "INSERT INTO ops_state(state, status, last_run_at, attempts,"
                " blocking_msg_id, payload) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(state) DO UPDATE SET"
                "   status=excluded.status,"
                "   last_run_at=CASE WHEN ? THEN excluded.last_run_at"
                "                     ELSE ops_state.last_run_at END,"
                "   attempts=COALESCE(?, ops_state.attempts),"
                "   blocking_msg_id=excluded.blocking_msg_id,"
                "   payload=COALESCE(excluded.payload, ops_state.payload)",
                (state, status, now, attempts or 0, blocking_msg_id,
                 json.dumps(payload, ensure_ascii=False) if payload else None,
                 1 if touch else 0, attempts),
            )
            rows = []
            for m in messages:
                rows.append((
                    m["id"], m.get("run_id"), m.get("state", state), m["level"],
                    m["text"], m.get("created_at") or now,
                    m.get("acked_at"), m.get("ack_by"), m.get("ack_action"),
                    m.get("key"), m.get("period"),
                ))
            if rows:
                # 同一 (state, key) 的旧未确认消息作废，避免页面上堆同样的告警
                for m in messages:
                    k = m.get("key")
                    if not k:
                        continue
                    con.execute(
                        "UPDATE ops_messages SET acked_at=?, ack_by='superseded',"
                        " ack_action='superseded'"
                        " WHERE state=? AND key=? AND acked_at IS NULL AND id<>?",
                        (now, m.get("state", state), k, m["id"]))
                con.executemany(
                    "INSERT OR REPLACE INTO ops_messages"
                    "(id, run_id, state, level, text, created_at, acked_at,"
                    " ack_by, ack_action, key, period) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    rows)

    def set_status(self, state: str, status: str, *,
                   blocking_msg_id: str | None = None,
                   attempts: int | None = None,
                   last_attempts: int | None = None,
                   touch: bool = False) -> None:
        """只改状态行（用于恢复、确认后的状态迁移）。"""
        now = datetime.now().isoformat(timespec="seconds")
        with transaction(self.db_path) as con:
            con.execute(
                "INSERT INTO ops_state(state, status, last_run_at, attempts,"
                " last_attempts, blocking_msg_id) VALUES (?,?,?,COALESCE(?,0),"
                " COALESCE(?,0),?)"
                " ON CONFLICT(state) DO UPDATE SET"
                "   status=excluded.status,"
                "   last_run_at=CASE WHEN ? THEN excluded.last_run_at"
                "                     ELSE ops_state.last_run_at END,"
                "   attempts=COALESCE(?, ops_state.attempts),"
                "   last_attempts=COALESCE(?, ops_state.last_attempts),"
                "   blocking_msg_id=excluded.blocking_msg_id",
                (state, status, now, attempts, last_attempts, blocking_msg_id,
                 1 if touch else 0, attempts, last_attempts),
            )

    def ack_message(self, msg_id: str, *, by: str = "user",
                    action: str | None = None) -> dict | None:
        now = datetime.now().isoformat(timespec="seconds")
        with transaction(self.db_path) as con:
            row = con.execute("SELECT * FROM ops_messages WHERE id=?", (msg_id,)).fetchone()
            if row is None:
                return None
            con.execute(
                "UPDATE ops_messages SET acked_at=?, ack_by=?, ack_action=? WHERE id=?",
                (now, by, action, msg_id))
            out = dict(row)
            out.update({"acked_at": now, "ack_by": by, "ack_action": action})
            return out


    # ---------------- 静音
    def snooze(self, state: str, key: str, mode: str, *,
               remind_at: str | None = None, period: str | None = None) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with transaction(self.db_path) as con:
            con.execute(
                "INSERT OR REPLACE INTO ops_snooze"
                "(state, key, mode, remind_at, period, created_at) VALUES (?,?,?,?,?,?)",
                (state, key, mode, remind_at, period, now))

    def snoozes(self, state: str | None = None) -> list[dict]:
        sql = "SELECT * FROM ops_snooze"
        args: list = []
        if state:
            sql += " WHERE state=?"
            args.append(state)
        sql += " ORDER BY created_at"
        with connection(self.db_path) as con:
            return [dict(r) for r in con.execute(sql, args).fetchall()]

    def clear_snooze(self, state: str, key: str) -> None:
        with transaction(self.db_path) as con:
            con.execute("DELETE FROM ops_snooze WHERE state=? AND key=?", (state, key))

    def stale_snoozes(self, days: int = 90) -> list[dict]:
        """静音超过 N 天的条目（运维页当待办展示，不弹告警）。"""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with connection(self.db_path) as con:
            rows = con.execute(
                "SELECT * FROM ops_snooze WHERE created_at < ? ORDER BY created_at",
                (cutoff,)).fetchall()
        return [dict(r) for r in rows]


    # ---------------- 运行日志（看门狗）
    def log_run_start(self, run_id: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with transaction(self.db_path) as con:
            con.execute(
                "INSERT OR REPLACE INTO ops_run_log(run_id, started_at, status)"
                " VALUES (?,?,?)", (run_id, now, "running"))

    def log_run_end(self, run_id: str, status: str, summary: str = "") -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with transaction(self.db_path) as con:
            con.execute(
                "UPDATE ops_run_log SET finished_at=?, status=?, summary=?"
                " WHERE run_id=?", (now, status, summary, run_id))

    def last_run(self) -> dict | None:
        with connection(self.db_path) as con:
            row = con.execute(
                "SELECT * FROM ops_run_log ORDER BY started_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None
