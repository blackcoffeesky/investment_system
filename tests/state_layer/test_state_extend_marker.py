"""外推段的验收：图表必须追到面板最后一天，并**标出**哪些是外推。

2026-11-24：用户发现"数据到 11-24，图表只到 11-23"——图表读的是存盘标签，
没走 `extend`。修好后这里钉住三件事：
1. 页面最大日期 == 面板最后一天
2. 外推段有独立 class（`seg ext`）+ 虚线/半透明，不与拟合段混淆
3. extend 不改历史：重叠区间与存盘标签逐位一致
"""

from __future__ import annotations

import re
import sys

sys.path.insert(0, ".")

from fastapi.testclient import TestClient

from service.app import app
from shared.adapter import get_data
from state_layer import state_assets, store
from state_layer.port import get_port


def _panel():
    return get_data(state_assets.panel_of("ashare"))


def test_chart_reaches_panel_last_day():
    panel = _panel()
    last = str(panel.index[-1].date())
    page = TestClient(app).get("/state?asset=ashare").text
    assert last in page, f"页面里没有面板最后一天 {last}"
    assert "外推" in page, "图注里没有说明外推"


def test_extrapolated_segments_are_marked():
    page = TestClient(app).get("/state?asset=ashare").text
    assert page.count('class="seg ext"') >= 1, "外推段没有独立 class"
    ext = re.findall(r'class="seg ext"[^>]*stroke-dasharray="3 2"[^>]*opacity="0.5"', page)
    assert ext, "外推段没有虚线/半透明标记"


def test_extend_does_not_change_history():
    """重叠区间必须逐位一致（防止 extend 改写历史标签）。"""
    panel = _panel()
    port = get_port()
    for name in ("short_price", "short_vol", "long_price"):
        stored = store.load_group_labels("ashare", name)
        ext = port.extend("ashare", panel, stored)
        common = stored.index.intersection(ext.index)
        diff = int((stored.reindex(common) != ext.reindex(common)).sum())
        assert diff == 0, f"{name} 历史标签被 extend 改了 {diff} 天"
        assert ext.index.max() >= stored.index.max()
