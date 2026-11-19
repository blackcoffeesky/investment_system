"""把指标拼成候选矩阵（研究用）。

**这里允许直接取数**——`tests/cross/test_layering.py` 跳过 `experiment/`。
生产代码不行：那边只能通过 `get_data` 在装配点取，核心代码只收 DataFrame。

取哪些数、为什么：

| 来源 | 用途 |
|---|---|
| `csi_all` | 指数 OHLCV + 成交额 → 趋势/波动/量能 |
| `stock_daily` | 个股横截面 → 宽度、截面离散度（市场内部一致性） |
| `margin_balance` | 两融余额 → 杠杆资金 |
| `etf_shares` | 宽基 ETF 合计份额 → 申购赎回 |
"""

from __future__ import annotations

import pandas as pd

from experiment.state_lab import indicators as I


def _index_panel() -> pd.DataFrame:
    """指数 OHLCV + 成交额（日频）。"""
    from datahub.port import get_port

    got = get_port().series_map(["csi_all"])
    cols = {k.split(":", 1)[1]: v for k, v in got.items() if k.startswith("csi_all:")}
    return pd.DataFrame(cols).sort_index()


def _breadth() -> pd.DataFrame:
    from datahub.port import get_port

    got = get_port().series_map(["breadth"])
    cols = {k.split(":", 1)[1]: v for k, v in got.items() if k.startswith("breadth:")}
    return pd.DataFrame(cols).sort_index()


def _xs_dispersion() -> pd.DataFrame:
    """个股截面离散度——要读 1000 万行，比较慢（约 20~30 秒）。"""
    from datahub.port import get_port

    from datahub.port import get_port as _gp

    _ = _gp()
    from datahub.fin.read import read_long

    sd = read_long("stock_daily", columns=["close"])   # code/date 是索引，只能传值列
    return I.xs_dispersion_pct(sd)


def _flow() -> pd.DataFrame:
    """资金层：两融余额 + ETF 合计份额。"""
    from datahub.port import get_port

    got = get_port().series_map(["margin_balance", "etf_shares"])
    margin = got.get("margin_balance")          # single 表的 key 就是表名
    sh = {k: v for k, v in got.items() if k.startswith("etf_shares:")}

    out = pd.DataFrame(index=(margin.index if margin is not None else None))
    if margin is not None:
        out["margin_balance"] = margin
    if sh:
        # ETF 份额是 long 表的多个 key；按日求和
        wide = pd.DataFrame(sh)
        out["etf_total_shares"] = wide.sum(axis=1, min_count=1)
    return out.sort_index()


def build(include_xs: bool = True) -> pd.DataFrame:
    """候选指标矩阵。**全部是原始指标，不做标准化**（标准化交给下游/模型）。"""
    idx = _index_panel()
    close, high, low = idx["close"], idx["high"], idx["low"]
    open_ = idx["open"] if "open" in idx else close

    feats: dict[str, pd.Series] = {}

    # ---- 趋势（方向效率 + 显著性 + 方向）
    for n in (20, 120):
        feats[f"er_{n}"] = I.er(close, n)
        feats[f"drift_snr_{n}"] = I.drift_snr(close, n)
        feats[f"cum_ret_{n}_pct"] = I.cum_ret_pct(close, n)
    feats["ma_pos_120_pct"] = I.ma_pos_pct(close, 120)
    feats["drawdown_250_pct"] = I.drawdown_pct(close, 250)
    feats["autocorr_20"] = I.autocorr(close, 20)
    feats["vr_5_100"] = I.variance_ratio(close, 5, 100)
    feats["hurst_250"] = I.hurst_diag(close, 250)     # 仅诊断

    # ---- 波动（幅度）
    for n in (20, 60):
        feats[f"rv_{n}_pct"] = I.rv_pct(close, n)
    feats["parkinson_20_pct"] = I.parkinson_pct(high, low, 20)
    feats["gk_20_pct"] = I.garman_klass_pct(open_, high, low, close, 20)
    feats["atr_14_pct"] = I.atr_pct(high, low, close, 14)
    feats["rv_pct_rank_250"] = I.vol_pct_rank(feats["rv_20_pct"], 250)
    feats["vol_of_vol_20"] = I.vol_of_vol(feats["rv_20_pct"], 20)

    # ---- 量能 / 宽度 / 结构
    if "amount" in idx:
        feats["amount_z_250"] = I.level_z(idx["amount"], 250)
    br = _breadth()
    if "adv" in br and "dec" in br:
        feats["adv_ratio"] = I.adv_ratio(br["adv"], br["dec"])
    if {"limit_up", "limit_down", "total"} <= set(br.columns):
        feats["limit_net"] = I.limit_net(br["limit_up"], br["limit_down"], br["total"])
    if include_xs:
        xs = _xs_dispersion()
        feats["xs_std_pct"] = xs["xs_std_pct"]

    out = pd.DataFrame(feats)

    # ---- 资金
    flow = _flow()
    if "margin_balance" in flow:
        out["margin_chg_5_pct"] = I.chg_pct(flow["margin_balance"], 5)
        out["margin_z_250"] = I.level_z(flow["margin_balance"], 250)
    if "etf_total_shares" in flow:
        out["etf_shares_chg_20_pct"] = I.chg_pct(flow["etf_total_shares"], 20)

    return out.sort_index()
