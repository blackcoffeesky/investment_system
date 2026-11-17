"""状态层特征新鲜度 + 声明一致性回归测试。

背景（2026-11-14 踩过）
----------------------
数据层把 ETF 代码从裸码改成带市场前缀（`etf_shares:sh510300`），
`state_assets/ashare/panel.py::_etf_dispersion` 还按裸码和 `GROUPS` 比对，
一个都匹配不上 → `ETF_DISPERSION` **整列 NaN**。后果不是报错，而是静默雪崩：

    HMM 的 valid_index 要求 FEATURES 全非空 → 一行都不认领
    → 标签停在旧日期（外推没料）→ 卡片「没有状态标签」被拒
    → 单点后验为空 → 页面 transition 没有 stay_prob → 模板报错

所以第一条盯的是「每列最后一次有值是哪天」，而不是「有没有抛异常」。

背景（2026-11-15，panel.py 退役）
--------------------------------
"这个资产要哪些表"从 `panel.py` 搬进了 `config.REQUIRED_TABLES`。它**不参与取数**
（`get_data` 按 adapter 自己的声明取），所以两份声明可能各自漂——第二条用
`tables_of()` 把它们钉死。
"""

from __future__ import annotations

from datahub.fin.derive import bare_code
from shared.adapter import Panel, get_data, tables_of

# 允许落后 csi_all 的交易日数：宏观 / ETF 表本身有 lag_days，
# 但「整列 NaN」或「停在几个月前」不能容忍。
MAX_LAG_TRADING_DAYS = 5

# 资产 → 耦合层面板（状态层用哪一组）
PANELS = {"ashare": Panel.STATE_ASHARE, "gold": Panel.STATE_GOLD}


def test_feature_columns_reach_latest_trading_day():
    from state_layer import state_assets

    # ---- ① 代码归一：数据层前缀怎么变，都要能对上（归一逻辑在 derive.etf_group_shares）
    for raw, want in (("sh510300", "510300"), ("sz159919", "159919"),
                      ("c510300", "510300"), ("510300", "510300"),
                      ("510300.SH", "510300")):
        assert bare_code(raw) == want, f"{raw} 归一成 {bare_code(raw)}，应为 {want}"

    # ---- ② 每列特征的最后有效日期
    asset = "ashare"
    pkg = state_assets.load(asset)
    panel = get_data(PANELS[asset])

    close = panel["close"].dropna()      # adapter 给的是小写语义列名
    if close.empty:
        return                      # 空库（新环境）不判，与其它测试一致

    feats = pkg.features.build(panel)
    anchor = close.index[-1]

    bad: dict[str, str] = {}
    for col in pkg.config.FEATURES:
        if col not in feats:
            bad[col] = "特征矩阵里没有这一列"
            continue
        valid = feats[col].dropna()
        if valid.empty:
            bad[col] = "整列 NaN（数据源没接上？）"
            continue
        lag = int((close.index > valid.index[-1]).sum())
        if lag > MAX_LAG_TRADING_DAYS:
            bad[col] = (f"最后有效日 {valid.index[-1].date()}，"
                        f"落后 {lag} 个交易日")

    assert not bad, (f"{asset} 有特征列失效（csi_all 最近交易日 "
                     f"{anchor.date()}）：{bad}")


def test_required_tables_match_adapter():
    """`config.REQUIRED_TABLES` 必须与 adapter 实际读的表**完全相等**。

    `get_data` 自己按 adapter 的 `_SPECS`/`_EXTRA_TABLES` 取数，config 那份只是
    `port.required_tables()` 的装配清单（service / causual cli 拿它做并集）。
    两份漂了不会报错，只会让下游悄悄少取表——所以在这里钉死。
    """
    from state_layer import state_assets

    for asset, panel in PANELS.items():
        declared = set(state_assets.load(asset).config.REQUIRED_TABLES)
        actual = set(tables_of(panel))
        assert declared == actual, (
            f"{asset} 的 REQUIRED_TABLES 与 adapter 声明不一致："
            f"config 多出 {sorted(declared - actual)}、"
            f"adapter 多出 {sorted(actual - declared)}")
