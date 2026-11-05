"""状态机：调度、消息、确认。"""

from __future__ import annotations

from enum import Enum


class AckAction(str, Enum):
    """用户对一条消息的三选一。"""
    RETRY = "retry"
    SKIP = "skip"
    ACK_ONLY = "ack"


# 每个级别的消息允许哪些操作
# ERROR 不能"跳过"——数据不可用还往下走，等于自欺；也不能只是"知道了"
ALLOWED_ACTIONS = {
    "error": {AckAction.RETRY},
    "warning": {AckAction.RETRY, AckAction.SKIP, AckAction.ACK_ONLY},
    # INFO 是"知道了就行"：状态已经是 DONE，任何改状态的操作都会破坏 prefix 判定
    "info": {AckAction.ACK_ONLY},
}
