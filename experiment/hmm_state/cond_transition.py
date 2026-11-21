"""资金行为 × 状态 → 条件转移（第一版）。

    python -m experiment.hmm_state.cond_transition

## 想回答什么

现有 HMM 的转移矩阵是 `P(S_{t+1} | S_t)`——只看状态本身。
这里加一层条件：**给定今天是状态 i，且今天出现了某种资金行为 m，明天去各状态的概率**。

```
P(S_{t+1}=j | S_t=i, M_t=m)   vs   P(S_{t+1}=j | S_t=i)
                 ↑ 资金行为                    ↑ 基准
```

有意义的是**抬升（lift）**，不是条件概率本身：转移概率高的格子本来就容易留，
要看"资金行为把某个去向抬高了多少"。

## 三条纪律（不然结论会是假的）

1. **资金信号必须归一化**。高波动时成交额、两融变动天然更大，
   不归一化的话，条件分析只是在复述"现在波动高"——那是已经知道的事。
   这里用**相对 20 日均值**的偏离，再取 250 日滚动 z。
2. **分档阈值只能用过去**。用滚动 250 日的 33%/67% 分位切档，
   **不用全样本分位**（那是未来信息）。
3. **小样本不给结论**。每行必须带 n；n < 30 只标出、不解释。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MIN_N = 30
Z_WIN = 250


# ---------------------------------------------------------------- 资金信号


def _rolling_z(s: pd.Series, win: int = Z_WIN) -> pd.Series:
    mu = s.rolling(win, min_periods=win // 2).mean()
    sd = s.rolling(win, min_periods=win // 2).std()
    return (s - mu) / sd.replace(0, np.nan)


def _rolling_bucket(s: pd.Series, win: int = Z_WIN) -> pd.Series:
    """按**滚动**分位切三档：低 / 中 / 高（阈值只来自过去）。"""
    lo = s.rolling(win, min_periods=win // 2).quantile(1 / 3)
    hi = s.rolling(win, min_periods=win // 2).quantile(2 / 3)
    out = pd.Series(np.where(s <= lo, "低", np.where(s >= hi, "高", "中")), index=s.index)
    out[s.isna()] = np.nan
    return out


def money_signals(px: pd.DataFrame) -> pd.DataFrame:
    """两个可得的 A 股资金/活跃度信号（都做了归一化）。"""
    from datahub.port import get_port

    out = pd.DataFrame(index=px.index)

    # ① 两融余额 5 日变化率 → 杠杆资金进出
    try:
        mb = get_port().read("margin_balance")
        if isinstance(mb, pd.Series):
            mb = mb.reindex(px.index).ffill()
            out["margin_z"] = _rolling_z(mb.pct_change(5) * 100.0)
    except Exception as e:  # noqa: BLE001
        print(f"  [警告] margin_balance 不可用：{type(e).__name__}")

    # ② 成交额 / 20 日均额 − 1 → 活跃度（不是净流入，只是"有没有人交易"）
    if "amount" in px.columns:
        amt = px["amount"].astype(float)
        out["turnover_z"] = _rolling_z(amt / amt.rolling(20, min_periods=5).mean() - 1.0)

    return out


# ---------------------------------------------------------------- 条件转移


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (np.nan, np.nan)
    p = k / n
    d = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / d
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, mid - half), min(1.0, mid + half))


def conditional_table(state: pd.Series, signal: pd.Series) -> pd.DataFrame:
    """每个 (状态, 资金档) → 下一状态的分布，附 n、lift 与 Wilson CI。"""
    nxt = state.shift(-1)
    ok = nxt.notna() & state.notna()
    base = pd.crosstab(state[ok], nxt[ok], normalize="index")     # P(next|state)

    rows = []
    for (s, bucket), g in pd.DataFrame({"s": state[ok], "n": nxt[ok],
                                        "m": signal.reindex(state.index)[ok]}).dropna().groupby(["s", "m"]):
        n = len(g)
        dist = g["n"].value_counts(normalize=True)
        for to in base.columns:
            p_cond = float(dist.get(to, 0.0))
            p_base = float(base.loc[s, to])
            lo, hi = _wilson(int((g["n"] == to).sum()), n)
            rows.append({"状态": s, "资金档": bucket, "去向": to, "n": n,
                         "条件概率": round(p_cond, 3),
                         "基准概率": round(p_base, 3),
                         "lift": round(p_cond - p_base, 3),
                         "CI": f"[{lo:.2f},{hi:.2f}]" if n >= MIN_N else "样本不足",
                         "可信": n >= MIN_N})
    return pd.DataFrame(rows)


def main() -> int:
    from pathlib import Path

    from experiment.state_lab.matrix import _index_panel
    from experiment.hmm_state.features import LONG_DEFAULT, build_groups, order_col_for
    from experiment.hmm_state.final import K_OF, fit_dim, grid

    out = Path(__file__).resolve().parents[1] / "out" / "hmm_state"
    out.mkdir(parents=True, exist_ok=True)

    px = _index_panel()
    groups = build_groups(px, LONG_DEFAULT)
    fitted = {n: fit_dim(n, groups[n], order_col_for(n, LONG_DEFAULT), k=K_OF[n])
              for n in groups}

    sig = money_signals(px)
    print("资金信号覆盖：")
    for c in sig.columns:
        print(f"  {c}: {sig[c].notna().sum()} 天，{sig[c].first_valid_index()} → {sig[c].last_valid_index()}")
    print()

    md = ["# 资金行为 × 状态 → 条件转移（第一版）\n",
          f"资金信号先归一化（滚动 z），分档用**滚动** 250 日分位；n < {MIN_N} 不给结论。\n"]

    for label, (pk, vk) in {"短周期": ("短价格", "短波动"),
                            "中长期": ("长价格", "长波动")}.items():
        cell = grid(fitted[pk]["states"], fitted[vk]["states"])
        for col in sig.columns:
            bucket = _rolling_bucket(sig[col])
            tab = conditional_table(cell, bucket)
            if tab.empty:
                continue
            tab.to_csv(out / f"cond_{label}_{col}.csv", index=False, encoding="utf-8-sig")
            # 只挑「lift 大、n 够」的行展示
            top = tab[tab["可信"]].reindex(
                tab[tab["可信"]]["lift"].abs().sort_values(ascending=False).index).head(8)
            md += [f"\n## {label} × {col}\n", "\n", top.to_string(index=False), "\n"]
            print(f"=== {label} × {col}（lift 绝对值最大的 6 条，n≥{MIN_N}）===")
            print(top.head(6).to_string(index=False))
            print()

    (out / "cond_transition.md").write_text("\n".join(md), encoding="utf-8")
    print(f"→ {out / 'cond_transition.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
