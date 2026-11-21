"""资金变量：信号定义 + 归一化 + 滚动分档。

    from experiment.hmm_state.money import load_signals, buckets, coverage

## 命名纪律（用户 2026-11-21 要求）

**不要把持仓量叫"分歧度"。** 未平仓合约增加可能反映参与度、套保需求、风险敞口扩大，
但**观测不到多空双方的观点分歧**。分歧度是待验证的解释，不是数据定义。

| 信号 | 口径 | 单位 |
|---|---|---|
| `basis_z` | 次月合约年化基差率的均值（截尾 ±50%） | — |
| `oi_level_rel` | 总持仓 / 自身 250 日均值 − 1 | 比例 |
| `oi_change_z` | 总持仓变化 ΔOI | 手 |
| `oi_change_pct_z` | 总持仓变化率 ΔOI / OI | 比例 |
| `margin_abs_z` | 两融余额变化 | 亿元 |
| `margin_pct_z` | 两融余额变化率 | % |
| `etf_abs_z` | 宽基 ETF 总份额变化 | 亿份 |
| `etf_pct_z` | 宽基 ETF 总份额变化率 | % |
| `turnover_z` | 成交额 / 20 日均额 − 1 | 比例 |

## 绝对 vs 相对（用户要求区分）

`*_abs_z` 是**绝对变化**（亿元 / 亿份 / 手），`*_pct_z` 是**相对变化**。
两者在不同波动环境下含义不同：绝对量放大可能只是市场变大，相对量才是"占比变了"。
**不能只用一种**，所以两个都算；报告里成对出现。

## 归一化纪律

1. 一律取**滚动 z**（250 日），因为 2015 与 2025 的量纲不可比。
2. 分档阈值用**滚动 250 日分位**（33%/67%），不用全样本分位。
3. 缺数据就留 NaN，分析时按行丢弃并报出剩余样本量。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pathlib import Path

Z_WIN = 250
BQ = (1 / 3, 2 / 3)
BASIS_CLIP = 0.5


def _roll_z(s, win: int = Z_WIN) -> pd.Series:
    mu = s.rolling(win, min_periods=win // 2).mean()
    sd = s.rolling(win, min_periods=win // 2).std()
    return (s - mu) / sd.replace(0, np.nan)


def _rolling_bucket(s: pd.Series) -> pd.Series:
    lo = s.rolling(Z_WIN, min_periods=Z_WIN // 2).quantile(BQ[0])
    hi = s.rolling(Z_WIN, min_periods=Z_WIN // 2).quantile(BQ[1])
    out = pd.Series(np.where(s <= lo, "低", np.where(s >= hi, "高", "中")), index=s.index)
    out[s.isna()] = np.nan
    return out


# ---------------------------------------------------------------- 原始量


def futures_raw() -> pd.DataFrame:
    """期货：基差（次月、年化、截尾）+ 持仓水平与变化。四品种按日聚合。"""
    path = Path(__file__).resolve().parents[2] / "data" / "股指期货基差与持仓.csv"
    if not path.exists():
        print("  [警告] 没有期货文件，先跑 experiment.hmm_state.fetch_futures")
        return pd.DataFrame()

    df = pd.read_csv(path, parse_dates=["date"])
    df = df.sort_values(["date", "variety", "days_to_expiry"])
    second = (df.groupby(["date", "variety"])
              .apply(lambda g: g.iloc[1] if len(g) > 1 else g.iloc[0],
                     include_groups=False).reset_index())
    second["ann"] = second["annual_basis_rate"].clip(-BASIS_CLIP, BASIS_CLIP)

    oi = df.groupby("date")["open_interest"].sum()
    oi_chg = df.groupby("date")["oi_chg"].sum()
    return pd.DataFrame({
        "basis_raw": second.groupby("date")["ann"].mean(),
        "oi_level_raw": oi / oi.rolling(250, min_periods=120).mean() - 1.0,
        "oi_chg_raw": oi_chg,
        "oi_chg_pct_raw": oi_chg / oi.replace(0, np.nan),
    })


def etf_raw(px: pd.DataFrame) -> pd.DataFrame:
    from datahub.port import get_port

    try:
        df = get_port().read("etf_shares")
    except Exception as e:  # noqa: BLE001
        print(f"  [警告] etf_shares 不可用：{type(e).__name__}")
        return pd.DataFrame()
    if df.empty:
        return pd.DataFrame()
    wide = df.pivot_table(index="date", columns="code", values="shares", aggfunc="sum")
    tot = wide.reindex(px.index).ffill(limit=5).sum(axis=1)
    chg = tot.diff(5)
    return pd.DataFrame({"etf_abs_raw": chg / 1e8,
                         "etf_pct_raw": (chg / tot.replace(0, np.nan)) * 100.0})


def margin_raw(px: pd.DataFrame) -> pd.DataFrame:
    from datahub.port import get_port

    try:
        mb = get_port().read("margin_balance")
    except Exception as e:  # noqa: BLE001
        print(f"  [警告] margin_balance 不可用：{type(e).__name__}")
        return pd.DataFrame()
    if isinstance(mb, pd.DataFrame):
        mb = mb.iloc[:, 0]
    mb = mb.reindex(px.index).ffill()
    chg = mb.diff(5)
    return pd.DataFrame({"margin_abs_raw": chg,
                         "margin_pct_raw": (chg / mb.replace(0, np.nan)) * 100.0})


def turnover_raw(px: pd.DataFrame) -> pd.Series:
    if "amount" not in px.columns:
        return pd.Series(dtype=float)
    amt = px["amount"].astype(float)
    return (amt / amt.rolling(20, min_periods=5).mean() - 1.0).rename("turnover_raw")


# ---------------------------------------------------------------- 总装


SIGNALS = {
    "basis_z": "基差（年化，次月均值）",
    "oi_level_rel": "持仓水平（相对 250 日均值）",
    "oi_change_z": "持仓变化（手）",
    "oi_change_pct_z": "持仓变化率",
    "margin_abs_z": "两融余额变化（亿元）",
    "margin_pct_z": "两融变化率",
    "etf_abs_z": "ETF 份额变化（亿份）",
    "etf_pct_z": "ETF 份额变化率",
    "turnover_z": "成交额 / 20 日均额",
}

#: 变化口径：绝对 / 相对。`basis_z` 是比率、`oi_level_rel` 是水平，都不属于"变化"。
KIND = {
    "basis_z": "态度",
    "oi_level_rel": "水平",
    "oi_change_z": "绝对变化",
    "oi_change_pct_z": "相对变化",
    "margin_abs_z": "绝对变化",
    "margin_pct_z": "相对变化",
    "etf_abs_z": "绝对变化",
    "etf_pct_z": "相对变化",
    "turnover_z": "相对变化",
}


def load_signals(px: pd.DataFrame) -> pd.DataFrame:
    raw = pd.DataFrame(index=px.index)
    fut = futures_raw()
    if not fut.empty:
        raw = raw.join(fut.reindex(px.index).ffill(limit=3))
    for part in (etf_raw(px), margin_raw(px)):
        if not part.empty:
            raw = raw.join(part.reindex(px.index))
    t = turnover_raw(px)
    if not t.empty:
        raw["turnover_raw"] = t

    out = pd.DataFrame(index=px.index)
    for name, src in (("basis_z", "basis_raw"),
                      ("oi_change_z", "oi_chg_raw"),
                      ("oi_change_pct_z", "oi_chg_pct_raw"),
                      ("margin_abs_z", "margin_abs_raw"),
                      ("margin_pct_z", "margin_pct_raw"),
                      ("etf_abs_z", "etf_abs_raw"),
                      ("etf_pct_z", "etf_pct_raw"),
                      ("turnover_z", "turnover_raw")):
        if src in raw.columns:
            out[name] = _roll_z(raw[src].astype(float))
    if "oi_level_raw" in raw.columns:
        # 水平量本身已是相对值，只需要一个温和的 z 让量纲统一
        out["oi_level_rel"] = _roll_z(raw["oi_level_raw"].astype(float))
    return out


def buckets(z: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({c: _rolling_bucket(z[c]) for c in z.columns}, index=z.index)


def coverage(z: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for c in z.columns:
        s = z[c].dropna()
        rows.append({"信号": c, "说明": SIGNALS.get(c, ""), "口径": KIND.get(c, ""),
                     "有效天": len(s),
                     "起始": s.index.min().date() if len(s) else None,
                     "截止": s.index.max().date() if len(s) else None})
    return pd.DataFrame(rows)
