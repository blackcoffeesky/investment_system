"""状态标签词汇表。

标签（trend_up / range / trend_down / extreme_*）是**状态层**的词汇：
它定义"市场有哪几种状态"，与因果层的边无关。因果层只是消费这些标签。
"""

from state_layer.labels import ashare, gold

BY_ASSET = {"ashare": ashare, "gold": gold}


def get(asset: str):
    if asset not in BY_ASSET:
        raise KeyError(f"没有这个资产的标签定义：{asset}")
    return BY_ASSET[asset]
