"""兼容 shim：本模块已改名 `shared.utils.time_align`（2026-11-15）。

职责只有"纯时间操作"（lag 推后、日频对齐、低频聚合），不再有"面板"含义。
旧路径继续转发；新代码请用 `from shared.utils.time_align import ...`。

⚠️ **默认值变了**：`to_daily` 的 `ffill` 默认 False、`build` 的 `policy` 默认 "raw"。
旧调用要日频填充必须显式写 `ffill=True` / `policy={...: "daily"}`。
"""

from shared.utils.time_align import (POLICIES, build, daily_index, get, lag_of,
                                     raw, to_daily, to_low_freq)

__all__ = ["get", "lag_of", "raw", "to_daily", "to_low_freq", "daily_index",
           "build", "POLICIES"]
