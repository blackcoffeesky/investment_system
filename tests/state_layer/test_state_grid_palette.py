"""九宫格必须用自己的 9 色调色板 + 中文 hover（2026-11-24 坏过一次）。

坏法：`_charts` 的 grid 分支漏了 `labels`/`colors` 赋值 → 九宫格继承了上一轮
`short_vol` 的标签，颜色回退到单组调色板（hover 只剩 `low`）。
这里用**渲染出来的 svg 文本**断言，而不是靠"看着有颜色"。
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from service.app import app

GRID_COLORS = {"#a8c4e0", "#e8b98a", "#e89a9a", "#5d8ec4", "#d98b3f",
               "#cf5b5b", "#2f5f96", "#a85c12", "#8f1f1f"}


def _grid_svg(html: str) -> str:
    """取九宫格那张 svg（标题是「短期九宫格」后面第一个 <svg …>）。"""
    i = html.find("短期九宫格")
    assert i != -1
    j = html.find("<svg", i)
    return html[j:html.find("</svg>", j)]


def test_grid_uses_own_palette():
    page = TestClient(app).get("/state?asset=ashare").text
    svg = _grid_svg(page)
    used = set(re.findall(r'stroke="(#[0-9a-f]{6})"', svg))
    hit = used & GRID_COLORS
    assert len(hit) >= 6, f"九宫格配色没生效：用到 {sorted(used)}"
    assert "#6b7280" not in hit


def test_grid_hover_is_chinese_combo():
    page = TestClient(app).get("/state?asset=ashare").text
    svg = _grid_svg(page)
    titles = re.findall(r"<title>([^<]+)</title>", svg)
    assert titles, "九宫格没有 hover 文案"
    combo = [t for t in titles if " - " in t]
    assert len(combo) >= 1, f"hover 不是中文组合：{titles[:3]}"
    assert re.search(r"(下跌|震荡|上涨) - (低波|中波|高波)", combo[0]), combo[0]
    # 不许出现裸的单组标签
    for bare in ("· low</title>", "· normal</title>", "· high</title>"):
        assert bare not in svg, f"九宫格 hover 出现了裸单组标签：{bare}"
