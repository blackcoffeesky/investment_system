"""跨模块共享的**纯工具**。

规则（与 `shared/__init__.py` 同一条）：
1. 只放纯函数——不读库、不碰文件、不 import 任何业务模块
2. 各模块可以 import 这里，反过来不行
3. 一件事只有一份实现：时间对齐、日历对齐、标准化、派生特征

模块清单
--------
- `time_align`     lag 推后 / 日频对齐 / 低频聚合（2026-11-15 由 panel_align 改名）
- `panel_align`    **兼容 shim**，转发 time_align
- `data_processing` 标准化、滚动 z
"""

from shared.utils import data_processing, time_align

__all__ = ["data_processing", "time_align"]
