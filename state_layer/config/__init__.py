"""按资产取配置。"""

from __future__ import annotations

from types import ModuleType

from state_layer.config import ashare, gold

CONFIGS: dict[str, ModuleType] = {"ashare": ashare, "gold": gold}


def get(asset: str) -> ModuleType:
    if asset not in CONFIGS:
        raise KeyError(f"没有这个资产的状态配置：{asset}（可用：{sorted(CONFIGS)}）")
    return CONFIGS[asset]
