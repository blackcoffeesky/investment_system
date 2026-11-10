"""消息与状态结果：状态机唯一的对外表达。

三级消息，行为完全不同：

| 级别 | 行为 | 用户操作 |
|---|---|---|
| ERROR | 阻塞状态机 | 手动修复后确认 → **重跑该状态** |
| WARNING | 可跳过 | 确认后 → 标记 skipped_by_ack，继续 |
| INFO | 自动继续 | 仅通知 |
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class MessageLevel(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class StateStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    BLOCKED = "blocked"            # 被 ERROR 卡住
    WAITING_ACK = "waiting_ack"    # 等用户确认（WARNING）
    WARNED = "warned"                   # 有告警但不阻塞后续状态
    SNOOZED = "snoozed"                 # 已检查但警告被静音；下次仍会重新检查


# 算作"可以继续往下走"的状态
PASSABLE = {StateStatus.DONE, StateStatus.SNOOZED, StateStatus.WARNED}
PASSABLE_VALUES = {s.value for s in PASSABLE}


def new_id(prefix: str = "m") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@dataclass
class Message:
    level: MessageLevel
    text: str
    state: str = ""
    key: str = ""          # 静音标识（如 cb_holdings）
    period: str = ""       # 该警告对应的数据期
    blocking: bool = True  # 是否阻塞后续状态（数据源抖动不阻塞）
    id: str = field(default_factory=lambda: new_id("m"))
    run_id: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    acked_at: str | None = None
    ack_by: str | None = None

    @property
    def acked(self) -> bool:
        return self.acked_at is not None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "run_id": self.run_id, "level": self.level.value,
            "text": self.text, "state": self.state, "created_at": self.created_at,
            "acked_at": self.acked_at, "ack_by": self.ack_by,
            "ack_action": getattr(self, "ack_action", None),
            "key": self.key, "period": self.period,
            "blocking": self.blocking,
        }


@dataclass
class StateResult:
    """一个状态跑完的结果。`messages` 可以有多个（比如同时报 WARNING 和 INFO）。"""
    messages: list[Message] = field(default_factory=list)

    @classmethod
    def ok(cls, text: str = "", state: str = "", *, key: str = "") -> "StateResult":
        r = cls()
        if text:
            r.messages.append(Message(MessageLevel.INFO, text, state, key=key))
        return r

    @classmethod
    def warn(cls, text: str, state: str = "", *, key: str = "",
             period: str = "", blocking: bool = True) -> "StateResult":
        r = cls()
        r.messages.append(Message(MessageLevel.WARNING, text, state,
                                  key=key, period=period, blocking=blocking))
        return r

    @classmethod
    def error(cls, text: str, state: str = "") -> "StateResult":
        r = cls()
        r.messages.append(Message(MessageLevel.ERROR, text, state))
        return r

    @property
    def level(self) -> MessageLevel | None:
        """取最高级别：有 ERROR 就是 ERROR，其次 WARNING，否则 INFO。"""
        levels = {m.level for m in self.messages}
        if MessageLevel.ERROR in levels:
            return MessageLevel.ERROR
        if MessageLevel.WARNING in levels:
            return MessageLevel.WARNING
        return MessageLevel.INFO if levels else None

    @property
    def error_message(self) -> Message | None:
        return next((m for m in self.messages if m.level == MessageLevel.ERROR), None)

    @property
    def warn_message(self) -> Message | None:
        return next((m for m in self.messages if m.level == MessageLevel.WARNING), None)
