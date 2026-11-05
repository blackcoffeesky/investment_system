"""状态基类。每个日常动作 = 一个状态子类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from service.ops.context import Context
    from service.ops.messages import StateResult


class BaseState(ABC):
    """抽象状态。

    子类只需声明三件事（name / prefix / label）并实现三个方法。
    **不要**在 run() 里写持久化或消息投递——那是 Manager 的职责。
    """

    name: str = ""                      # 唯一名，如 "fetch"
    prefix: tuple[str, ...] = ()        # 前置状态；全部 done/skipped 才可运行
    label: str = ""                     # 展示名
    # 用户点"跳过"时的默认静音方式：once / days:N / period_change
    # 按状态声明，不设全局默认——不同状态的"再提醒一次"含义不同
    default_snooze: str = "days:7"

    @abstractmethod
    def start(self, ctx: "Context") -> None:
        """进入（打日志、准备资源）。不应有副作用地改数据。"""

    @abstractmethod
    def run(self, ctx: "Context") -> "StateResult":
        """执行。返回结果，不自己记消息。"""

    @abstractmethod
    def end(self, ctx: "Context") -> None:
        """退出（清理）。无论 run 成功失败都会调用。"""

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} {self.name}>"
