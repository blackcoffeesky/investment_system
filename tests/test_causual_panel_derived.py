"""causual A 股面板：表到位 + 派生列没整体消失。

背景（2026-11-14 踩过，比 state_layer 那次更隐蔽）
--------------------------------------------------
数据层把 ETF 代码从裸码改成带市场前缀（`etf_shares:sh510300`），
`causual_assets/ashare/panel.py::_derived` 还按 `c510300` 的老写法剥前缀、
再和裸码写的 `GROUPS` 比对 → 一个都匹配不上：

    group() 全返回 None → SIZE_FLOW / ETF_DISPERSION / NATIONAL_ETF /
    NATIONAL_ETF_EVENT **四列整体消失**；`sum([])` 抛的 AttributeError 又被
    `except: return out` 吞掉，**任何地方都不报错**

静默后果：`TREATMENT_COL["NATIONAL_ETF"]` 指向的 `national_etf_event` 不在面板里
→ `signal_layer.build_bundle` 里 `tcol not in df.columns` 直接 `continue`
→ 国家队这条边**整条不进卡片**（config 里正式声明的节点悄悄消失）。

2026-11-16 起这五列由**耦合层**算好（datahub 的 `DERIVED` 注册表 → adapter 落列），
causual 只做装配；本文件盯的东西没变：**列在不在、新不新鲜、边看不看得见**。

所以这条测试盯三件事：
1. `REQUIRED_TABLES` 里每张表在数据层都真的存在且有数据
2. 关键派生列**存在**、且最后有效日在最近交易日 5 个交易日内
3. 每条边的 treatment / source 列都在面板里（防"整条边被跳过"）

以及**反向验证**：把 `_derived` 打桩成返回空 dict，上面的判定必须变红。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# 允许落后最近交易日的交易日数。宏观/ETF 表本身有 lag_days，
# 但「整列消失」或「停在几个月前」不能容忍。
MAX_LAG_TRADING_DAYS = 5

# 由耦合层算好、落成面板列的关键派生列——少任何一列都算失效
DERIVED_COLS = ("etf_dispersion", "size_flow", "national_etf",
                "national_etf_event", "dividend_yield")

_CACHE: dict = {}


def _context() -> dict:
    """读一次数据 + 拼一次面板，多个断言共用（避免每个 test 重读库）。"""
    if _CACHE:
        return _CACHE
    from causual.causual_assets import load
    from causual.causual_core.cli import _load_panel
    from datahub.port import get_port

    pkg = load("ashare")
    raw_panel = _load_panel("ashare")
    panel = pkg.load_raw(raw_panel)
    # 交易日主轴 = 收盘价；整列缺了就用面板索引兜底（这种情况下别的断言会先红）
    if "close" in panel.columns and panel["close"].notna().any():
        close = panel["close"].dropna()
    else:
        close = pd.Series(1.0, index=panel.index)
    _CACHE.update(pkg=pkg, raw_panel=raw_panel, panel=panel, close=close,
                  meta=get_port().metadata())
    return _CACHE


def _bad_tables(meta: pd.DataFrame | None, tables) -> dict[str, str]:
    """`REQUIRED_TABLES` 里的表，在数据层是否真的存在且有数据。

    面板改成 DataFrame 之后不再有 `{表名: Series}` 可查——改成查 metadata：
    表登记在 `DataPort.tables()` 里、且 `end_date` 非空。
    """
    if meta is None or not len(meta):
        return {}
    md = meta.set_index("name")
    bad: dict[str, str] = {}
    for t in tables:
        if t not in md.index:
            bad[t] = "数据层没有登记这张表（删表 / 改名了？）"
            continue
        end = md.loc[t, "end_date"]
        if pd.isna(end) or str(end) in ("", "NaT"):
            bad[t] = "表在，但没有 end_date（空表？）"
    return bad


def _bad_derived(panel: pd.DataFrame, close: pd.Series) -> dict[str, str]:
    """派生列体检：返回 {列: 问题}；空 dict = 通过。

    这是本文件的核心判定——**打桩让派生列消失时，它必须返回非空**。

    也盯 `±inf`：ETF 上市前份额是 0，`pct_change` 会给出 inf（曾经有 42 个）。
    inf 不报错，但进 z-score / 分位就污染整列，和 NaN 一样隐蔽。
    """
    bad: dict[str, str] = {}
    if close.empty:
        return bad
    for col in DERIVED_COLS:
        if col not in panel.columns:
            bad[col] = "派生列不存在（_derived 返回空 dict？）"
            continue
        n_inf = int(np.isinf(panel[col]).sum())
        if n_inf:
            bad[col] = f"有 {n_inf} 个 ±inf（除零？进 z-score 会污染整列）"
            continue
        valid = panel[col].dropna()
        if valid.empty:
            bad[col] = "整列 NaN（耦合层没给这一列？）"
            continue
        lag = int((close.index > valid.index[-1]).sum())
        if lag > MAX_LAG_TRADING_DAYS:
            bad[col] = f"最后有效日 {valid.index[-1].date()}，落后 {lag} 个交易日"
    return bad


def _bad_edge_cols(pkg, panel: pd.DataFrame) -> dict[str, str]:
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
    """`REQUIRED_TABLES` 里每张表都要真的在数据层存在且有数据。

    这条是**故意 fail-closed** 的：数据层删表 / 改表名时立刻红。
    `REQUIRED_TABLES` 来自 `tables_of(PANEL_KIND)`（耦合层的声明，不抄一份）。
    """
    ctx = _context()
    if ctx["close"].empty:
        return                      # 空库（新环境）不判，与其它测试一致

    bad = _bad_tables(ctx["meta"], ctx["pkg"].REQUIRED_TABLES)
    assert not bad, f"ashare 有表没取到数据：{bad}"


def test_dividend_annual_no_longer_required():
    """迁移守卫：causual **已彻底不依赖 `dividend_annual`**。

    v5.42 把「分红/净利润」换成股息率后，这张表就不该再出现在依赖清单里
    ——否则数据层一删表，装配点直接 `DatabaseError`。
    """
    ctx = _context()
    tables = tuple(ctx["pkg"].REQUIRED_TABLES)
    assert "dividend_annual" not in tables, (
        "REQUIRED_TABLES 里还有 dividend_annual——数据层要删这张表了")
    assert "dividend_annual" not in ctx["raw_panel"].columns


def test_dividend_yield_reaches_panel():
    """股息率必须是面板的**真列**（否则供需层那一行会静默显示"无数据"）。

    2026-11-16 起它由耦合层算好（`DERIVED["dividend_yield"]`，`period="Y"`
    对齐到次年 1/1 再 ffill）——causual 既不算也不算对齐，只读列。
    缺列必须 `KeyError`（`PANEL_COLS` 一致），**不允许**静默整列 NaN。
    """
    ctx = _context()
    if ctx["close"].empty:
        return

    panel = ctx["panel"]
    assert "dividend_yield" in panel.columns, "面板里没有 dividend_yield"
    v = panel["dividend_yield"].dropna()
    assert not v.empty, "dividend_yield 整列 NaN（耦合层没给这一列？）"

    last = float(v.iloc[-1])
    assert 0.0 < last < 15.0, f"股息率 {last} 不像百分数（应为 0~15）"

    lag = int((ctx["close"].index > v.index[-1]).sum())
    assert lag <= MAX_LAG_TRADING_DAYS, (
        f"dividend_yield 最后有效日 {v.index[-1].date()}，落后 {lag} 个交易日")


def test_missing_panel_column_raises():
    """缺列必须 `KeyError`，不能静默整列 NaN。

    `load_raw` 用 `panel[list(PANEL_COLS)]` 取列——少一列当场炸。
    这条把"静默降级"这条路钉死（`_dividend_yield` 那套"告警 + 整列 NaN"已删）。
    """
    from causual.causual_assets.ashare import pipeline as P

    ctx = _context()
    if not len(ctx["raw_panel"]):
        return

    broken = ctx["raw_panel"].drop(columns=["dividend_yield"])
    try:
        P.load_raw(broken)
    except KeyError as e:
        assert "dividend_yield" in str(e), e
    else:
        raise AssertionError("少了 dividend_yield 却没人报错——又变成静默 NaN 了")


def test_ashare_derived_columns_present_and_fresh():
    """关键派生列存在，且最后有效日落在最近交易日 5 个交易日内。"""
    ctx = _context()
    if ctx["close"].empty:
        return

    anchor = ctx["close"].index[-1]
    bad = _bad_derived(ctx["panel"], ctx["close"])
    assert not bad, f"ashare 派生列失效（最近交易日 {anchor.date()}）：{bad}"


def test_bare_code_normalizes_etf_codes():
    """数据层的代码前缀怎么变，ETF 分组都要能对上。

    归一逻辑**只有一份**，在 `datahub.fin.derive.bare_code`（2026-11-16 随
    `etf_group_shares` 一起沉进数据层；分组知识归数据层，消费方不再各存一份）。
    """
    from datahub.fin.derive import bare_code

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

    打桩的是 `pipeline._derived`：它是**装配派生列的唯一接缝**（现在只是从耦合层
    的 `base` 里取列，不再计算）。这条测试是那个函数必须留着的理由。
    """
    from causual.causual_assets.ashare import pipeline as pipeline_mod

    ctx = _context()
    if ctx["close"].empty:
        return

    # 打桩前后各体检一次：先确认现在是干净的，否则反向验证没有意义
    assert not _bad_derived(ctx["panel"], ctx["close"]), "打桩前就已经是脏的"

    original = pipeline_mod._derived
    try:
        pipeline_mod._derived = lambda df, base: {}       # type: ignore[assignment]
        broken = pipeline_mod.load_raw(ctx["raw_panel"])
    finally:
        pipeline_mod._derived = original                  # type: ignore[assignment]

    # 派生列确实整体没了（= 当年那个 bug 的现象），而且**不抛异常**
    assert not [c for c in DERIVED_COLS if c in broken.columns], \
        "打桩没生效：派生列还在"

    bad = _bad_derived(broken, ctx["close"])
    assert set(bad) == set(DERIVED_COLS), (
        f"派生列整体消失时没被全部抓住：只报了 {sorted(bad)}")
    assert all("不存在" in v for v in bad.values()), bad
