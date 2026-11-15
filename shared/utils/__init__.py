"""跨模块共享的**纯工具**。

规则（与 `shared/__init__.py` 同一条）：
1. 只放纯函数——不读库、不碰文件、不 import 任何业务模块
2. 各模块可以 import 这里，反过来不行
3. 一件事只有一份实现：面板对齐、日历对齐、标准化
"""

from shared.utils import calendar, data_processing, panel_align

__all__ = ["calendar", "data_processing", "panel_align"]
