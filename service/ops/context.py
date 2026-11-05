"""状态上下文：状态运行期间能拿到的所有东西。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Context:
    data_service: Any                      # service.data_service.DataService
    logger: Any = None                     # logging.Logger
    extra: dict[str, Any] = field(default_factory=dict)

    # 单次运行内由 Manager 注入
    state_name: str = ""

    def __post_init__(self) -> None:
        if self.logger is None:
            import logging

            self.logger = logging.getLogger("service.ops")
