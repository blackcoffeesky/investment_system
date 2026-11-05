"""资产插件注册表。

每个资产是一个子包，必须提供 `pipeline.py`，其中导出：
    ASSET / TITLE / TARGET / LABEL_COL / LABELS / LABEL_NAME / EDGES
    build_panel(asof) -> (df, spec)
    fit_edges(df, spec, seed) -> {models, path}
    make_card(df, spec, models, when) -> str

新增资产 = 新建一个目录 + 在 AVAILABLE 里登记，核心代码不用改。
"""

from __future__ import annotations

import importlib

AVAILABLE = ("gold", "ashare")


def load(asset: str):
    if asset not in AVAILABLE:
        raise KeyError(f"未知资产 {asset!r}；可选：{AVAILABLE}")
    return importlib.import_module(f"causual.causual_assets.{asset}.pipeline")
