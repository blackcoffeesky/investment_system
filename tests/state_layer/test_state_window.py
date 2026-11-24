"""服务端窗口渲染的断言（不依赖任何浏览器行为）。

2026-11-24 起 /state 用「表单 + 服务端按 lo/hi 只渲染窗口内的段」，前端脚本已全部删除。
这里钉住：同一资产、不同窗口 → 段数不同、svg 数不变、页面里没有 chart.js。
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from service.app import app

C = TestClient(app)


def _segs(html: str) -> int:
    return len(re.findall(r'<line class="seg"', html))


def test_window_changes_segment_count():
    p1 = C.get("/state?asset=ashare").text                 # 默认最近一年
    p2 = C.get("/state?asset=ashare&lo=0&hi=2499").text     # 全段
    assert _segs(p1) < 1600, f"默认窗口段太多（{_segs(p1)}），窗口没生效？"
    assert _segs(p2) > 2000, f"全段段太少（{_segs(p2)}）"
    assert p1.count("<svg") == p2.count("<svg") == 4


def test_default_window_is_last_year():
    """默认窗口的首末日期应落在约 251 个交易日之内。"""
    page = C.get("/state?asset=ashare").text
    spans = re.findall(r"当前窗口 (\d{4}-\d{2}-\d{2}) ~ (\d{4}-\d{2}-\d{2})（(\d+) 天）", page)
    assert spans, "页面里没有窗口区间标注"
    for _, _, days in spans:
        assert int(days) <= 251, f"默认窗口 {days} 天，超过一年"


def test_no_frontend_script():
    page = C.get("/state?asset=ashare").text
    assert "/state/chart.js" not in page, "前端脚本应该已经删掉"
    assert not re.search(r'<script[^>]*src=', page), "页面不该再引用外部脚本"
    assert C.get("/state/chart.js").status_code == 404


def test_backend_clamps_window():
    """lo/hi 反了或越界：服务端夹紧，页面仍 200 且有窗口。"""
    for q in ("lo=900&hi=100", "lo=-5&hi=99999", "lo=abc&hi=xyz"):
        r = C.get(f"/state?asset=ashare&{q}")
        assert r.status_code == 200, q


def test_gold_page_window():
    page = C.get("/state?asset=gold").text
    assert page.count("<svg") == 1
    assert _segs(page) > 0
