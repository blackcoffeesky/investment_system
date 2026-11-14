"""卡片冒烟测试：P3 删旧 regime 代码前的安全网。

它覆盖的是"用真实数据生成一张卡"这条路径——之前的测试都没跑过 `pipeline.run`，
删代码时没有这层保护就只能靠手感。

一次出卡会写**四张**产物（日频卡 / 明细 / 月频卡 / 月频明细），这四张都要查
`nan` 字样：以前只盯日频卡，于是明细里的 `[+nan, +nan]`（CI 缺失没做 isfinite）
和 `+nan`（政策利率几个月没动 → 滚动标准差 0 → z 算不出）藏了很久。
"""

from __future__ import annotations

from pathlib import Path

# 每个资产只生成一次，两条测试共用（生成一次要十几秒）
_GENERATED: dict[str, tuple[Path, str, list[Path]]] = {}


def _generate(asset: str) -> tuple[Path, str, list[Path]]:
    """跑一遍出卡，返回 (日频卡路径, 该资产月频卡所属月, 本次写出的四张路径)。

    路径是照着 `pipeline.run()` 的落盘规则拼的：日频卡与明细在
    `cards/YYYY/MM/`，月频两张在**月频目标月**的 `cards/YYYY/MM/` 下——
    顺手验证了 `macro_month(asset=...)` 算出来的月份就是实际落盘的位置
    （这正是黄金被 A 股的表拖到 10 月那个 bug 的检查点）。
    """
    from causual.causual_core import pipeline
    from causual.causual_core.cli import _load_series
    from causual.port import get_port as causal_port
    from state_layer.port import get_port as state_port

    sp = state_port()
    series, meta = _load_series(asset, state=sp)
    view = sp.causual_view(asset, series, meta)
    path = pipeline.run(asset, series, meta, state=view, quiet=True)

    d = path.stem.split("_", 1)[1]                       # 2026-11-10
    month = causal_port().macro_month(meta, asset=asset)
    cards_dir = path.parent                              # …/cards/2026/11
    macro_dir = path.parents[2] / month[:4] / month[5:7]  # …/cards/YYYY/MM
    produced = [path,
                cards_dir / f"detail_{d}.md",
                macro_dir / f"macro_{month}.md",
                macro_dir / f"macro_detail_{month}.md"]
    return path, month, produced


def _generated(asset: str) -> tuple[Path, str, list[Path]]:
    if asset not in _GENERATED:
        _GENERATED[asset] = _generate(asset)
    return _GENERATED[asset]


def test_card_generation_smoke():
    path, month, produced = _generated("ashare")
    text = path.read_text(encoding="utf-8")

    assert "α(当期归因)" in text
    assert "净指向分解" in text
    assert "当前状态" in text, "缺状态提示行"

    missing = [p.name for p in produced if not p.exists()]
    assert not missing, f"run() 应该写出这四张，缺 {missing}"
    assert month == "2026-10", f"ashare 月频卡归档月应为 2026-10，实际 {month}"

    import markdown

    html = markdown.markdown(text, extensions=["tables", "fenced_code", "toc",
                                               "sane_lists"])
    assert "<table>" in html, "表格没渲染出来"
    assert "<p>|" not in html, "有表格没被解析"


def test_all_cards_have_no_nan():
    """四张产物 × 两个资产，都不许出现 `nan` 字样。

    A 股明细里曾经是 `[+nan, +nan]`（CI 缺失），黄金明细里是
    `| FED_RATE | … | +nan | … | +nan |`（z 算不出来）。都改成了「—」。
    """
    for asset in ("ashare", "gold"):
        _, month, produced = _generated(asset)
        assert month in ("2026-09", "2026-10"), f"{asset} 归档月异常：{month}"
        missing = [p.name for p in produced if not p.exists()]
        assert not missing, f"{asset} 缺产物 {missing}"
        for p in produced:
            body = p.read_text(encoding="utf-8")
            assert "nan" not in body.lower(), f"{asset}/{p.name} 里出现 nan"


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
