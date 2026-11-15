"""跨模块共享的**纯工具**。

规则：
1. 这里只放纯函数——不读库、不碰文件、不 import 任何业务模块
2. 各模块可以 import shared，反过来不行
3. 一件事只有一份实现：时间对齐、日历对齐、标准化、派生特征

⚠️ **绝对不要在这里 import `shared.adapter`**：adapter 会拖进 `datahub` + sqlite，
于是任何 `import shared`（连纯函数调用）都会打开数据库模块。需要取数的消费方
自己 `from shared.adapter import get_data`，那是显式的第二步。

`features` 也是纯函数，但体积大、依赖广，按需 import（`from shared.utils import features`）。
"""

from shared.utils import calendar, data_processing, panel_align, time_align

__all__ = ["calendar", "data_processing", "panel_align", "time_align"]
