"""趋势 × 波动 的 9 格表：先看格子够不够样本，再看它有没有交易含义。

    python -m experiment.state_lab.cross_tab

## 分层口径（第一版，刻意简单）

**趋势 3 档** —— 用长期漂移的**统计显著性**，不用涨跌符号：

```
drift_snr_120 ≥ +1  → 上行
drift_snr_120 ≤ −1  → 下行
其余                → 震荡
```

为什么不用 `cum_ret_120` 的符号：那样"横盘微涨 0.3%"会被判成上行，
而它和"上行 8%"是两种状态。用 SNR 是把"涨了"和"涨得可信"分开。

**波动 3 档** —— GK 与 ATR 的滚动分位取平均，再按三分位切：

```
vol = mean(rank(gk_20), rank(atr_14))     # 都在各自的 250 日窗口内
vol < 1/3        → 低波
1/3 ~ 2/3        → 中波
> 2/3            → 高波
```

**为什么保留 ATR（虽然它和 GK 高度相关）**：GK 只用四价、不含跳空；
ATR 用真实波幅、含隔夜跳空。A 股隔夜跳空是真实风险，所以两者都留。

## 判据（按顺序）

1. **样本量**：每格 ≥ 60 个交易日、≥ 15 个独立游程 —— 不满足的格子**不许命名**
2. **持续期**：中位游程长度 ≥ 10 交易日 —— 否则实盘来不及反应
3. **条件分布**：未来 20 日收益的 5/25/50/75/95 分位 + 胜率
   —— **只看均值会骗人**："能赚但先亏 15%"和"稳赚"均值一样
"""

from __future__ import annotations

import numpy as np
import pandas as pd

OUT_NAME = "cross_tab"


def load_matrix() -> pd.DataFrame:
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "out" / "state_lab" / "matrix.csv"
    if not p.exists():
        from experiment.state_lab import matrix
        m = matrix.build()
        m.to_csv(p, encoding="utf-8-sig")
        return m
    return pd.read_csv(p, index_col=0, parse_dates=True)


def trend_bucket(m: pd.DataFrame, snr: float = 1.0) -> pd.Series:
    s = m["drift_snr_120"]
    out = pd.Series("震荡", index=m.index, dtype=object)
    out[s >= snr] = "上行"
    out[s <= -snr] = "下行"
    out[s.isna()] = np.nan
    return out


def vol_bucket(m: pd.DataFrame) -> pd.Series:
    gk_rank = m["gk_20_pct"].rolling(250, min_periods=120).apply(
        lambda x: float((x[:-1] < x[-1]).mean()), raw=True)
    atr_rank = m["atr_14_pct"].rolling(250, min_periods=120).apply(
        lambda x: float((x[:-1] < x[-1]).mean()), raw=True)
    vol = (gk_rank + atr_rank) / 2.0
    out = pd.Series(np.nan, index=m.index, dtype=object)
    out[vol < 1 / 3] = "低波"
    out[(vol >= 1 / 3) & (vol < 2 / 3)] = "中波"
    out[vol >= 2 / 3] = "高波"
    return out


def episodes(labels: pd.Series) -> pd.Series:
    """每段连续相同标签的长度，广播回原索引（只对非空行）。"""
    s = labels.dropna()
    run = (s != s.shift()).cumsum()
    size = s.groupby(run).transform("size")
    return size


def build_table(m: pd.DataFrame, fwd: int = 20) -> tuple[pd.DataFrame, pd.Series]:
    close = m["close"]
    tr = trend_bucket(m)
    vo = vol_bucket(m)
    cell = tr.astype(object) + "-" + vo.astype(object)
    cell = cell.where(tr.notna() & vo.notna())

    epi = episodes(cell)
    fwd_ret = (close.shift(-fwd) / close - 1.0) * 100.0

    rows = []
    for name, grp in cell.dropna().groupby(cell.dropna()):
        idx = grp.index
        r = fwd_ret.reindex(idx).dropna()
        rows.append({
            "格子": name,
            "交易日": len(idx),
            "游程数": int(epi.reindex(idx).pipe(lambda x: (x != x.shift()).sum())),
            "中位持续": float(epi.reindex(idx).median()),
            "占比%": round(len(idx) / len(cell.dropna()) * 100, 1),
            "未来20日均值%": round(float(r.mean()), 2) if len(r) else np.nan,
            "胜率%": round(float((r > 0).mean()) * 100, 1) if len(r) else np.nan,
            "p05": round(float(r.quantile(0.05)), 2) if len(r) else np.nan,
            "p25": round(float(r.quantile(0.25)), 2) if len(r) else np.nan,
            "中位%": round(float(r.median()), 2) if len(r) else np.nan,
            "p75": round(float(r.quantile(0.75)), 2) if len(r) else np.nan,
            "p95": round(float(r.quantile(0.95)), 2) if len(r) else np.nan,
        })
    order = [f"{t}-{v}" for t in ("上行", "震荡", "下行") for v in ("低波", "中波", "高波")]
    tab = pd.DataFrame(rows).set_index("格子").reindex(order).dropna(how="all")
    return tab, fwd_ret


def verdict(tab: pd.DataFrame) -> list[str]:
    """按判据逐格打标：能不能用。"""
    notes: list[str] = []
    for name, r in tab.iterrows():
        problems = []
        if r["交易日"] < 60:
            problems.append(f"样本 {int(r['交易日'])} < 60 天")
        if r["游程数"] < 15:
            problems.append(f"独立游程 {int(r['游程数'])} < 15")
        if r["中位持续"] < 10:
            problems.append(f"中位持续 {r['中位持续']:.0f} < 10 天")
        notes.append(f"- `{name}`: " + ("可用" if not problems else "⚠️ " + "；".join(problems)))
    return notes


def robustness(m: pd.DataFrame, cells) -> tuple[pd.DataFrame, pd.DataFrame]:
    """同一格子分前后两段，看均值是否反号。"""
    tr, vo = trend_bucket(m), vol_bucket(m)
    cell = (tr.astype(object) + "-" + vo.astype(object)).where(tr.notna() & vo.notna())
    fwd = (m["close"].shift(-20) / m["close"] - 1.0) * 100.0
    df = pd.DataFrame({"cell": cell, "fwd": fwd}).dropna()
    df["半段"] = ["前段(<=2018)" if d.year <= 2018 else "后段(>=2019)" for d in df.index]
    mean = df.pivot_table(index="cell", columns="半段", values="fwd", aggfunc="mean").round(2)
    n = df.pivot_table(index="cell", columns="半段", values="fwd", aggfunc="size")
    return mean.reindex(cells), n.reindex(cells)


def forward_vol(m: pd.DataFrame, cells) -> pd.DataFrame:
    """未来 20 日已实现波动（年化 %）。状态若能预测风险，这张表应该稳定且单调。"""
    tr, vo = trend_bucket(m), vol_bucket(m)
    cell = (tr.astype(object) + "-" + vo.astype(object)).where(tr.notna() & vo.notna())
    r = m["close"].pct_change()
    fvol = r.rolling(20).std().shift(-20) * np.sqrt(252) * 100.0
    df = pd.DataFrame({"cell": cell, "fvol": fvol}).dropna()
    df["半段"] = ["前段(<=2018)" if d.year <= 2018 else "后段(>=2019)" for d in df.index]
    piv = df.pivot_table(index="cell", columns="半段", values="fvol", aggfunc="median").round(1)
    return piv.reindex(cells)


def main() -> int:
    from pathlib import Path

    out = Path(__file__).resolve().parents[1] / "out" / "state_lab"
    out.mkdir(parents=True, exist_ok=True)

    from experiment.state_lab.matrix import _index_panel

    m = load_matrix()
    # 矩阵里只有指标，收盘价单独取（只看分布，不进模型）
    m = m.join(_index_panel()[["close"]], how="left").dropna(subset=["close"])
    tab, fwd = build_table(m)
    tab.to_csv(out / f"{OUT_NAME}.csv", encoding="utf-8-sig")

    robust = robustness(m, tab.index)
    fvol = forward_vol(m, tab.index)

    md = ["# 趋势 × 波动：9 格表（第一版）\n",
          f"区间 {m.index.min().date()} ~ {m.index.max().date()}，"
          f"有效日 {int(tab['交易日'].sum())} 天（趋势/波动任一缺失的日子不计）\n",
          tab.to_string(), "\n", "## 逐格判定\n", *verdict(tab), "\n",
          "## 稳健性：前后半段的「未来 20 日均值」\n",
          "**同一格子在不同年代符号相反 ⇒ 这个均值不能当预期收益用。**\n",
          robust[0].to_string(), "\n", "样本数：\n", robust[1].to_string(), "\n",
          "## 未来 20 日已实现波动（年化 %，中位数）\n",
          "**波动是可以被预测的那一半。**\n",
          fvol.to_string(), "\n"]
    (out / f"{OUT_NAME}.md").write_text("\n".join(md), encoding="utf-8")
    print(tab.to_string())
    print()
    print("\n".join(verdict(tab)))
    print(f"\n→ {out / (OUT_NAME + '.md')}")
    return 0
