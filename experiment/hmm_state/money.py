"""资金信号：期货基差持仓、两融、ETF份额、成交额；滚动Z、滚动3分桶
禁止把持仓量命名为分歧度，持仓代表参与度/风险敞口
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from pathlib import Path
from experiment.hmm_state.config import Z_WIN, BQ, BASIS_CLIP

SIGNALS = {
    "basis_z": "基差（年化次月均值）",
    "oi_level_rel": "持仓水平相对250日均值",
    "oi_change_z": "持仓变化（手，绝对）",
    "oi_change_pct_z": "持仓变化率（相对）",
    "margin_abs_z": "两融余额变化亿元",
    "margin_pct_z": "两融余额变化率%",
    "etf_abs_z": "ETF份额变化亿份",
    "etf_pct_z": "ETF份额变化率%",
    "turnover_z": "成交额相对20日均额",
}
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


def _roll_z(s: pd.Series, win=Z_WIN) -> pd.Series:
    mu = s.rolling(win, min_periods=win//2).mean()
    sd = s.rolling(win, min_periods=win//2).std()
    return (s - mu) / sd.replace(0, np.nan)


def _rolling_bucket(s: pd.Series) -> pd.Series:
    lo = s.rolling(Z_WIN, min_periods=Z_WIN//2).quantile(BQ[0])
    hi = s.rolling(Z_WIN, min_periods=Z_WIN//2).quantile(BQ[1])
    out = pd.Series(np.where(s <= lo, "低", np.where(s >= hi, "高", "中")), index=s.index)
    out[s.isna()] = np.nan
    return out


def futures_raw():
    path = Path(__file__).resolve().parents[2] / "data" / "股指期货基差与持仓.csv"
    if not path.exists():
        print("[WARN]缺少期货文件，先运行fetch_futures")
        return pd.DataFrame()
    df = pd.read_csv(path, parse_dates=["date"])
    df = df.sort_values(["date", "variety", "days_to_expiry"])
    # 取次月合约
    second = df.groupby(["date", "variety"]).apply(
        lambda g: g.iloc[1] if len(g)>1 else g.iloc[0], include_groups=False
    ).reset_index()
    second["ann"] = second["annual_basis_rate"].clip(-BASIS_CLIP, BASIS_CLIP)
    oi = df.groupby("date")["open_interest"].sum()
    oi_chg = df.groupby("date")["oi_chg"].sum()
    return pd.DataFrame({
        "basis_raw": second.groupby("date")["ann"].mean(),
        "oi_level_raw": oi / oi.rolling(250, min_periods=120).mean() - 1.0,
        "oi_chg_raw": oi_chg,
        "oi_chg_pct_raw": oi_chg / oi.replace(0, np.nan),
    })


def etf_raw(px: pd.DataFrame):
    from datahub.port import get_port
    try:
        df = get_port().read("etf_shares")
    except Exception as e:
        print(f"[WARN]etf_shares不可用 {e}")
        return pd.DataFrame()
    wide = df.pivot_table(index="date", columns="code", values="shares", aggfunc="sum")
    tot = wide.reindex(px.index).ffill(limit=5).sum(axis=1)
    chg = tot.diff(5)
    return pd.DataFrame({
        "etf_abs_raw": chg / 1e8,
        "etf_pct_raw": (chg / tot.replace(0, np.nan)) *100
    })


def margin_raw(px: pd.DataFrame):
    from datahub.port import get_port
    try:
        mb = get_port().read("margin_balance")
    except Exception as e:
        print(f"[WARN]margin_balance不可用 {e}")
        return pd.DataFrame()
    if isinstance(mb, pd.DataFrame):
        mb = mb.iloc[:,0]
    mb = mb.reindex(px.index).ffill()
    chg = mb.diff(5)
    return pd.DataFrame({
        "margin_abs_raw": chg,
        "margin_pct_raw": (chg/mb.replace(0, np.nan))*100
    })


def turnover_raw(px: pd.DataFrame) -> pd.Series:
    if "amount" not in px.columns:
        return pd.Series(dtype=float)
    amt = px["amount"].astype(float)
    return (amt / amt.rolling(20, min_periods=5).mean() - 1.0).rename("turnover_raw")


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
    mapping = [
        ("basis_z", "basis_raw"),
        ("oi_change_z", "oi_chg_raw"),
        ("oi_change_pct_z", "oi_chg_pct_raw"),
        ("margin_abs_z", "margin_abs_raw"),
        ("margin_pct_z", "margin_pct_raw"),
        ("etf_abs_z", "etf_abs_raw"),
        ("etf_pct_z", "etf_pct_raw"),
        ("turnover_z", "turnover_raw"),
    ]
    for name, src in mapping:
        if src in raw.columns:
            out[name] = _roll_z(raw[src].astype(float))
    if "oi_level_raw" in raw.columns:
        out["oi_level_rel"] = _roll_z(raw["oi_level_raw"].astype(float))
    return out


def buckets(z_df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({c:_rolling_bucket(z_df[c]) for c in z_df.columns}, index=z_df.index)


def coverage(z_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for c in z_df.columns:
        s = z_df[c].dropna()
        rows.append({
            "信号": c, "说明": SIGNALS.get(c,""), "口径": KIND.get(c,""),
            "有效天": len(s),
            "起始": s.index.min().date() if len(s) else None,
            "截止": s.index.max().date() if len(s) else None
        })
    return pd.DataFrame(rows)
