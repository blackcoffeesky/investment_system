"""特征新鲜度回归测试：任何一列特征整体失效，必须当场红。

背景（2026-11-14 踩过）
----------------------
数据层把 ETF 代码从裸码改成带市场前缀（`etf_shares:sh510300`），
`state_assets/ashare/panel.py::_etf_dispersion` 还按裸码和 `GROUPS` 比对，
一个都匹配不上 → `ETF_DISPERSION` **整列 NaN**。后果不是报错，而是静默雪崩：

    HMM 的 valid_index 要求 FEATURES 全非空 → 一行都不认领
    → 标签停在旧日期（外推没料）→ 卡片「没有状态标签」被拒
    → 单点后验为空 → 页面 transition 没有 stay_prob → 模板报错

所以这条测试盯的是「每列最后一次有值是哪天」，而不是「有没有抛异常」。
"""

from __future__ import annotations

# 允许落后 csi_all 的交易日数：宏观 / ETF 表本身有 lag_days，
# 但「整列 NaN」或「停在几个月前」不能容忍。
MAX_LAG_TRADING_DAYS = 5


def test_feature_columns_reach_latest_trading_day():
    from state_layer import state_assets
    from state_layer.cli import load_series

    # ---- ① 代码归一：数据层前缀怎么变，都要能对上 GROUPS
    from state_layer.state_assets.ashare.panel import bare_code

    for raw, want in (("sh510300", "510300"), ("sz159919", "159919"),
                      ("c510300", "510300"), ("510300", "510300"),
                      ("510300.SH", "510300")):
        assert bare_code(raw) == want, f"{raw} 归一成 {bare_code(raw)}，应为 {want}"

    # ---- ② 每列特征的最后有效日期
    asset = "ashare"
    pkg = state_assets.load(asset)
    series, meta = load_series(asset)
    panel = pkg.panel(series, meta)

    close = panel["CLOSE"].dropna()
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
