"""资金变量：四类信号 + 归一化 + 滚动分档（L2 的数据层）。

    from experiment.hmm_state.money import load_signals, buckets

## 四类信号（用户 2026-11-21 定的口径）

| 家族 | 数据 | 代表什么 | 方向解读 |
|---|---|---|---|
| **期货基差** | 中金所 IF/IM 年化基差率 | 期货资金的态度 | 升水（>0）= 乐观；贴水 = 对冲/悲观 |
| **期货持仓** | 总持仓量（OI） | **分歧度** | 持仓放大 = 多空分歧上升，不是方向 |
| **ETF 份额** | 上交所宽基 ETF 份额 | 中长期资金态度 | 份额增加 = 有资金借 ETF 进场 |
| **两融** | 沪深两融余额 | 杠杆资金 | 余额升 = 加杠杆 |
| **成交额** | 指数成交额 / 20 日均额 | 场内活跃度 | 放量 = 参与度上升 |

## 三条口径纪律

1. **全部先归一化**（滚动 z, 250 日）。原始量纲在高低波动期差几个数量级，
   不归一化的话，条件分析只是在复述"现在波动高"。
2. **分档阈值只能用过去**：滚动 250 日的 33%/67% 分位。
3. **缺失不补**：某个信号缺数据就留 NaN，CJK 分析时按行丢弃并报出剩余样本量。

## 已知覆盖问题（会写进报告）

- 期货只有 **IF（2015 起）和 IM（2022 起）**：IC/IH 的现货指数本地库里没有，
  akshare 在这台机器上被代理挡住（`ProxyError`）。所以 2022 年之前只有 IF。
- 基差年化在**临近交割**时会被极小分母放大，这里对年化值做 ±50% 截尾。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pathlib import Path

Z_WIN = 250
BQ = (1 / 3, 2 / 3)
BASIS_CLIP = 0.5


def _roll_z(s: pd.Series, win: int = Z_WIN) -> pd.Series:
    mu = s.rolling(win, min_periods=win // 2).mean()
    sd = s.rolling(win, min_periods=win // 2).std()
    return (s - mu) / sd.replace(0, np.nan)


def _rolling_bucket(s: pd.Series) -> pd.Series:
    lo = s.rolling(Z_WIN, min_periods=Z_WIN // 2).quantile(BQ[0])
    hi = s.rolling(Z_WIN, min_periods=Z_WIN // 2).quantile(BQ[1])
    out = pd.Series(np.where(s <= lo, "低", np.where(s >= hi, "高", "中")), index=s.index)
    out[s.isna()] = np.nan
    return out


# ---------------------------------------------------------------- 各家族


def futures_signals() -> pd.DataFrame:
    """基差（年化、截尾）与持仓（总 OI）。多品种按日聚合。"""
    path = Path(__file__).resolve().parents[2] / "data" / "股指期货基差与持仓.csv"
    if not path.exists():
        print("  [警告] 没有期货文件，先跑 experiment.hmm_state.fetch_futures")
        return pd.DataFrame()

    df = pd.read_csv(path, parse_dates=["date"])
    # 基差：只用次近月合约（当月临近交割，年化会被分母放大）
    df = df.sort_values(["date", "variety", "days_to_expiry"])
    second = (df.groupby(["date", "variety"])
              .apply(lambda g: g.iloc[1] if len(g) > 1 else g.iloc[0],
                     include_groups=False).reset_index())
    second["ann"] = second["annual_basis_rate"].clip(-BASIS_CLIP, BASIS_CLIP)

    basis = second.groupby("date")["ann"].mean().rename("basis_raw")
    oi = df.groupby("date")["open_interest"].sum().rename("oi_raw")
    oi_chg = df.groupby("date")["oi_chg"].sum().rename("oi_chg_raw")
    # ⚠️ 总持仓量有长期上升趋势（品种扩容、市场成长）。直接做滚动 z 会变成"永远偏高"，
    # 事件研究里体现为切换前后 oi_z 恒在 1.3~1.4 —— 那是趋势，不是分歧度。
    # 先除以自身 250 日均值去掉趋势，再做 z。
    oi_rel = (oi / oi.rolling(250, min_periods=120).mean() - 1.0).rename("oi_rel_raw")
    return pd.DataFrame({"basis_raw": basis, "oi_raw": oi_rel,
                         "oi_chg_raw": oi_chg})
    return pd.DataFrame({"basis_raw": basis, "oi_raw": oi, "oi_chg_raw": oi_chg})


def etf_signals(px: pd.DataFrame) -> pd.Series:
    """宽基 ETF 份额的 5 日变化率（按当日总份额加权）。"""
    from datahub.port import get_port

    try:
        df = get_port().read("etf_shares")
    except Exception as e:  # noqa: BLE001
        print(f"  [警告] etf_shares 不可用：{type(e).__name__}")
        return pd.Series(dtype=float, name="etf_raw")

    if df.empty:
        return pd.Series(dtype=float, name="etf_raw")
    wide = df.pivot_table(index="date", columns="code", values="shares", aggfunc="sum")
    wide = wide.reindex(px.index).ffill(limit=5)
    tot = wide.sum(axis=1)
    return (tot.pct_change(5) * 100.0).rename("etf_raw")


def margin_signals(px: pd.DataFrame) -> pd.Series:
    from datahub.port import get_port

    try:
        mb = get_port().read("margin_balance")
    except Exception as e:  # noqa: BLE001
        print(f"  [警告] margin_balance 不可用：{type(e).__name__}")
        return pd.Series(dtype=float, name="margin_raw")
    if isinstance(mb, pd.DataFrame):
        mb = mb.iloc[:, 0]
    mb = mb.reindex(px.index).ffill()
    return (mb.pct_change(5) * 100.0).rename("margin_raw")


def turnover_signals(px: pd.DataFrame) -> pd.Series:
    if "amount" not in px.columns:
        return pd.Series(dtype=float, name="turnover_raw")
    amt = px["amount"].astype(float)
    return (amt / amt.rolling(20, min_periods=5).mean() - 1.0).rename("turnover_raw")


# ---------------------------------------------------------------- 总装


SIGNALS = {
    "basis_z": "期货基差（年化，次月）",
    "oi_z": "期货持仓（分歧度）",
    "oi_chg_z": "期货持仓变化",
    "etf_z": "ETF 份额 5 日变化",
    "margin_z": "两融 5 日变化",
    "turnover_z": "成交额 / 20 日均额",
}

#: 哪些是"越大越强"、哪些是"越大越分歧"（只影响解读，不影响计算）
KIND = {"basis_z": "态度", "oi_z": "分歧", "oi_chg_z": "分歧",
        "etf_z": "资金", "margin_z": "杠杆", "turnover_z": "活跃"}


def load_signals(px: pd.DataFrame) -> pd.DataFrame:
    """返回 z 分数矩阵（列见 SIGNALS）。"""
    raw = pd.DataFrame(index=px.index)
    fut = futures_signals()
    if not fut.empty:
        raw = raw.join(fut.reindex(px.index).ffill(limit=3))
    raw["etf_raw"] = etf_signals(px)
    raw["margin_raw"] = margin_signals(px)
    raw["turnover_raw"] = turnover_signals(px)

    out = pd.DataFrame(index=px.index)
    for z, src in (("basis_z", "basis_raw"), ("oi_z", "oi_raw"), ("oi_chg_z", "oi_chg_raw"),
                   ("etf_z", "etf_raw"), ("margin_z", "margin_raw"),
                   ("turnover_z", "turnover_raw")):
        if src in raw.columns:
            out[z] = _roll_z(raw[src].astype(float))
    return out


def buckets(z: pd.DataFrame) -> pd.DataFrame:
    """每个信号按滚动分位切「低/中/高」。"""
    return pd.DataFrame({c: _rolling_bucket(z[c]) for c in z.columns}, index=z.index)


def coverage(z: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for c in z.columns:
        s = z[c].dropna()
        rows.append({"信号": c, "说明": SIGNALS.get(c, ""), "类型": KIND.get(c, ""),
                     "有效天": len(s),
                     "起始": s.index.min().date() if len(s) else None,
                     "截止": s.index.max().date() if len(s) else None})
    return pd.DataFrame(rows)
