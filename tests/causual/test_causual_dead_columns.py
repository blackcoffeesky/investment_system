"""边用到的列：要么有数据，要么**显式声明没接**——不许"看起来有、其实是空"。

背景（2026-11-15）
------------------
`SOCIAL_FINANCING` 是恒 NaN 的桩列，却被当成 `EQUITY_SUPPLY` 边的 confounder。
两个估计器都会**静默跳过**全 NaN / 零方差的控制变量
（`estimators.fit_dml_edge` 的 `notna().any()`、`fit_distributed_lag` 的 `sd 有限`），
于是配置上写着"控制了社融"、实际根本没控——卡片、快照、日志里都看不出来。

这类东西的判据不是"会不会抛异常"（不会），而是**"这一列有没有值、有没有人说过它没值"**。
所以检查三条：

1. 每条边的 `treatment` / `source` / `confounder` 列，必须在面板里且非空行数 > 0
2. 例外只能来自 `config.NOT_CONNECTED`（显式声明"未接入 + 原因"）
3. **反向**：声明了却已经有数据 → 红。否则那张表会烂成"永久豁免清单"

第 3 条是这套机制能活下来的关键：数据接上时不删声明，测试就红。
"""

from __future__ import annotations

# 每个资产只拼一次面板（走耦合层取数，比较慢）
_PANELS: dict[str, tuple] = {}


def _panel(asset: str):
    """(pkg, 面板) —— 面板已经过 prepare()，就是信号层实际吃到的那张。"""
    if asset not in _PANELS:
        from causual.causual_assets import load
        from causual.causual_core.cli import _load_panel

        pkg = load(asset)
        _PANELS[asset] = (pkg, pkg.prepare(pkg.load_raw(_load_panel(asset))))
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

# 已退役的大写面板列名。**只增不改**：
# 往这里加名字 = 宣告"这个名字永久退役"，以后谁再写它就红。
# 2026-11-16 追加：六个特征列沉进耦合层，名字改成语义小写。
LEGACY_COLS = frozenset({
    "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "CSI_ALL", "CSI300", "CSI1000",
    "SHIBOR3M", "MARGIN_BALANCE", "PMI", "INSIDER_NET", "RETAIL_SALES",
    "RETAIL_SALES_MOM", "CPI_YOY", "PPI_YOY", "INDUSTRIAL_YOY", "EXPORTS_YOY",
    "M1_YOY", "M2_YOY", "M1_LEVEL", "M2_LEVEL", "TOTAL_SHARES",
    "TOTAL_MKTCAP", "EQUITY_FIN_MONTHLY", "EQUITY_SUPPLY", "VIX", "USDCNY",
    "USD_INDEX", "FED_RATE", "REAL_YIELD_10Y", "GEOPOLITIC_RISK", "XAU_USD",
    "CB_HOLDINGS", "ETF_HOLDINGS", "OPEN_INTEREST", "COT_NET_LONG",
    "CPI_SURPRISE", "GOLD_CNY",
    # v5.48：六个特征列改成耦合层的语义名
    "SIZE_FLOW", "ETF_DISPERSION", "NATIONAL_ETF", "NATIONAL_ETF_EVENT",
    "CB_GOLD_BUY", "ETF_FLOW", "DIVIDEND_YIELD",
})


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


def test_edge_columns_come_from_adapter_or_pipeline():
    """边的每一列必须**有出处**：耦合层给的语义列，或本管线自己算的派生列。

    这条抓两类错，都很阴：
    1. **手写错名**（gold 四列带 `gold_` 前缀，写成 `cpi_surprise` 就匹配不上）；
    2. **改名没改干净**：`confounders` 在估计器里是 `df[c]` 直接取列，写错不会
       报错，只会被"全 NaN / 零方差"守卫**静默跳过**——配置上写着"控制了"，
       实际没控（就是 `SOCIAL_FINANCING` 那个坑）。
    """
    from shared.adapter import COLUMNS, Panel

    problems: dict[str, dict[str, str]] = {}
    for asset, panel in (("ashare", Panel.CAUSUAL_ASHARE),
                         ("gold", Panel.CAUSUAL_GOLD)):
        pkg, prepared = _panel(asset)
        # `COLUMNS[panel]` 就是 `columns_of(panel)`（adapter 只在包里导出前者）
        # 允许的出处 = 耦合层语义列 ∪ 本管线产出的列（prepare 的派生列）
        allowed = set(COLUMNS[panel]) | set(prepared.columns)
        bad = {col: who for col, who in _refs(pkg).items()
               if col not in allowed or col in LEGACY_COLS}
        if bad:
            problems[asset] = bad

    assert not problems, (
        f"有边引用了耦合层与管线都没有的列名（手写错名 / 改名漏改）：{problems}")


def test_no_legacy_uppercase_columns():
    """退役的大写列名不许再出现在面板里（`prepare()` 的派生列除外）。

    "两套列名"正是这一轮要拿掉的东西：留着一套就迟早两边漂移。
    """
    from causual.causual_core.cli import _load_panel

    for asset in ASSETS:
        pkg = _panel(asset)[0]
        raw = pkg.load_raw(_load_panel(asset))
        stale = sorted(set(raw.columns) & LEGACY_COLS)
        assert not stale, f"{asset} 面板里还有退役的大写列名：{stale}"


def test_ashare_panel_column_count():
    """面板列数钉死：列合并与派生列搬迁之后不该多也不该少。

    2026-11-15 改语义列名并掉三对（`CLOSE`+`CSI_ALL`、`EQUITY_SUPPLY`+
    `EQUITY_FIN_MONTHLY`、`CLOSE`+`GOLD_CNY`）；2026-11-16 六列派生量改名
    （数目不变，只是大小写）；2026-11-28 删 `SHIBOR3M` 边（连 `shibor3m` 列与
    `SHIBOR_CHG_BP` 派生列一起去掉）→ A 股 raw 35→34、prep 56→54。
    数字对不上说明有列被漏搬或重复加。
    """
    expect = {"ashare": (34, 54), "gold": (19, 28)}
    for asset, (want_raw, want_prep) in expect.items():
        pkg, prep = _panel(asset)
        from causual.causual_core.cli import _load_panel

        raw = pkg.load_raw(_load_panel(asset))
        assert raw.shape[1] == want_raw, (
            f"{asset} 面板 raw 列数 {raw.shape[1]}，期望 {want_raw}：{sorted(raw.columns)}")
        assert prep.shape[1] == want_prep, (
            f"{asset} 面板 prep 列数 {prep.shape[1]}，期望 {want_prep}")


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
