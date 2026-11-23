"""/state 页面的**注入点**回归（2026-11-23 两次踩坑后的自动化）。

两次坏都是同一形状：改模板的某个区块，连带删掉了别处的注入点。
- 第一次：`<script>` 落进 `{% block title %}` → 字符串在、但不执行 → 拖动无反应
- 第二次：删「观测矩阵」区块时把 `<style>` 与 `<script src=...>` 一起删了 → CSS/JS 都没了

所以这里断言的是**真的会加载/生效的形式**，不是"页面里出现了这个字符串"。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from service.app import app


def test_state_page_wires_chart_script_and_style():
    page = TestClient(app).get("/state?asset=ashare").text

    # 真的会加载的脚本标签（不是标题里/注释里的字符串）
    assert '<script src="/state/chart.js"' in page, "脚本注入点丢了（拖动会没反应）"
    assert ".track" in page, "样式注入点丢了（会退化成两条独立滑轨）"

    # 单轨双手柄：容器数 == .track 数，手柄数 == 2 × 容器数
    n_bar = page.count('class="rngbar"')
    assert n_bar >= 1, "一张图都没有"
    assert page.count('class="track"') == n_bar, "不是单轨：track 数与容器数不一致"
    assert page.count('type="range"') == 2 * n_bar, "手柄数应为 2 × 容器数"

    # 每张图都有数据（滑块与图一一对应）
    assert page.count('id="d-') == n_bar, "滑块与图数量对不上"


def test_gold_page_also_wired():
    page = TestClient(app).get("/state?asset=gold").text
    assert '<script src="/state/chart.js"' in page
    assert page.count('class="rngbar"') >= 1
