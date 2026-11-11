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


def test_state_features_match_prepare():
    """状态层的特征在"原始面板"和"因果层 prepare 后的面板"上必须一致。

    状态层自带最小派生（VOLUME_Z / MARGIN_CHG / SHIBOR_CHG_BP），
    和因果层 prepare() 里的公式是两份实现——这条测试防止漂移。
    """
    from causual.causual_assets.ashare import pipeline as P
    from causual.causual_core.cli import _load_series
    from state_layer import state_assets

    series, meta = _load_series("ashare")
    raw = P.load_raw(series, meta)
    prep = P.prepare(raw)

    pkg = state_assets.load("ashare")
    a = pkg.features.build(raw)
    b = pkg.features.build(prep)
    assert a.index.equals(b.index)
    assert float((a - b).abs().max().max()) < 1e-9
