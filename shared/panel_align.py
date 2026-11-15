"""兼容 shim：实现已移到 `shared.utils.panel_align`。

2026-11-14 起 `shared/` 拆成 `utils/`（纯工具）+ `adapter/`（取数耦合层）。
这里保留旧路径转发，**四个 panel.py 与 experiment/ 的现有 import 都不用改**；
新代码请用 `from shared.utils.panel_align import ...`。
"""

from shared.utils.panel_align import (build, daily_index, get, lag_of, to_daily)

__all__ = ["get", "lag_of", "to_daily", "daily_index", "build"]
