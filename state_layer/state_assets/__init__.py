"""状态资产包：每个资产在这里声明"我要哪些表、怎么算特征"。

对外入口：

    from state_layer import state_assets
    pkg = state_assets.load("ashare")
    pkg.REQUIRED_TABLES                      # 该资产要哪些表（= config.REQUIRED_TABLES）
    pkg.config.K / FEATURES / EXTREME_PARAMS / name_states
    pkg.features.build(get_data(state_assets.panel_of("ashare")))

面板数据由耦合层 `shared.adapter` 统一提供（2026-11-15 各模块的 `panel.py` 退役）：
资产包只声明配置与特征，不再自己拼面板，也就不会随数据层改表名而漂。

状态层**不依赖因果层**：配置、特征、标签词汇都在这里，数据从 adapter 取。
"""

from __future__ import annotations

import importlib

from shared.adapter import Panel


def load(asset: str):
    """返回资产包的命名空间（config + features + REQUIRED_TABLES）。"""
    mod = importlib.import_module(f"state_layer.state_assets.{asset}")
    cfg = mod.config

    class _Pkg:
        ASSET = asset
        config = cfg
        features = mod.features
        # 刻意**不留默认值**：资产包漏写就 AttributeError 当场炸。
        # 留 `getattr(..., ())` 会让 `required_tables()` 静默少几张表（DESIGN：失败要响）。
        REQUIRED_TABLES = tuple(cfg.REQUIRED_TABLES)

    return _Pkg


AVAILABLE = ("ashare", "gold")


def panel_of(asset: str) -> Panel:
    """资产 → 耦合层的 Panel。状态层只用 `STATE_*` 这一组。"""
    key = f"STATE_{asset.upper()}"
    try:
        return Panel[key]
    except KeyError:
        raise KeyError(f"{asset!r} 没有对应的状态面板；可选：{list(AVAILABLE)}") from None
