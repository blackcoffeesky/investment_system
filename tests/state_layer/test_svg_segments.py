"""状态图几何断言：**线段必须首尾相接、段数 = 点数-1**（不允许断缝）。

对 `routes._svg()` 真实产出的 SVG 做正则解析——不是对着实现抄一遍。
"""

from __future__ import annotations

import re

import pandas as pd

from state_layer.web.routes import _svg


def _rows(n: int = 60) -> list[dict]:
    idx = pd.bdate_range("2026-01-01", periods=n)
    labels = ["trend_up"] * 20 + ["range"] * 20 + ["trend_down"] * 20
    return [{"date": str(d)[:10], "close": 100 + i * 0.5 + (i % 7),
             "amount": 1e9 + i, "label": labels[i]} for i, d in enumerate(idx)]


def _segs(svg: str) -> list[tuple[float, float, float, float]]:
    pat = re.compile(r'<line class="seg" '
                     r'x1="([-\d.]+)" y1="([-\d.]+)" '
                     r'x2="([-\d.]+)" y2="([-\d.]+)"')
    return [(float(a), float(b), float(c), float(d))
            for a, b, c, d in pat.findall(svg)]


def test_segments_are_continuous():
    rows = _rows(60)
    segs = _segs(_svg(rows))
    assert len(segs) == len(rows) - 1, f"段数应为点数-1：{len(segs)} vs {len(rows) - 1}"
    for i in range(len(segs) - 1):
        x1, y1, x2, y2 = segs[i]
        nx1, ny1, _, _ = segs[i + 1]
        assert abs(x2 - nx1) < 0.01, f"第 {i} 段与第 {i+1} 段 x 断开：{x2} vs {nx1}"
        assert abs(y2 - ny1) < 0.01, f"第 {i} 段与第 {i+1} 段 y 断开：{y2} vs {ny1}"


def test_colour_changes_at_switch_day():
    """切换日：第 19 段（trend_up→range）与第 20 段颜色不同，但端点重合。"""
    rows = _rows(60)
    svg = _svg(rows)
    cols = re.findall(r'class="seg"[^>]*stroke="(#[0-9a-f]{6})"', svg)
    assert len(cols) == len(rows) - 1
    assert cols[19] != cols[20], f"切换日颜色应不同：{cols[19]} / {cols[20]}"
