"""第 4 项（完整版）：转移结构四件套。

分类口径**冻结**（不再调整，也不合并状态）：

```
趋势  drift_snr_120，阈值 ±1      → 上行 / 震荡 / 下行
波动  mean(rank(GK20), rank(ATR14))，250 日滚动分位三分档 → 低 / 中 / 高
联合  9 格
```

四部分：

1. **边际转移** —— 趋势 3×3、波动 3×3 的一步转移；持续概率、转出方向、切换频率、跨年稳定性
2. **联合转移** —— 9×9；`震荡-中波` 是否是低↔高波的过渡站；`上行-高波` / `下行-高波` 的主要出口
3. **持续时间依赖** —— 按已持续天数分档（1–2 / 3–5 / 6–10 / 10+），给条件转移概率 + Wilson 置信区间 + 样本量
4. **趋势与波动的先后** —— 领先/滞后 20 日内的条件概率差，区分同期与领先

三条硬检查：

- **行和 = 1**（每个矩阵都断言）
- **游程按连续片段统计**（先按 run_id 聚合，再筛标签）
- **状态只用手上有的数据**（`drift_snr_120` / 分位都是滚动的，已由 `lookahead.py` 验过）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

#: 用户指定的持续天数分档
DUR_BUCKETS: list[tuple[str, int, int]] = [
    ("1-2天", 1, 2),
    ("3-5天", 3, 5),
    ("6-10天", 6, 10),
    ("10天+", 11, 10**9),
]

MIN_N = 30
LEADLAG = 10


# ---------------------------------------------------------------- 基础


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """比例的 Wilson 区间（小样本比正态近似稳）。"""
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / d
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, mid - half), min(1.0, mid + half))


def durations(labels: pd.Series) -> pd.Series:
    """每一天处于当前状态的第几天（游程内从 1 开始）。"""
    s = labels.dropna()
    run_id = (s != s.shift()).cumsum()
    return s.groupby(run_id).cumcount() + 1


def counts_matrix(labels: pd.Series) -> pd.DataFrame:
    s = labels.dropna()
    nxt = s.shift(-1)
    ok = nxt.notna()
    return pd.crosstab(s[ok], nxt[ok])


def row_prob(counts: pd.DataFrame) -> pd.DataFrame:
    p = counts.div(counts.sum(axis=1).replace(0, np.nan), axis=0)
    # 硬检查：行和必须为 1（有 n 的行）
    bad = p.sum(axis=1).dropna()
    assert ((bad - 1.0).abs() < 1e-9).all(), f"行和不为 1：{bad[((bad - 1).abs() > 1e-9)]}"
    return p.round(3)


def order_of(series: pd.Series) -> list[str]:
    """按语义排序列（上行→震荡→下行 / 低→中→高）。"""
    pref = ["上行", "震荡", "下行", "低波", "中波", "高波"]
    got = [c for c in series.dropna().unique()]
    head = [p for p in pref if p in got]
    rest = sorted(set(got) - set(head))
    return head + rest


# ---------------------------------------------------------------- 1. 边际转移


def marginal(labels: pd.Series) -> dict:
    counts = counts_matrix(labels)
    seq = order_of(labels)
    counts = counts.reindex(index=seq, columns=seq).fillna(0).astype(int)
    prob = row_prob(counts)
    n = counts.sum(axis=1)
    stay = pd.Series({s: prob.loc[s, s] for s in seq})
    ci = {s: wilson(int(counts.loc[s, s]), int(n[s])) for s in seq}
    return {"counts": counts, "prob": prob, "n": n, "stay": stay, "ci": ci}


def per_year_stay(labels: pd.Series) -> pd.DataFrame:
    """逐年"留在这个状态"的概率（看稳定性）。"""
    s = labels.dropna()
    nxt = s.shift(-1)
    ok = nxt.notna()
    rows = {}
    for year, grp in s[ok].groupby(s[ok].index.year):
        sub = pd.DataFrame({"cur": grp, "nxt": nxt[ok].loc[grp.index]})
        rows[year] = sub.groupby("cur").apply(
            lambda d: float((d["nxt"] == d.name).mean()), include_groups=False)
    return pd.DataFrame(rows).round(3)


def switch_freq(labels: pd.Series) -> pd.Series:
    """每年切换次数 & 平均停留。"""
    s = labels.dropna()
    flips = (s != s.shift()).sum() - 1
    years = (s.index[-1] - s.index[0]).days / 365.25
    return pd.Series({"切换次数": int(flips), "年数": round(years, 1),
                      "切换/年": round(flips / years, 1)})


def persistence_controls(vol: pd.Series, close: pd.Series) -> dict:
    """自持续概率的三个对照 —— 判断"标签黏性"有多少是结构、多少是定义。

    1. **边际基准**：随机置换标签后的自持续 = Σp²（完全无记忆时的期望）
    2. **实际**：当前标签的自持续
    3. **消平滑对照**：用**当日** |收益| 的 250 日分位分档（不做 20 日滚动平均）

    第 3 条最容易被忽略：波动档是 `mean(rank(GK20), rank(ATR14))`，
    **20 日滚动窗口本身就带重叠**，会把标签黏住。它不能说明市场真的"停留"。
    """
    v = vol.dropna()
    marg = v.value_counts(normalize=True)
    base = float((marg ** 2).sum())
    obs = float((v.shift(-1) == v).mean())

    r = close.pct_change().abs()
    q = r.rolling(250, min_periods=120).apply(
        lambda x: float((x[:-1] < x[-1]).mean()), raw=True)
    lab = pd.Series(np.nan, index=close.index, dtype=object)
    lab[q < 1 / 3] = "低"
    lab[(q >= 1 / 3) & (q < 2 / 3)] = "中"
    lab[q >= 2 / 3] = "高"
    lr = lab.dropna()
    raw = float((lr.shift(-1) == lr).mean())

    return {"边际占比": marg.round(3), "随机置换基准": round(base, 3),
            "实际自持续": round(obs, 3), "消平滑对照": round(raw, 3)}


# ---------------------------------------------------------------- 3. 持续依赖


def duration_dependence(labels: pd.Series) -> pd.DataFrame:
    """每一档已持续天数下的"留下概率" + Wilson CI + n。"""
    s = labels.dropna()
    dur = durations(s)
    nxt = s.shift(-1)
    ok = nxt.notna()
    rows = []
    for name, lo, hi in DUR_BUCKETS:
        mask = ok & (dur >= lo) & (dur <= hi)
        sub = pd.DataFrame({"cur": s[mask], "nxt": nxt[mask],
                            "dur": dur[mask]})
        for state, g in sub.groupby("cur"):
            k = int((g["nxt"] == state).sum())
            n = int(len(g))
            lo_ci, hi_ci = wilson(k, n)
            rows.append({"状态": state, "档": name, "n": n,
                         "留下概率": round(k / n, 3) if n else np.nan,
                         "CI下": round(lo_ci, 3) if n else np.nan,
                         "CI上": round(hi_ci, 3) if n else np.nan})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 4. 先后关系


def change_indicator(labels: pd.Series) -> pd.Series:
    """1 = 当天该维度发生了变化。"""
    s = labels.dropna()
    return (s != s.shift()).astype(int).rename("chg")


def lead_lag(a: pd.Series, b: pd.Series, max_lag: int = LEADLAG) -> pd.DataFrame:
    """`P(b 变化在 t+k | a 变化在 t)` 相对基线的抬升。

    k>0 表示 b 滞后于 a（a 先动）。基线 = b 的无条件变化频率。
    这不是因果，只是时间先后与条件概率。
    """
    df = pd.DataFrame({"a": a, "b": b}).dropna()
    base = float(df["b"].mean())
    rows = []
    for k in range(-max_lag, max_lag + 1):
        shifted = df["b"].shift(-k)          # k>0：b 往后看 k 天
        m = df["a"] == 1
        n = int(m.sum())
        if n == 0:
            continue
        p = float(shifted[m].mean(skipna=True))
        rows.append({"k": k, "n": n, "P(b变化|a变化)": round(p, 4),
                     "基线": round(base, 4), "抬升": round(p - base, 4)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 报表


def main() -> int:
    from pathlib import Path

    from experiment.state_lab.cross_tab import load_matrix, trend_bucket, vol_bucket

    out = Path(__file__).resolve().parents[1] / "out" / "state_lab"
    out.mkdir(parents=True, exist_ok=True)

    from experiment.state_lab.matrix import _index_panel

    m = load_matrix()
    m = m.join(_index_panel()[["close"]], how="left")
    tr = trend_bucket(m)
    vo = vol_bucket(m)
    cell = (tr.astype(object) + "-" + vo.astype(object)).where(tr.notna() & vo.notna())

    md: list[str] = ["# 状态转移（第 4 项 · 完整版）\n",
                     "分类口径冻结：趋势 `drift_snr_120 ±1`；波动 GK20+ATR14 统一滚动分位三档。\n"]

    # ---------------- 1. 边际
    md.append("\n## 1. 边际转移\n")
    for name, lab in (("趋势", tr), ("波动", vo)):
        res = marginal(lab)
        md += [f"### {name} 一步转移（行内概率）\n", res["prob"].to_string(), "\n",
               f"\n样本数（行）：\n", res["n"].to_string(), "\n",
               f"\n自持续概率与 Wilson 95% CI：\n"]
        for s in res["stay"].index:
            lo, hi = res["ci"][s]
            md.append(f"- `{s}`: {res['stay'][s]:.3f}  [{lo:.3f}, {hi:.3f}]  n={int(res['n'][s])}")
        md += ["\n逐年自持续（看稳定性）：\n", per_year_stay(lab).to_string(), "\n",
               "\n切换频率：\n", switch_freq(lab).to_string(), "\n"]
        res["prob"].to_csv(out / f"transitions_v2_{name}.csv", encoding="utf-8-sig")

    # ---------------- 2. 联合
    res9 = marginal(cell)
    md += ["\n## 2. 联合状态（9 格）一步转移\n", res9["prob"].fillna(0).to_string(), "\n",
           "\n样本数（行）：\n", res9["n"].to_string(), "\n"]
    res9["prob"].fillna(0).to_csv(out / "transitions_v2_9x9.csv", encoding="utf-8-sig")

    s = cell.dropna()
    nxt = s.shift(-1)
    ok = nxt.notna() & (s == "震荡-中波")
    into_mid = s.shift(1)[ok].value_counts()
    out_mid = nxt[ok].value_counts()
    md += ["\n### `震荡-中波` 的进出口\n", "**从哪来：**\n", into_mid.to_string(), "\n",
           "\n**到哪去：**\n", out_mid.to_string(), "\n"]
    for focus in ("上行-高波", "下行-高波"):
        f = ok & (s == focus)
        if f.sum() == 0:
            continue
        md += [f"\n**`{focus}` 的出口**（n={int(f.sum())}）：\n", nxt[f].value_counts().to_string(), "\n"]

    ctrl = persistence_controls(vo, m["close"])
    md += ["\n### 自持续的三个对照（判断黏性有多少来自定义）\n",
           f"- 随机置换基准（=Σp²）: **{ctrl['随机置换基准']}**\n",
           f"- 实际自持续: **{ctrl['实际自持续']}**\n",
           f"- 消平滑对照（当日 |收益| 分档）: **{ctrl['消平滑对照']}**\n",
           "\n> 波动档用了 20 日滚动平均 + 250 日分位，**窗口重叠本身就会让标签黏住**。\n"
           "> 因此这些转移概率**不能直接当作 HMM 的转移先验**——那会把窗口平滑学成状态黏性。\n",]

    # ---------------- 3. 持续依赖
    dd_tr = duration_dependence(tr)
    dd_vo = duration_dependence(vo)
    md += ["\n## 3. 持续时间依赖（1–2 / 3–5 / 6–10 / 10+）\n",
           "### 波动层\n", dd_vo.to_string(index=False), "\n",
           "\n### 趋势层\n", dd_tr.to_string(index=False), "\n"]
    dd_vo.to_csv(out / "transitions_v2_dur_vol.csv", index=False, encoding="utf-8-sig")
    dd_tr.to_csv(out / "transitions_v2_dur_trend.csv", index=False, encoding="utf-8-sig")

    # ---------------- 4. 先后
    chg_tr = change_indicator(tr)
    chg_vo = change_indicator(vo)
    ll1 = lead_lag(chg_tr, chg_vo)     # 趋势先动 → 波动后动？
    ll2 = lead_lag(chg_vo, chg_tr)     # 波动先动 → 趋势后动？
    md += ["\n## 4. 趋势与波动的先后（领先/滞后 ±%d 日）\n" % LEADLAG,
           "`k>0` 表示 b 在 a 之后变化。抬升 = 条件概率 − 无条件概率。**这是时间先后，不是因果。**\n",
           "\n### 趋势变化在前 → 波动怎么变\n", ll1.to_string(index=False), "\n",
           "\n### 波动变化在前 → 趋势怎么变\n", ll2.to_string(index=False), "\n"]
    ll1.to_csv(out / "transitions_v2_leadlag_trend2vol.csv", index=False, encoding="utf-8-sig")
    ll2.to_csv(out / "transitions_v2_leadlag_vol2trend.csv", index=False, encoding="utf-8-sig")

    (out / "transitions_v2.md").write_text("\n".join(md), encoding="utf-8")

    # ---- 控制台摘要
    print("=== 波动层一步转移 ===")
    print(marginal(vo)["prob"].to_string())
    print("\n=== 持续依赖（波动）===")
    print(dd_vo.to_string(index=False))
    print("\n=== 领先/滞后 ===")
    print(ll1[ll1["k"].isin([-3, -1, 0, 1, 3])].to_string(index=False))
    print(ll2[ll2["k"].isin([-3, -1, 0, 1, 3])].to_string(index=False))
    print(f"\n→ {out / 'transitions_v2.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
