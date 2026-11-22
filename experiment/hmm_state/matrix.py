"""读取基础指数面板 csi_all，返回包含 open high low close amount 的DataFrame"""
from __future__ import annotations
import pandas as pd
from datahub.port import get_port


def _index_panel() -> pd.DataFrame:
    got = get_port().series_map(["csi_all"])
    cols = {k.split(":",1)[1]:v for k, v in got.items() if k.startswith("csi_all:")}
    return pd.DataFrame(cols).sort_index()
