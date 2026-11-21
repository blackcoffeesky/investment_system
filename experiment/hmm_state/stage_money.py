"""L1：资金信号 × 状态 → 条件转移，**三级证据**。

    python -m experiment.hmm_state.stage_money

## 为什么不能只看 95% CI

组合数量很大（9 信号 × 3 资金档 × 9 个去向 × 若干状态 × 4 个步长），
逐条看 95% CI 的话，**即使资金一点信息都没有，也会偶然冒出几条"显著"**。
所以按三级证据报告（用户 2026-11-21 定的标准）：

| 等级 | 判据 | 含义 |
|---|---|---|
| **探索性** | 前后半段 lift 同号 | 值得继续看 |
| **候选信号** | 同号 + 合并样本 **block bootstrap** CI 不含 0 + 至少一半分段 CI 不含 0 | 有初步增量信息 |
| **稳健信号** | 两个分段 CI **都**不含 0 + 方向一致 + 后续独立时间段复现 | 可进诊断输出 |

"后续独立时间段复现" 的落地：把 OOS 再切成 早/中/晚 三段，要求在**最后一段**同号。

## 范围

只做**短周期九宫格**。长周期只保留价格（K=2），波动率维度的状态对可视化与资金分析都没有意义，
所以中长期不做资金条件转移。

## 统计口径

- 条件概率 `P(S_{t+h}=j | S_t=i, M_t=m)`，基准 `P(S_{t+h}=j | S_t=i)`
- 置信区间：**block bootstrap**（块长 20，保住重叠窗口的序列相关），1000 次
- 小样本：合并样本 n < 60 或任一子段 n < 25 的直接丢弃，不参与评级
"""

from __future__ import annotations

import numpy as np
import pandas as pd

HORIZONS = (1, 5, 10, 20)
BLOCK = 20
REPS = 1000
MIN_N = 60
MIN_N_SEG = 25
SEED = 42


# ---------------------------------------------------------------- 基础


def _prep(state: pd.Series, signal: pd.Series, h: int) -> pd.DataFrame:
    df = pd.DataFrame({"s": state, "n": state.shift(-h),
                       "m": signal.reindex(state.index)}).dropna()
    return df.sort_index()


def _bucket_probs(df: pd.DataFrame, s, m, to) -> float:
    sub = df[(df["s"] == s) & (df["m"] == m)]
    if sub.empty:
        return np.nan
    return float((sub["n"] == to).mean())


def _block_indices(n: int, size: int, rng) -> np.ndarray:
    starts = np.arange(0, n, size)
    pick = rng.integers(0, len(starts), size=len(starts))
    idx = np.concatenate([np.arange(starts[i], min(starts[i] + size, n)) for i in pick])
    return np.sort(idx)


def boot_lift(df: pd.DataFrame, s, m, to, reps: int = REPS, seed: int = SEED):
    """block bootstrap 的 lift 区间：条件概率 − 基准概率，一起重采样。"""
    if df.empty or df[(df["s"] == s) & (df["m"] == m)].empty:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(reps):
        idx = _block_indices(len(df), BLOCK, rng)
        sample = df.iloc[idx]
        cond = _bucket_probs(sample, s, m, to)
        base = float((sample[sample["s"] == s]["n"] == to).mean()) if (sample["s"] == s).any() else np.nan
        if np.isfinite(cond) and np.isfinite(base):
            vals.append(cond - base)
    if not vals:
        return (np.nan, np.nan)
    return (float(np.percentile(vals, 5)), float(np.percentile(vals, 95)))


def _segments(state: pd.Series, k: int = 3) -> list[pd.Series]:
    idx = state.dropna().index
    n = len(idx)
    return [state.loc[idx[i * n // k: (i + 1) * n // k]] for i in range(k)]


def evaluate(state: pd.Series, signal: pd.Series, h: int) -> pd.DataFrame:
    """一个 (状态序列, 信号, 步长) 的全部组合 + 三级评级。"""
    full = _prep(state, signal, h)
    if len(full) < MIN_N:
        return pd.DataFrame()
    segs = [_prep(sg, signal, h) for sg in _segments(state)]
    early, mid, late = segs

    rows = []
    for (s, m), grp in full.groupby(["s", "m"]):
        if len(grp) < MIN_N:
            continue
        for to in sorted(full["n"].unique()):
            cond = float((grp["n"] == to).mean())
            base = float((full[full["s"] == s]["n"] == to).mean())
            lift = cond - base
            if abs(lift) < 0.03:
                continue
            lo, hi = boot_lift(full, s, m, to)
            halves = []
            for seg in (early, late):
                g2 = seg[(seg["s"] == s) & (seg["m"] == m)]
                b2 = seg[seg["s"] == s]
                if len(g2) < MIN_N_SEG or len(b2) < MIN_N_SEG:
                    halves.append((np.nan, np.nan, np.nan))
                    continue
                l2 = float((g2["n"] == to).mean()) - float((b2["n"] == to).mean())
                lo2, hi2 = boot_lift(seg, s, m, to, reps=REPS // 2)
                halves.append((l2, lo2, hi2))

            same_sign = (np.isfinite(halves[0][0]) and np.isfinite(halves[1][0])
                         and np.sign(halves[0][0]) == np.sign(halves[1][0])
                         and np.sign(halves[0][0]) == np.sign(lift))
            ci_main = np.isfinite(lo) and np.isfinite(hi) and (lo > 0 or hi < 0)
            n_seg_sig = sum(1 for (l2, lo2, hi2) in halves
                            if np.isfinite(lo2) and (lo2 > 0 or hi2 < 0))
            seg2_sig = sum(1 for (l2, lo2, hi2) in halves
                           if np.isfinite(lo2) and (lo2 > 0 or hi2 < 0))

            tier = ""
            if same_sign:
                tier = "探索性"
            if same_sign and ci_main and n_seg_sig >= 1:
                tier = "候选"
            g_late = late[(late["s"] == s) & (late["m"] == m)]
            late_same = False
            if len(g_late) >= MIN_N_SEG:
                l3 = float((g_late["n"] == to).mean()) - float(
                    (late[late["s"] == s]["n"] == to).mean())
                late_same = np.isfinite(l3) and np.sign(l3) == np.sign(lift)
            if same_sign and ci_main and seg2_sig == 2 and late_same:
                tier = "稳健"

            rows.append({
                "状态": s, "资金档": m, "去向": to, "n": len(grp),
                "条件": round(cond, 3), "基准": round(base, 3),
                "lift": round(lift, 3),
                "CI合并": f"[{lo:.2f},{hi:.2f}]" if np.isfinite(lo) else "—",
                "lift前": round(halves[0][0], 3) if np.isfinite(halves[0][0]) else np.nan,
                "CI前": (f"[{halves[0][1]:.2f},{halves[0][2]:.2f}]"
                         if np.isfinite(halves[0][1]) else "—"),
                "lift后": round(halves[1][0], 3) if np.isfinite(halves[1][0]) else np.nan,
                "CI后": (f"[{halves[1][1]:.2f},{halves[1][2]:.2f}]"
                         if np.isfinite(halves[1][1]) else "—"),
                "段内CI个数": n_seg_sig,
                "等级": tier,
            })
    return pd.DataFrame(rows)


def main() -> int:
    from pathlib import Path

    from experiment.state_lab.matrix import _index_panel
    from experiment.hmm_state.features import LONG_DEFAULT, build_groups, order_col_for
    from experiment.hmm_state.final import K_OF, fit_dim, grid
    from experiment.hmm_state.money import KIND, SIGNALS, buckets, coverage, load_signals

    out = Path(__file__).resolve().parents[1] / "out" / "hmm_state"
    out.mkdir(parents=True, exist_ok=True)

    px = _index_panel()
    groups = build_groups(px, LONG_DEFAULT)
    # 只建 MODEL 里有的维度（中长期不建波动模型）
    from experiment.hmm_state.final import MODEL
    groups = {n: f for n, f in groups.items() if n in MODEL}
    fitted = {n: fit_dim(n, groups[n], order_col_for(n, LONG_DEFAULT), k=K_OF[n])
              for n in groups}
    cell = grid(fitted["短价格"]["states"], fitted["短波动"]["states"])

    z = load_signals(px)
    bk = buckets(z)
    cov = coverage(z)

    all_rows = []
    for c in z.columns:
        for h in HORIZONS:
            t = evaluate(cell, bk[c], h)
            if t.empty:
                continue
            t["信号"] = c
            t["口径"] = KIND.get(c, "")
            t["h"] = h
            all_rows.append(t)
    tab = pd.concat(all_rows, ignore_index=True)
    tab.to_csv(out / "money_l1.csv", index=False, encoding="utf-8-sig")

    order = {"稳健": 0, "候选": 1, "探索性": 2}
    tab["_rank"] = tab["等级"].map(order).fillna(9)
    tab = tab.sort_values(["_rank", "信号", "h"]).drop(columns="_rank")

    md = ["# 资金信号 × 状态：条件转移（三级证据）\n",
          f"block bootstrap：块长 {BLOCK}，{REPS} 次；n<{MIN_N} 或子段 n<{MIN_N_SEG} 丢弃。\n",
          "\n## 数据覆盖\n", cov.to_string(index=False), "\n",
          "\n## 信号口径\n"]
    for k, v in SIGNALS.items():
        md.append(f"- `{k}`（{KIND.get(k, '')}）：{v}")
    md += ["\n\n## 等级分布\n",
           tab.groupby(["等级"]).size().to_string(), "\n"]

    for tier in ("稳健", "候选", "探索性"):
        sub = tab[tab["等级"] == tier]
        if sub.empty:
            md.append(f"\n### {tier}：0 条\n")
            continue
        md += [f"\n### {tier}（{len(sub)} 条）\n",
               sub.head(25).to_string(index=False), "\n"]

    (out / "money.md").write_text("\n".join(md), encoding="utf-8")

    print("=== 等级分布 ===")
    print(tab.groupby("等级").size().to_string())
    for tier in ("稳健", "候选"):
        sub = tab[tab["等级"] == tier]
        print(f"\n=== {tier}（{len(sub)} 条）===")
        if len(sub):
            print(sub.head(12)[["信号", "h", "状态", "资金档", "去向", "n",
                                "lift", "CI合并", "lift前", "lift后"]].to_string(index=False))
    print(f"\n→ {out / 'money.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
