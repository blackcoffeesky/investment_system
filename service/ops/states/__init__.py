"""状态注册表。**一个模块一个文件**，这里只负责列出所有状态。

加新模块时：
1. 在 `states/` 下新建 `<module>.py`（里面放该模块的全部状态）
2. 在这里 import 并加进 `STATES`
"""

from __future__ import annotations

from service.ops.base_state import BaseState
from service.ops.states.causal import GenerateCardsState, GenerateMacroState
from service.ops.states.data import FetchState, ValidateState

# 将来：
# from service.ops.states.causal import CausalCardState

STATES: list[BaseState] = [
    FetchState(),
    ValidateState(),
    GenerateCardsState(),
    GenerateMacroState(),
]

__all__ = ["STATES", "FetchState", "ValidateState",
           "GenerateCardsState", "GenerateMacroState"]
