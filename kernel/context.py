"""状态运行期的上下文与端口集合。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ServiceBag:
    """按模块名取 port。

    缺端口在**取的时候**就报错——而不是让状态跑到一半才发现
    `ctx.extra.get("cards")` 是 None（那个 bug 真的发生过）。
    """

    def __init__(self, ports: dict[str, Any] | None = None):
        self._ports: dict[str, Any] = dict(ports or {})

    def __getitem__(self, name: str) -> Any:
        if name not in self._ports:
            have = "、".join(sorted(self._ports)) or "（无）"
            raise KeyError(
                f"模块 {name} 没有暴露 service_port。"
                f"现有端口：{have}。检查该模块 register 时的 service_port_factory。")
        return self._ports[name]

    def get(self, name: str, default: Any = None) -> Any:
        return self._ports.get(name, default)

    def __contains__(self, name: str) -> bool:
        return name in self._ports

    def names(self) -> list[str]:
        return sorted(self._ports)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ServiceBag {self.names()}>"


@dataclass
class Context:
    """状态运行期间能拿到的所有东西。"""

    services: ServiceBag = field(default_factory=ServiceBag)
    logger: Any = None
    state_name: str = ""

    def __post_init__(self) -> None:
        if self.logger is None:
            import logging

            self.logger = logging.getLogger("service.ops")

    @property
    def data(self):
        """数据层端口的快捷方式（几乎所有状态都要用）。"""
        return self.services["datahub"]
