"""通用命名工具：**按某列的序位**把状态编号映射到固定词表。

序位命名是 A 股三组的命名方式（2026-11-22）：训练集画像按 `order_col` 升序排，
第 0 名 → 词表第 0 个……重训不会换语义（编号本来就会换位，标签不会）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def order_labeler(column: str, labels: tuple[str, ...]):
    """按 `column` 从小到大把状态编号映射到固定词表（0 = 最弱 / 最低波）。

    这是新方案（2026-11-22）的命名方式：**序位命名**。序位由训练集画像定，
    不需要"对齐到参照均值"（所以 `make_labeler` 的匈牙利匹配退役了）。
    """

    def _label(raw: pd.DataFrame) -> dict[int, str]:
        s = raw[column].to_numpy(dtype=float)
        order = np.argsort(s, kind="stable")
        return {int(idx): str(labels[min(pos, len(labels) - 1)])
                for pos, idx in enumerate(order)}

    return _label


