"""L1 + L3：资金变量与状态转移。

    python -m experiment.hmm_state.stage_money

## L1　条件转移 `P(S_{t+h}=j | S_t=i, M_t=m)`

对 6 个资金信号 × 2 套格子 × h ∈ {1,5,10,20}：

- `基准` = `P(S_{t+h}=j | S_t=i)`（不用资金信息）
- `条件` = 加上资金档之后的概率
- `lift` = 条件 − 基准；同时给 n、Wilson 90% CI
- **前后半段各算一次**：两半的 lift 同号且都 ≥ 0.03 才算"稳定"

判据不是"lift 最大"，而是**稳定**。OOS 只有约 1000 天，分成两半后每格更薄。

## L3　以状态切换为中心的观察（资金是向量，没有天然强弱）

资金不是标量，直接问"资金强会不会换状态"没有定义。所以反过来做：

1. **切换前后的资金形态**：以切换日 t 为原点，看每个资金信号在 t±20 天的平均 z。
   若某信号在 t−k 就明显偏离，它是**领先指标**；只在 t 之后偏离，是**跟随**。
2. **按切换类型分组**：`0-0 → 1-0` 和 `1-1 → 0-1` 的资金形态可能完全不同。
3. 这套结果可以反过来用：**某种资金形态 → 大概率去某个状态**（第 2 条的反向查表）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MIN_N = 30
MIN_N_HALF = 20
LIFT_MIN = 0.03
HORIZONS = (1, 5, 10, 20)

#: 信号在"低/高"档时的可读含义（用于报告，不影响计算）
READ = {
    "basis_z": ("贴水加深", "升水扩大"),
    "oi_z": ("持仓收缩（分歧下降）", "持仓放大（分歧上升）"),
    "oi_chg_z": ("持仓减少", "持仓增加"),
    "etf_z": ("ETF 净赎回", "ETF 净申购"),
    "margin_z": ("两融收缩（去杠杆）", "两融扩张（加杠杆）"),
    "turnover_z": ("缩量", "放量"),
}


def _wilson(k: int, n: int, z: float = 1.645) -> tuple[float, float]:
    if n == 0:
        return (np.nan, np.nan)
    p = k / n
    d = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / d
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, mid - half), min(1.0, mid + half))


def cond_lift(state: pd.Series, signal: pd.Series, h: int) -> pd.DataFrame:
    """某一时间步长下的条件转移表。"""
    nxt = state.shift(-h)
    df = pd.DataFrame({"s": state, "n": nxt,
                       "m": signal.reindex(state.index)}).dropna()
    if df.empty:
        return pd.DataFrame()
    base = pd.crosstab(df["s"], df["n"], normalize="index")
    rows = []
    for (s, m), g in df.groupby(["s", "m"]):
        n = len(g)
        dist = g["n"].value_counts(normalize=True)
        for to in base.columns:
            k = int((g["n"] == to).sum())
            lo, hi = _wilson(k, n)
            rows.append({"状态": s, "资金档": m, "去向": to, "n": n,
                         "条件": round(float(dist.get(to, 0.0)), 3),
                         "基准": round(float(base.loc[s, to]), 3),
                         "lift": round(float(dist.get(to, 0.0)) - float(base.loc[s, to]), 3),
                         "CI下": round(lo, 2), "CI上": round(hi, 2)})
    return pd.DataFrame(rows)


def stable_lifts(state: pd.Series, signal: pd.Series, h: int) -> pd.DataFrame:
    """前后半段各算一次，只留下"同号且都不小"的 lift。"""
    idx = state.dropna().index
    mid = idx[len(idx) // 2]
    parts = {"前段": state.loc[:mid], "后段": state.loc[mid:]}
    tables = {}
    for label, sub in parts.items():
        t = cond_lift(sub, signal, h)
        if not t.empty:
            t["半段"] = label
            tables[label] = t
    if len(tables) < 2:
        return pd.DataFrame()

    a = tables["前段"].set_index(["状态", "资金档", "去向"])
    b = tables["后段"].set_index(["状态", "资金档", "去向"])
    j = a.join(b, lsuffix="_前", rsuffix="_后", how="inner")
    j = j.reset_index()
    j = j[(j["n_前"] >= MIN_N_HALF) & (j["n_后"] >= MIN_N_HALF)]
    if j.empty:
        return j
    j["同号"] = np.sign(j["lift_前"]) == np.sign(j["lift_后"])
    j["最小幅度"] = j[["lift_前", "lift_后"]].abs().min(axis=1)
    j["稳定"] = j["同号"] & (j["最小幅度"] >= LIFT_MIN)
    return j.sort_values("最小幅度", ascending=False)


# ---------------------------------------------------------------- L3


def event_study(state: pd.Series, z: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """切换日前后各资金信号的平均 z。"""
    sw = state.notna() & (state != state.shift(1))
    events = state.index[sw]
    events = events[events != state.index[0]]
    if len(events) < 10:
        return pd.DataFrame()

    rows = []
    for k in range(-window, window + 1):
        shifted = z.shift(-k)          # k>0：看切换之后第 k 天
        vals = shifted.reindex(events)
        rec = {"offset": k, "n": int(vals.notna().any(axis=1).sum())}
        for c in z.columns:
            rec[c] = round(float(vals[c].mean()), 3)
        rows.append(rec)
    return pd.DataFrame(rows)


def transition_shapes(state: pd.Series, z: pd.DataFrame,
                      window: int = 10, min_events: int = 8) -> pd.DataFrame:
    """按"从哪个格 → 到哪个格"分组，看切换前的资金形态（t−10..t−1 的均值）。"""
    prev = state.shift(1)
    sw = state.notna() & prev.notna() & (state != prev)
    idx = state.index[sw]
    idx = idx[idx != state.index[0]]

    rows = []
    for t in idx:
        before = z.loc[:t].iloc[-window - 1:-1]
        if len(before) < window:
            continue
        rec = {"从": prev.loc[t], "到": state.loc[t], "日期": t}
        for c in z.columns:
            rec[c] = float(before[c].mean())
        rows.append(rec)
    if not rows:
        return pd.DataFrame()
    d = pd.DataFrame(rows)
    g = d.groupby(["从", "到"]).agg(**{c: (c, "mean") for c in z.columns},
                                     n=("日期", "size")).reset_index()
    return g[g["n"] >= min_events].round(2)


def main() -> int:
    from pathlib import Path

    from experiment.state_lab.matrix import _index_panel
    from experiment.hmm_state.features import LONG_DEFAULT, build_groups, order_col_for
    from experiment.hmm_state.final import K_OF, fit_dim, grid
    from experiment.hmm_state.money import load_signals, buckets, coverage

    out = Path(__file__).resolve().parents[1] / "out" / "hmm_state"
    out.mkdir(parents=True, exist_ok=True)

    px = _index_panel()
    groups = build_groups(px, LONG_DEFAULT)
    fitted = {n: fit_dim(n, groups[n], order_col_for(n, LONG_DEFAULT), k=K_OF[n])
              for n in groups}
    z = load_signals(px)
    bk = buckets(z)

    md = ["# 资金变量 × 状态（L1 条件转移 + L3 事件研究）\n",
          "\n## 数据覆盖\n", coverage(z).to_string(index=False), "\n"]

    grids = {"短周期": grid(fitted["短价格"]["states"], fitted["短波动"]["states"]),
             "中长期": grid(fitted["长价格"]["states"], fitted["长波动"]["states"])}

    # ---------------- L1
    summ = []
    for gname, cell in grids.items():
        for c in z.columns:
            for h in HORIZONS:
                st = stable_lifts(cell, bk[c], h)
                if st.empty:
                    continue
                n_stable = int(st["稳定"].sum())
                best = st.iloc[0] if len(st) else None
                summ.append({
                    "格子": gname, "信号": c, "h": h,
                    "可比组合": len(st), "稳定组合": n_stable,
                    "最大稳定lift": round(float(best["最小幅度"]), 3) if n_stable else np.nan,
                    "示例": (f"{best['状态']} 且 {best['资金档']} → {best['去向']}"
                             if n_stable else ""),
                })
    tab = pd.DataFrame(summ)
    tab.to_csv(out / "money_l1.csv", index=False, encoding="utf-8-sig")
    md += ["\n## L1　条件转移（前后半段都成立的 lift）\n", tab.to_string(index=False), "\n"]
    print("=== L1 稳定 lift 汇总（只列稳定组合 > 0）===")
    print(tab[tab["稳定组合"] > 0].to_string(index=False))
    print()

    # ---------------- L3
    md.append("\n## L3　切换前后的资金形态（平均 z，±20 日）\n")
    for gname, cell in grids.items():
        ev = event_study(cell, z)
        if ev.empty:
            continue
        ev.to_csv(out / f"money_l3_{gname}.csv", index=False, encoding="utf-8-sig")
        peak = ev.iloc[(ev[list(z.columns)].abs().sum(axis=1)).values.argmax()]
        md += [f"\n### {gname}（切换事件 {int(ev['n'].max())} 次）\n", ev.to_string(index=False), "\n"]
        print(f"=== L3 {gname}：各 offset 的资金 z（绝对值合计最大的那行 offset={int(peak['offset'])}）")
        print(ev[ev["offset"].isin([-10, -5, -1, 0, 1, 5, 10])].to_string(index=False))
        print()

        shapes = transition_shapes(cell, z)
        if not shapes.empty:
            shapes.to_csv(out / f"money_l3_shapes_{gname}.csv", index=False, encoding="utf-8-sig")
            md += [f"\n**{gname} 各切换类型切换前 10 日的资金形态**\n", shapes.to_string(index=False), "\n"]

    (out / "money.md").write_text("\n".join(md), encoding="utf-8")
    print(f"→ {out / 'money.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
