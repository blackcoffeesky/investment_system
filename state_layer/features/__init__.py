"""特征构造入口。"""

from __future__ import annotations

import pandas as pd

from state_layer.features import ashare, gold

BUILDERS = {"ashare": ashare.build, "gold": gold.build}


def build(asset: str, panel: pd.DataFrame) -> pd.DataFrame:
    if asset not in BUILDERS:
        raise KeyError(f"没有这个资产的特征构造：{asset}")
    return BUILDERS[asset](panel)
