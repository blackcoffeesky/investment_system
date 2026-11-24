"""拟合窗口/预测 的验收（2026-11-24 改版后）。

- 阴影带 = 当前拟合训练窗口（class=trainband，opacity 0.12）
- 阴影右侧 = 预测天数（文字 "→ 预测 N 天" + 图注）
- 不再画每次重估的竖虚线；外推段也不再虚线（和正常段一样）
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from fastapi.testclient import TestClient

from service.app import app
from shared.adapter import get_data
from state_layer import state_assets, store
from state_layer.port import get_port


def _page(asset: str = "ashare") -> str:
    return TestClient(app).get(f"/state?asset={asset}").text


def test_chart_reaches_panel_last_day():
    panel = get_data(state_assets.panel_of("ashare"))
    page = _page()
    assert str(panel.index[-1].date()) in page
    assert "预测" in page and "当前拟合训练窗口" in page


def test_train_band_present_and_no_refit_lines():
    page = _page()
    assert page.count('class="trainband"') >= 1
    assert 'class="rf"' not in page, "竖虚线应已删除"
    assert "stroke-dasharray" not in page, "外推段不该再用虚线"


def test_band_text_mentions_outside_window():
    page = _page()
    # 默认窗口是最近一年，训练窗口起点（约 1000 天前）必然在窗口外
    assert "起点在窗口外" in page


def test_extend_does_not_change_history():
    panel = get_data(state_assets.panel_of("ashare"))
    port = get_port()
    for name in ("short_price", "short_vol", "long_price"):
        stored = store.load_group_labels("ashare", name)
        ext = port.extend("ashare", panel, stored)
        common = stored.index.intersection(ext.index)
        assert int((stored.reindex(common) != ext.reindex(common)).sum()) == 0, name
