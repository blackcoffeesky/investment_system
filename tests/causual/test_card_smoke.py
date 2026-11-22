"""卡片冒烟测试：P3 删旧 regime 代码前的安全网。

它覆盖的是"用真实数据生成一张卡"这条路径——之前的测试都没跑过 `pipeline.run`，
删代码时没有这层保护就只能靠手感。

一次出卡会写**四张**产物（日频卡 / 明细 / 月频卡 / 月频明细），这四张都要查
`nan` 字样：以前只盯日频卡，于是明细里的 `[+nan, +nan]`（CI 缺失没做 isfinite）
和 `+nan`（政策利率几个月没动 → 滚动标准差 0 → z 算不出）藏了很久。

⚠️ **产物写到临时目录，不碰 `causual/data/output`**（2026-11-22 踩过）：
这个测试以前用默认 `out_dir`，于是**跑一次测试就把正式卡重写了**——正好撞上
"重估 edges 后要备份旧卡再重出"的那次，备份里拿到的已经是被测试覆盖过的新卡，
对照直接失效。测试只该证明"能出卡、没 nan"，不该改用户的产物。
"""


from __future__ import annotations

import tempfile
from pathlib import Path

#: 运行超过 10 秒（`run_tests.py --fast` 会跳过）
SLOW = True

# 每个资产只生成一次，两条测试共用（生成一次要十几秒）
_GENERATED: dict[str, tuple[Path, str, list[Path]]] = {}

#: 本次测试专用的产物根目录（**不是** `causual/data/output`）
_OUT_DIR = Path(tempfile.mkdtemp(prefix="causual_cards_"))


def _generate(asset: str) -> tuple[Path, str, list[Path]]:
    """跑一遍出卡，返回 (日频卡路径, 该资产月频卡所属月, 本次写出的四张路径)。

    路径是照着 `pipeline.run()` 的落盘规则拼的：日频卡与明细在
    `cards/YYYY/MM/`，月频两张在**月频目标月**的 `cards/YYYY/MM/` 下——
    顺手验证了 `macro_month(asset=...)` 算出来的月份就是实际落盘的位置
    （这正是黄金被 A 股的表拖到 10 月那个 bug 的检查点）。
    """

    from causual.causual_core import pipeline
    from causual.causual_core.cli import _freshness, _load_panel, state_view
    from causual.port import get_port as causal_port

    # 装配点取一次面板，喂给状态层和因果层（2026-11-16 起两边都只收 DataFrame）；
    # `state_view()` 会把 v2 的画像表一起补进来（spec 里没有 profile）
    panel = _load_panel(asset)
    view = state_view(asset, panel)
    rows = _freshness(asset, panel)
    # `out_dir` 显式指到测试自己的临时目录：**不许**让测试重写 `causual/data/output`
    path = pipeline.run(asset, panel, freshness=rows, state=view,
                        out_dir=_OUT_DIR, quiet=True)

    d = path.stem.split("_", 1)[1]                       # 2026-11-10
    month = causal_port().macro_month(rows)
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
    """状态层特征在「原始面板」与「因果层 prepare 后面板」上必须逐值相同。

    为什么还要守这一条：状态层**只读面板列**（`short_price(panel)`），而 causual 的
    `prepare()` 会往同一张表上加几十列。只要 prepare 不去改写状态层吃的列
    （`close` / `high` / `low` / `open`），两边算出来的特征就该一模一样——
    一旦有人让 `prepare()` 覆盖这些列，这条当场红。

    顺便对账一处**更容易静默失效**的东西：`spec` 里声明的特征名
    （`groups[primary]["features"]`）必须等于构造函数产出的列名。列名对不上时
    状态层不会报错，只是特征矩阵变窄、模型照跑——那是查不出来的漂移。

    （2026-11-22：状态层把 9 个旧特征换成 `short_price` / `short_vol` / `long_price`
    三个构造函数，`features.build()` 已退役；`etf_dispersion` 不再由状态层计算。）
    """
    from causual.causual_assets.ashare import pipeline as P
    from causual.causual_core.cli import _load_panel
    from causual.causual_core.spec import is_v2, primary_group
    from state_layer import state_assets
    from state_layer.port import get_port as state_port

    raw = P.load_raw(_load_panel("ashare"))
    prep = P.prepare(raw)

    pkg = state_assets.load("ashare")
    a = pkg.features.build_groups(raw)
    b = pkg.features.build_groups(prep)

    # 组集合 = 配置里启用的那几个（多出来的组会白算，少一个会让模型静默少特征）
    assert set(a) == set(pkg.config.GROUPS_ENABLED), (set(a), pkg.config.GROUPS_ENABLED)

    for grp in a:
        assert a[grp].index.equals(b[grp].index), grp
        assert float((a[grp] - b[grp]).abs().max().max()) < 1e-9, grp

    # spec 声明的特征名 ↔ 构造函数产出的列名
    spec = state_port().spec("ashare")
    if is_v2(spec):
        prim = primary_group(spec)
        assert prim in a, f"spec 的 primary={prim!r} 不在 build_groups 的产出里：{sorted(a)}"
        declared = set(spec["groups"][prim]["features"])
        produced = set(a[prim].columns)
    else:
        declared = set((spec.get("data") or {}).get("features") or [])
        produced = {c for df in a.values() for c in df.columns}
    assert declared == produced, (
        f"spec 里声明的特征与实现产出的列不一致——多出 {sorted(produced - declared)}、"
        f"缺少 {sorted(declared - produced)}")
