"""边用到的列：要么有数据，要么**显式声明没接**——不许"看起来有、其实是空"。

背景（2026-11-15）
------------------
`SOCIAL_FINANCING` 是恒 NaN 的桩列，却被当成 `EQUITY_SUPPLY` 边的 confounder。
两个估计器都会**静默跳过**全 NaN / 零方差的控制变量
（`estimators.fit_dml_edge` 的 `notna().any()`、`fit_distributed_lag` 的 `sd 有限`），
于是配置上写着"控制了社融"、实际根本没控——卡片、快照、日志里都看不出来。

这类东西的判据不是"会不会抛异常"（不会），而是**"这一列有没有值、有没有人说过它没值"**。
所以检查两条：

1. 每条边的 `treatment` / `source` / `confounder` 列，必须在面板里且非空行数 > 0
2. 例外只能来自 `config.NOT_CONNECTED`（显式声明"未接入 + 原因"）
3. **反向**：声明了却已经有数据 → 红。否则那张表会烂成"永久豁免清单"

第 3 条是这套机制能活下来的关键：数据接上时不删声明，测试就红。
"""

from __future__ import annotations

# 每个资产只拼一次面板（panel 要走耦合层，比较慢）
_PANELS: dict[str, tuple] = {}


def _panel(asset: str):
    """(pkg, 面板) —— 面板已经过 prepare()，就是信号层实际吃到的那张。"""
    if asset not in _PANELS:
        from causual.causual_assets import load
        from causual.causual_core.cli import _load_series

        pkg = load(asset)
        series, meta = _load_series(asset)
        _PANELS[asset] = (pkg, pkg.prepare(pkg.load_raw(series, meta)))
    return _PANELS[asset]


def _refs(pkg) -> dict[str, str]:
    """{列名: 谁在用它} —— treatment / source / confounder 全算。"""
    out: dict[str, str] = {}
    for e in pkg.EDGES:
        tcol = pkg.TREATMENT_COL.get(e.source)
        if tcol:
            out.setdefault(tcol, f"{e.source} 边的 treatment")
        scol = pkg.SOURCE_COL.get(e.source)
        if scol:
            out.setdefault(scol, f"{e.source} 边的 source")
        for c in e.confounders:
            out.setdefault(c, f"{e.source} 边的 confounder")
    return out


ASSETS = ("ashare", "gold")


def test_edge_columns_have_data():
    """边的每一列都得有值，除非在 `NOT_CONNECTED` 里声明过。"""
    problems: dict[str, dict[str, str]] = {}

    for asset in ASSETS:
        pkg, panel = _panel(asset)
        declared = dict(getattr(pkg, "NOT_CONNECTED", {}))
        bad: dict[str, str] = {}
        for col, who in sorted(_refs(pkg).items()):
            if col in declared:
                continue                        # 已声明，由下一条测试盯"是否该撤销"
            if col not in panel.columns:
                bad[f"{col}"] = f"{who}：面板里根本没有这一列"
            elif int(panel[col].notna().sum()) == 0:
                bad[f"{col}"] = f"{who}：整列 NaN（没接数据就别说它控制住了）"
        if bad:
            problems[asset] = bad

    assert not problems, (
        f"有边在读空列。要么接数据，要么在 config.NOT_CONNECTED 里写明原因：{problems}")


def test_not_connected_registry_is_honest():
    """`NOT_CONNECTED` 不能变成永久豁免：

    - 声明了却已经有数据 → 红（该删声明了）
    - 声明了却没有任何边在用 → 红（无意义的条目）
    - 声明没写原因 → 红
    """
    problems: dict[str, dict[str, str]] = {}

    for asset in ASSETS:
        pkg, panel = _panel(asset)
        declared = dict(getattr(pkg, "NOT_CONNECTED", {}))
        used = _refs(pkg)
        bad: dict[str, str] = {}
        for col, why in declared.items():
            if not str(why).strip():
                bad[col] = "没写原因"
                continue
            if col not in used:
                bad[col] = "声明了豁免，但没有任何边在读它——删掉这条"
                continue
            if col in panel.columns and int(panel[col].notna().sum()) > 0:
                bad[col] = ("已经有数据了，把它从 NOT_CONNECTED 删掉"
                            "（否则这张表会变成永久豁免）")
        if bad:
            problems[asset] = bad

    assert not problems, f"NOT_CONNECTED 里有过期/无意义的条目：{problems}"


def test_not_connected_is_scoped_per_asset():
    """`NOT_CONNECTED` 是**资产自己的**声明，不能在 config 里漏定义。

    漏了不会报错（`getattr` 拿到空 dict），但等于这个资产的空列没人管——
    所以这里要求两个资产的 config 都显式声明（可以是空 dict）。
    """
    missing = []
    for asset in ASSETS:
        from causual.causual_assets import load

        if not hasattr(load(asset), "NOT_CONNECTED"):
            missing.append(asset)
    assert not missing, (
        f"这些资产的 config 没声明 NOT_CONNECTED（空也要写成空 dict）：{missing}")
