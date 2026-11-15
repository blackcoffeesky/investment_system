"""兼容 shim：实现已移到 `shared.utils.calendar`。

2026-11-14 起 `shared/` 拆成 `utils/`（纯工具）+ `adapter/`（取数耦合层）。
这里保留旧路径转发，**现有 import 一行都不用改**；新代码请用
`from shared.utils.calendar import ...`。
"""

from shared.utils.calendar import (AlignReport, align_to_trading_calendar,
                                   merge_shared_nodes, trading_calendar)

__all__ = ["AlignReport", "align_to_trading_calendar", "merge_shared_nodes",
           "trading_calendar"]
