"""/state 页面的结构 guard（2026-11-24 改为服务端渲染后的版本）。

历史：前端 JS 版本三次"测试通过、浏览器不生效"，最终整体删除，改成
「表单 + 服务端按 lo/hi 只渲染窗口内的段」。现在这里钉住的是**服务端可断言**的东西：
不依赖浏览器、不依赖脚本是否执行。
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from service.app import app


def test_page_has_forms_not_scripts():
    page = TestClient(app).get("/state?asset=ashare").text

    # 不再有外部脚本（缓存/执行顺序/浏览器差异这些坑一次性消失）
    assert "/state/chart.js" not in page
    assert not re.search(r'<script[^>]*src=', page)

    # 单轨双手柄：一个 .track 里两个 input，容器数 == track 数
    n_bar = page.count('class="rngbar"')
    assert n_bar >= 1, "一张图都没有"
    assert page.count('class="track"') == n_bar, "不是单轨：track 数与容器数不一致"
    assert page.count('name="lo"') == n_bar
    assert page.count('name="hi"') == n_bar

    # 每张图一个 GET 表单，提交到 /state，且带上 asset
    assert page.count('method="get"') == n_bar, "表单与图数量对不上"
    assert page.count('name="asset"') == n_bar, "表单没带 asset"


def test_gold_page_structure():
    page = TestClient(app).get("/state?asset=gold").text
    assert page.count('class="rngbar"') >= 1
    assert "/state/chart.js" not in page
