"""连接与事务：所有 sqlite 操作的唯一入口。

为什么要统一：
- 连接超时（默认 5 秒太短，源函数内部还要读库）
- 事务边界（"状态 + 消息"必须一起成功或一起失败）
- DDL 只跑一次（不要每次连接都 executescript）
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from datahub import DB_PATH


def connect(path: Path | None = None, timeout: float = 30.0) -> sqlite3.Connection:
    """连接 + 统一 PRAGMA。

    WAL：让"一个写、多个读"能并存（后台抓数时页面仍可打开）。
    busy_timeout：拿不到锁时等待，而不是立刻抛 database is locked。
    """
    con = sqlite3.connect(str(path or DB_PATH), timeout=timeout)
    con.row_factory = sqlite3.Row
    # busy_timeout 先设：后面所有语句（包括切 journal_mode）都要等锁
    con.execute("PRAGMA busy_timeout=30000")
    # journal_mode 是库级属性，已经是 WAL 就别再切——切换需要独占锁，
    # 别的连接正在写时这一步会卡住甚至报 database is locked。
    try:
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() != "wal":
            con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass            # 别人正拿着写锁；库已经是 WAL，跳过即可
    con.execute("PRAGMA synchronous=NORMAL")
    return con


@contextmanager
def transaction(path: Path | None = None, timeout: float = 15.0):
    """一个事务。正常退出提交，异常回滚。

    sqlite3 的 autocommit 行为容易踩坑，这里显式 BEGIN/COMMIT。
    """
    con = connect(path, timeout)
    try:
        con.execute("BEGIN")
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


@contextmanager
def connection(path: Path | None = None, timeout: float = 15.0):
    """只读/单语句用。不自动开事务。"""
    con = connect(path, timeout)
    try:
        yield con
    finally:
        con.close()


def exec_ddl(con: sqlite3.Connection, ddl: str) -> None:
    con.executescript(ddl)
    con.commit()
