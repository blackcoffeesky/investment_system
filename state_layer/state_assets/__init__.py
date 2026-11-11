"""状态资产包：每个资产在这里声明"我要哪些表、怎么拼面板、怎么算特征"。

对外只有一个入口：

    from state_layer import state_assets
    pkg = state_assets.load("ashare")
    pkg.REQUIRED_TABLES   # 需要 datahub 提供的表
    pkg.panel(series, meta)
    pkg.features.build(panel)
    pkg.config.K / FEATURES / EXTREME_PARAMS / name_states

状态层**不依赖因果层**：面板、特征、标签词汇都在这里，数据从 datahub 取。
"""

from __future__ import annotations

import importlib


def load(asset: str):
    """返回资产包的命名空间（config + panel + features + REQUIRED_TABLES）。"""
    mod = importlib.import_module(f"state_layer.state_assets.{asset}")
    cfg = mod.config

    class _Pkg:
        ASSET = asset
        config = cfg
        features = mod.features
        panel = staticmethod(mod.panel.panel)
        REQUIRED_TABLES = getattr(mod.panel, "REQUIRED_TABLES", ())

        # 常用配置代理，省得总写 pkg.config.X
        K = cfg.K
        FEATURES = cfg.FEATURES

    return _Pkg


AVAILABLE = ("ashare", "gold")
