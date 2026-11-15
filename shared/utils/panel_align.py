"""兼容 shim：本模块已改名 `shared.utils.time_align`（2026-11-15）。

改名理由：职责只有"纯时间操作"（lag 推后、日频对齐、低频聚合），
不再有"面板"这层含义。旧路径继续转发，现有 import 一行不用改。

⚠️ **默认值变了**：`to_daily(..., ffill=)` 从 True 改成 **False**，
`build(..., policy=)` 默认 `"raw"`。直接调这两个函数的旧代码要显式传
`ffill=True` / `policy={...: "daily"}`——这是刻意的（见 time_align 的模块注释）。
"""

from shared.utils.time_align import (POLICIES, build, daily_index, get, lag_of,
                                     raw, to_daily, to_low_freq)

__all__ = ["get", "lag_of", "raw", "to_daily", "to_low_freq", "daily_index",
           "build", "POLICIES"]
