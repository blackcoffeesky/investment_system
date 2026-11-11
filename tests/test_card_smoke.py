"""卡片冒烟测试：P3 删旧 regime 代码前的安全网。

它覆盖的是"用真实数据生成一张卡"这条路径——之前的测试都没跑过 `pipeline.run`，
删代码时没有这层保护就只能靠手感。
"""

from pathlib import Path


def test_card_generation_smoke():
    from causual.causual_core import pipeline
    from causual.causual_core.cli import _load_series

    series, meta = _load_series("ashare")
    path = pipeline.run("ashare", series, meta, quiet=True)
    text = Path(path).read_text(encoding="utf-8")

    assert "α(当期归因)" in text
    assert "净指向分解" in text
    assert "nan" not in text, "卡片里出现 nan"
    assert "当前状态" in text, "缺状态提示行"

    import markdown

    html = markdown.markdown(text, extensions=["tables", "fenced_code", "toc",
                                               "sane_lists"])
    assert "<table>" in html, "表格没渲染出来"
    assert "<p>|" not in html, "有表格没被解析"
