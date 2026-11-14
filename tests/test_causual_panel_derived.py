"""causual A 股面板：表到位 + 派生列没整体消失。

背景（2026-11-14 踩过，比 state_layer 那次更隐蔽）
--------------------------------------------------
数据层把 ETF 代码从裸码改成带市场前缀（`etf_shares:sh510300`），
`causual_assets/ashare/panel.py::_derived` 还按 `c510300` 的老写法剥前缀、
再和裸码写的 `GROUPS` 比对 → 一个都匹配不上：

    group() 全返回 None → SIZE_FLOW / ETF_DISPERSION / NATIONAL_ETF /
    NATIONAL_ETF_EVENT **四列整体消失**；`sum([])` 抛的 AttributeError 又被
    `except: return out` 吞掉，**任何地方都不报错**

静默后果：`TREATMENT_COL["NATIONAL_ETF"]` 指向的 `NATIONAL_ETF_EVENT` 不在面板里
→ `signal_layer.build_bundle` 里 `tcol not in df.columns` 直接 `continue`
→ 国家队这条边**整条不进卡片**（config 里正式声明的节点悄悄消失）。

所以这条测试盯两件事：
1. `REQUIRED_TABLES` 里每张表都真的取到了数据（表被删 / 改名要当场红）
2. 关键派生列**存在**、且最后有效日在最近交易日 5 个交易日内
3. 每条边的 treatment / source 列都在面板里（防"整条边被跳过"）

以及**反向验证**：把 `_derived` 打桩成返回空 dict，上面的判定必须变红。
"""

from __future__ import annotations

import pandas as pd

# 允许落后最近交易日的交易日数。etf_shares / 宏观表本身有 lag_days，
# 但「整列消失」或「停在几个月前」不能容忍。
MAX_LAG_TRADING_DAYS = 5

# 由 etf_shares 现算的关键派生列——少任何一列都算失效
DERIVED_COLS = ("SIZE_FLOW", "ETF_DISPERSION", "NATIONAL_ETF", "NATIONAL_ETF_EVENT")

_CACHE: dict = {}


def _context() -> dict:
    """读一次数据 + 拼一次面板，多个断言共用（避免每个 test 重读库）。"""
    if _CACHE:
        return _CACHE
    from causual.causual_assets import load
    from causual.causual_core.cli import _load_series

    pkg = load("ashare")
    series, meta = _load_series("ashare")
    panel = pkg.load_raw(series, meta)
    # 交易日主轴 = csi_all；CLOSE 整个缺了就用面板索引兜底（这种情况下别的断言会先红）
    if "CLOSE" in panel.columns and panel["CLOSE"].notna().any():
        close = panel["CLOSE"].dropna()
    else:
        close = pd.Series(1.0, index=panel.index)
    _CACHE.update(pkg=pkg, series=series, meta=meta, panel=panel, close=close)
    return _CACHE


def _missing_tables(series: dict, tables) -> dict[str, str]:
    """哪些表在数据包里一列都没有 / 有列但整列 NaN。"""
    bad: dict[str, str] = {}
    for t in tables:
        keys = [k for k in series if k == t or k.startswith(f"{t}:")]
        if not keys:
            bad[t] = "数据包里没有这张表（删表 / 改名了？）"
            continue
        if all(series[k].dropna().empty for k in keys):
            bad[t] = f"{len(keys)} 列全为 NaN"
    return bad


def _bad_derived(panel, close) -> dict[str, str]:
    """派生列体检：返回 {列: 问题}；空 dict = 通过。

    这是本文件的核心判定——**打桩让派生列消失时，它必须返回非空**。
    """
    bad: dict[str, str] = {}
    if close.empty:
        return bad
    for col in DERIVED_COLS:
        if col not in panel.columns:
            bad[col] = "派生列不存在（_derived 返回空 dict？）"
            continue
        valid = panel[col].dropna()
        if valid.empty:
            bad[col] = "整列 NaN（ETF 代码归一后没匹配上 GROUPS？）"
            continue
        lag = int((close.index > valid.index[-1]).sum())
        if lag > MAX_LAG_TRADING_DAYS:
            bad[col] = f"最后有效日 {valid.index[-1].date()}，落后 {lag} 个交易日"
    return bad


def _bad_edge_cols(pkg, panel) -> dict[str, str]:
    """每条边的 treatment / source 列都必须落在面板里。

    缺列不会报错，只会让这条边在 `build_bundle` 里被 `continue` 掉，
    卡片上自然也没有它——所以在这里拦住。
    """
    bad: dict[str, str] = {}
    for e in pkg.EDGES:
        tcol = pkg.TREATMENT_COL.get(e.source)
        if tcol is None:
            bad[f"{e.source}:treatment"] = "TREATMENT_COL 没登记"
        elif tcol not in panel.columns:
            bad[f"{e.source}:treatment"] = f"面板里没有 {tcol}"
        scol = pkg.SOURCE_COL.get(e.source)
        if scol is not None and scol not in panel.columns:
            bad[f"{e.source}:source"] = f"面板里没有 {scol}"
    return bad


def test_ashare_required_tables_have_data():
    """`REQUIRED_TABLES` 里每张表都要真的取到数据。

    ⚠️ 这条是**故意 fail-closed** 的：数据层删表（例如计划中的
    `dividend_annual` → 派生股息率）时，这里会立刻变红，逼着 causual
    的依赖同步改掉——因为表没了不会安静降级，`series_map` 会直接抛
    `DatabaseError: no such table`，整张面板连带卡片一起挂。
    """
    ctx = _context()
    if ctx["close"].empty:
        return                      # 空库（新环境）不判，与其它测试一致

    bad = _missing_tables(ctx["series"], ctx["pkg"].REQUIRED_TABLES)
    assert not bad, f"ashare 有表没取到数据：{bad}"


def test_ashare_derived_columns_present_and_fresh():
    """关键派生列存在，且最后有效日落在最近交易日 5 个交易日内。"""
    ctx = _context()
    if ctx["close"].empty:
        return

    anchor = ctx["close"].index[-1]
    bad = _bad_derived(ctx["panel"], ctx["close"])
    assert not bad, f"ashare 派生列失效（csi_all 最近交易日 {anchor.date()}）：{bad}"


def test_bare_code_normalizes_etf_codes():
    """数据层的代码前缀怎么变，`GROUPS` 都要能对上。"""
    from causual.causual_assets.ashare.panel import bare_code

    for raw, want in (("sh510300", "510300"), ("sz159919", "159919"),
                      ("c510300", "510300"), ("510300", "510300"),
                      ("510300.SH", "510300")):
        assert bare_code(raw) == want, f"{raw} 归一成 {bare_code(raw)}，应为 {want}"


def test_edges_columns_reach_panel():
    """每条边要用的列都在面板里——否则那条边会被静默跳过。"""
    from causual.causual_assets.ashare import pipeline as P

    ctx = _context()
    if ctx["close"].empty:
        return

    prepared = P.prepare(ctx["panel"])
    bad = _bad_edge_cols(ctx["pkg"], prepared)
    assert not bad, f"有边因为列缺失会被静默跳过：{bad}"


def test_guard_turns_red_when_derived_vanishes():
    """反向验证：把 `_derived` 打桩成返回空 dict，判定必须变红。

    没有这一条，上面几条测试只证明「今天数据是好的」——万一判定逻辑本身
    写宽了（例如只查 `col in panel` 而面板里其实全是 NaN），它会永远绿。
    """
    from causual.causual_assets.ashare import panel as panel_mod

    ctx = _context()
    if ctx["close"].empty:
        return

    # 打桩前后各体检一次：先确认现在是干净的，否则反向验证没有意义
    assert not _bad_derived(ctx["panel"], ctx["close"]), "打桩前就已经是脏的"

    original = panel_mod._derived
    try:
        panel_mod._derived = lambda idx, series: {}       # type: ignore[assignment]
        broken = panel_mod.panel(ctx["series"], ctx["meta"])
    finally:
        panel_mod._derived = original                    # type: ignore[assignment]

    # 派生列确实整体没了（= 当年那个 bug 的现象），而且**不抛异常**
    assert not [c for c in DERIVED_COLS if c in broken.columns], \
        "打桩没生效：派生列还在"

    bad = _bad_derived(broken, ctx["close"])
    assert set(bad) == set(DERIVED_COLS), (
        f"派生列整体消失时没被全部抓住：只报了 {sorted(bad)}")
    assert all("不存在" in v for v in bad.values()), bad
