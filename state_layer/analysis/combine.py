"""组的组合：把两个状态分组叠成一个二维格（九宫格）。

**这是"组合"的唯一实现**（2026-11-22 约定）。`web/routes.py`、页面、将来的
causual 用法都必须调这里，不许各拼一次——两份实现对不上时最难查。

纯函数：不读库、不写盘、不 import 资产包。
"""

from __future__ import annotations

import pandas as pd


def grid(price: pd.Series, vol: pd.Series,
         price_order: tuple[str, ...] = (),
         vol_order: tuple[str, ...] = ()) -> pd.Series:
    """价格档 × 波动档 → 形如 `"1-2"` 的格标签。

    参数
    ----
    price / vol : 逐日**标签**序列（不是状态编号——编号重训会换位）
    price_order / vol_order : 该组标签词表的顺序；给了就输出序号，不给就输出
        `"下跌-高波"` 这种可读串。

    返回
    ----
    Series，**index = 两个序列的交集**（各自区间不同，取交集才可比），
    name = `"grid"`。任一侧缺标签的日子被丢掉（不补）。
    """
    both = pd.concat([price.rename("p"), vol.rename("v")], axis=1, join="inner")
    both = both.dropna()
    if both.empty:
        return pd.Series(dtype=object, name="grid")

    def code(s: pd.Series, order: tuple[str, ...]) -> pd.Series:
        if not order:
            return s.astype(str)
        pos = {lab: i for i, lab in enumerate(order)}
        return s.map(lambda x: str(pos.get(str(x), str(x))))

    pcol = both.iloc[:, 0]
    vcol = both.iloc[:, 1]
    out = code(pcol, price_order) + "-" + code(vcol, vol_order)
    return out.rename("grid")


def grid_legend(price_order: tuple[str, ...], vol_order: tuple[str, ...],
                price_names: dict[str, str] | None = None,
                vol_names: dict[str, str] | None = None) -> list[dict]:
    """九宫格图注：把 `"i-j"` 翻译成人话（页面用，纯函数）。"""
    pn = price_names or {}
    vn = vol_names or {}
    return [
        {"code": f"{i}-{j}",
         "text": f"{pn.get(p, p)} × {vn.get(v, v)}"}
        for i, p in enumerate(price_order)
        for j, v in enumerate(vol_order)
    ]
