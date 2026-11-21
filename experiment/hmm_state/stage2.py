"""阶段二：HSMM 对照（四个基础 HMM 各自对照）。

    python -m experiment.hmm_state.stage2

## HSMM 的建法（两步法，不做全参数 EM）

```
1. 用训练集拟合普通 HMM，拿它的发射参数（均值/协方差）和转移矩阵 A
2. 用训练集上的解码状态估**每个状态的持续时间经验分布** f_k(d)
3. 把 (状态, 已持续天数) 展成增广状态空间，做前向过滤
```

只在训练集上估持续时间（满足"持续时间参数只用训练集"的约束）。
**没有重估发射参数**——所以两个模型的差别**只来自持续时间建模**，这是干净对照。

## 为什么这是公平比较

| | HMM | HSMM |
|---|---|---|
| 发射分布 | 训练集拟合 | **同一套** |
| 状态数 K | 3 | 3 |
| 转移 | 常数矩阵 | 常数矩阵 × 持续时间风险率 |
| 差别 | — | 只在"什么时候换状态" |

## 评价口径

- **OOS 一步预测对数得分**：`mean(-log p(x_t | x_1..t-1))`，越小越好。
  HMM 和 HSMM 各自用**自己的**转移结构算 `p`，所以这是真对照。
- 状态画像 / 持续时间 / 风险诊断：与阶段一同口径（过滤概率、区块 bootstrap、
  逐年、非重叠复核）。
- **不用"持续更长"当胜出依据**——持续时间只报告，不评分。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRAIN_FRAC = 0.7
K = 3
FWD = 20
MIN_N = 30
D_MAX_CAP = 120


# ---------------------------------------------------------------- 持续时间


def duration_pmf(train_states: pd.Series, d_max: int,
                 fallback_stay: np.ndarray | None = None) -> tuple[np.ndarray, dict]:
    """训练集上每个状态的持续时间分布 `f_k(d)`，**索引 0 对应持续 1 天**。

    ⚠️ 三处修正（2026-11-21 用户指出）：

    1. **索引偏移**：原实现用 `pmf[k, length] += 1`，而 `hz[:, 0]` 对应"已持续 1 天"，
       等于整体错开一格。现在统一：索引 `d` ↔ 持续 `d+1` 天。
    2. **首尾删失都要丢**：训练集第一个游程可能是从更早延续过来的（左删失），
       最后一个还没结束（右删失）。原实现只丢了右边。
    3. **样本太少的状态不用经验分布**：游程数 < 5 时退回几何分布
       （用 HMM 自己的对角概率 `A_ii` 推），否则经验分布就是几个点凑出来的。
    """
    s = train_states.to_numpy()
    runs: list[tuple[int, int]] = []
    cur, length = int(s[0]), 1
    for v in s[1:]:
        if v == cur:
            length += 1
        else:
            runs.append((cur, length))
            cur, length = int(v), 1
    runs.append((cur, length))
    # 丢首（左删失）与尾（右删失）
    trimmed = runs[1:-1] if len(runs) > 2 else []

    k_count = int(fallback_stay.shape[0]) if fallback_stay is not None else (int(s.max()) + 1)
    pmf = np.zeros((k_count, d_max))
    counts = np.zeros(k_count, dtype=int)
    for k, length in trimmed:
        if 0 <= k < k_count:
            pmf[k, min(length, d_max) - 1] += 1
            counts[k] += 1

    info = {"runs": len(runs), "used": len(trimmed), "per_state": counts.tolist()}
    for k in range(k_count):
        if counts[k] < 5:
            # 退回几何：P(D=d) = (1-p) p^(d-1)，p = 自转移概率
            p = float(fallback_stay[k]) if fallback_stay is not None else 0.9
            p = min(max(p, 1e-3), 1 - 1e-3)
            pmf[k] = (1 - p) * p ** np.arange(d_max)
            info.setdefault("fallback", []).append(k)
    pmf = np.clip(pmf, 1e-9, None)
    pmf /= pmf.sum(axis=1, keepdims=True)
    return pmf, info


def hazard(pmf: np.ndarray) -> np.ndarray:
    """离散风险率 `h_k(d) = P(D=d | D>=d) = f(d) / Σ_{u>=d} f(u)`。

    分母必须是**从 d 开始的尾部概率**（含 d 自身），不是 `P(D>d)`。
    最后一档是"≥ d_max"的合并档，其风险率 = f/f = 1（到上限必然离开），
    这一点在报告里注明——它不代表真实风险率，是合并档的端点。
    """
    surv_ge = np.cumsum(pmf[:, ::-1], axis=1)[:, ::-1]     # Σ_{u>=d}
    surv_ge = np.clip(surv_ge, 1e-12, None)
    return np.clip(pmf / surv_ge, 1e-9, 1 - 1e-9)


def embedded_transmat(a: np.ndarray) -> np.ndarray:
    """嵌入转移矩阵：对角线清零后按行归一。

    HSMM 里"是否离开"由风险率决定，所以离开之后**不能再回到原状态**
    （否则等于绕开持续时间模型重新开始计数）。原实现直接用完整 `A`，
    对角线会把状态送回自身，这是错的。
    """
    emb = a.astype(float).copy()
    np.fill_diagonal(emb, 0.0)
    row = emb.sum(axis=1, keepdims=True)
    bad = (row <= 0).ravel()
    if bad.any():
        emb[bad] = 1.0
        np.fill_diagonal(emb, 0.0)
        row = emb.sum(axis=1, keepdims=True)
    return emb / np.where(row <= 0, 1.0, row)


def _log_emission(model, x: np.ndarray, k: int) -> np.ndarray:
    from scipy.stats import multivariate_normal
    return np.column_stack([
        multivariate_normal.logpdf(x, mean=model.means_[i], cov=model.covars_[i],
                                   allow_singular=True) for i in range(k)])


def hmm_forward(log_b: np.ndarray, a: np.ndarray,
                init_state: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """HMM 前向。`init_state` 是**第一个观测之前**的状态分布（可来自训练集末尾）。"""
    n = len(log_b)
    out = np.empty((n, a.shape[0]))
    pred = np.empty(n)
    state = init_state
    for t in range(n):
        prior = state @ a
        un = prior * np.exp(log_b[t])
        s = un.sum()
        pred[t] = np.log(max(s, 1e-300))
        state = un / s
        out[t] = state
    return out, pred


def hsmm_forward(log_b: np.ndarray, a_emb: np.ndarray, hz: np.ndarray,
                 init_joint: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """HSMM 前向。状态空间 (k, d)，`init_joint[k, d]` 是第一个观测之前的联合分布。

    离开用风险率 `h_k(d)`，落到 `a_emb`（无自转移）；留下则 d → d+1。
    """
    n = len(log_b)
    k, dmax = hz.shape
    a = init_joint.astype(float).copy()
    a /= max(a.sum(), 1e-300)
    out = np.empty((n, k))
    pred = np.empty(n)
    for t in range(n):
        shift = float(np.max(log_b[t]))
        b = np.exp(log_b[t] - shift)
        leave = (a * hz).sum(axis=1)                 # (k,) 离开质量
        stay = a * (1.0 - hz)
        new = np.zeros_like(a)
        new[:, 0] = (leave @ a_emb) * b               # 落到其它状态的 d=1
        new[:, 1:] = stay[:, :-1] * b[:, None]        # 留在原状态，d→d+1
        new[:, -1] += stay[:, -1] * b                 # 合并档吸收
        tot = new.sum()
        pred[t] = np.log(max(tot, 1e-300)) + shift
        a = new / max(tot, 1e-300)
        out[t] = a.sum(axis=1)
    return out, pred


# ---------------------------------------------------------------- 单组对照


def run_group(name: str, feats: pd.DataFrame, order_col: str, px: pd.DataFrame) -> dict:
    """训练集拟合 → 训练集末尾的分布作为 OOS 先验 → 各自前向打分。"""
    from experiment.hmm_state.run import fit_hmm, filtered_proba, sort_states_by_risk

    df = feats.dropna()
    cut = int(len(df) * TRAIN_FRAC)
    tr, te = df.iloc[:cut], df.iloc[cut:]
    mu, sd = tr.mean(), tr.std(ddof=0).replace(0, np.nan)
    xtr = ((tr - mu) / sd).dropna()
    xte = ((te - mu) / sd).dropna()

    model = fit_hmm(xtr.to_numpy(), K)
    A, pi = model.transmat_, model.startprob_
    a_emb = embedded_transmat(A)

    # ---- 训练集：HMM 前向，拿末尾状态分布
    lb_tr = _log_emission(model, xtr.to_numpy(), K)
    p_tr, _ = hmm_forward(lb_tr, A, pi)
    init_state = p_tr[-1]                     # 训练集最后一天的过滤分布

    # ---- 持续时间分布：用**Viterbi 路径**（比 argmax 噪声小），首尾删失都丢
    vit = pd.Series(model.predict(xtr.to_numpy()), index=xtr.index)
    pmf, dur_info = duration_pmf(vit, D_MAX_CAP, fallback_stay=np.diag(A))
    hz = hazard(pmf)

    # ---- 训练集：HSMM 前向，拿末尾的 (状态, 持续天数) 联合分布
    joint0 = np.zeros((K, hz.shape[1]))
    joint0[:, 0] = pi
    p_tr_h, _ = hsmm_forward(lb_tr, a_emb, hz, joint0)
    # 重跑一次拿联合（上面只返回边缘），这里显式重建
    init_joint = _train_tail_joint(lb_tr, a_emb, hz, pi)

    # ---- OOS 打分
    lb = _log_emission(model, xte.to_numpy(), K)
    p_hmm, ll_hmm = hmm_forward(lb, A, init_state)
    p_hsmm, ll_hsmm = hsmm_forward(lb, a_emb, hz, init_joint)

    score_hmm = float(np.mean(-ll_hmm))
    score_hsmm = float(np.mean(-ll_hsmm))

    def states_of(p):
        st = pd.Series(p.argmax(axis=1), index=xte.index)
        risk = te.loc[xte.index, order_col].to_numpy()
        return pd.Series(sort_states_by_risk(st.to_numpy(), risk), index=xte.index)

    s_hmm, s_hsmm = states_of(p_hmm), states_of(p_hsmm)

    return {"name": name, "model": model, "pmf": pmf, "dur_info": dur_info,
            "score_hmm": score_hmm, "score_hsmm": score_hsmm,
            "states_hmm": s_hmm, "states_hsmm": s_hsmm,
            "profile_hmm": te.loc[xte.index].groupby(s_hmm).mean().round(2),
            "profile_hsmm": te.loc[xte.index].groupby(s_hsmm).mean().round(2)}


def _train_tail_joint(log_b, a_emb, hz, pi) -> np.ndarray:
    """训练集最后一天的 (状态, 已持续天数) 联合分布。"""
    k, dmax = hz.shape
    a = np.zeros((k, dmax))
    a[:, 0] = pi
    a /= a.sum()
    for t in range(len(log_b)):
        b = np.exp(log_b[t] - np.max(log_b[t]))
        leave = (a * hz).sum(axis=1)
        stay = a * (1.0 - hz)
        new = np.zeros_like(a)
        new[:, 0] = (leave @ a_emb) * b
        new[:, 1:] = stay[:, :-1] * b[:, None]
        new[:, -1] += stay[:, -1] * b
        a = new / max(new.sum(), 1e-300)
    return a


def _worst_year(states: pd.Series, px: pd.DataFrame) -> float:
    from experiment.hmm_state.stage1 import yearly_robust
    yr = yearly_robust(states, px)
    v = yr["ES5极差"].dropna()
    return round(float(v.min()), 1) if len(v) else float("nan")


def durations_of(states: pd.Series) -> dict:
    """OOS 游程统计（右删失的最后一段单独报）。"""
    s = states.to_numpy()
    runs, cur, length = [], s[0], 1
    for v in s[1:]:
        if v == cur:
            length += 1
        else:
            runs.append(length)
            cur, length = int(v), 1
    censored = length
    arr = np.array(runs)
    return {"游程数": len(arr), "中位": float(np.median(arr)),
            "均值": round(float(arr.mean()), 1),
            "p90": float(np.percentile(arr, 90)),
            "右删失长度": censored}


def main() -> int:
    from pathlib import Path

    from experiment.state_lab.matrix import _index_panel
    from experiment.hmm_state.stage1 import (ORDER_COL, build_groups, nonoverlap_check,
                                             risk_by, yearly_robust)

    out = Path(__file__).resolve().parents[1] / "out" / "hmm_state"
    out.mkdir(parents=True, exist_ok=True)

    px = _index_panel()
    groups = build_groups(px)

    md = ["# 阶段二：HSMM 对照（四个基础 HMM）\n",
          "两步法：HMM 的发射参数不动，只把换状态这一步改成由持续时间风险率驱动。\n",
          "判据不是持续时间更长；持续时间只报告，不评分。\n"]

    rows = []
    detail: list[str] = []
    for name, feats in groups.items():
        r = run_group(name, feats, ORDER_COL[name], px)

        d_hmm = durations_of(r["states_hmm"])
        d_hsmm = durations_of(r["states_hsmm"])
        rt_hmm = risk_by(r["states_hmm"], px)
        rt_hsmm = risk_by(r["states_hsmm"], px)

        def spread(rt):
            ok = rt[rt["可用"]]["ES5"]
            return round(float(ok.max() - ok.min()), 2) if len(ok) > 1 else np.nan

        rows.append({
            "维度": name,
            "OOS得分_HMM": round(r["score_hmm"], 4),
            "OOS得分_HSMM": round(r["score_hsmm"], 4),
            "得分更优": "HSMM" if r["score_hsmm"] < r["score_hmm"] else "HMM",
            "中位持续_HMM": d_hmm["中位"], "中位持续_HSMM": d_hsmm["中位"],
            "ES5极差_HMM": spread(rt_hmm), "ES5极差_HSMM": spread(rt_hsmm),
            "画像一致": round(float((r["profile_hmm"].round(1).fillna(-9) ==
                                     r["profile_hsmm"].round(1).fillna(-9)).mean().mean()), 3),
            "逐年最差_HMM": _worst_year(r["states_hmm"], px),
            "逐年最差_HSMM": _worst_year(r["states_hsmm"], px),
            "非重叠_HMM": nonoverlap_check(r["states_hmm"], px)["ES5极差"],
            "非重叠_HSMM": nonoverlap_check(r["states_hsmm"], px)["ES5极差"],
        })

        detail += [f"\n## {name}\n",
                   "\n**HMM 状态画像**\n", r["profile_hmm"].to_string(), "\n",
                   "\n**HSMM 状态画像**\n", r["profile_hsmm"].to_string(), "\n",
                   "\n**持续时间**\n",
                   pd.DataFrame({"HMM": d_hmm, "HSMM": d_hsmm}).to_string(), "\n",
                   "\n**风险（HMM）**\n", rt_hmm.to_string(), "\n",
                   "\n**风险（HSMM）**\n", rt_hsmm.to_string(), "\n"]

    tab = pd.DataFrame(rows)
    tab.to_csv(out / "stage2_compare.csv", index=False, encoding="utf-8-sig")
    (out / "stage2.md").write_text("\n".join(md + ["\n## 对照总表\n", tab.to_string(), "\n"] + detail),
                                   encoding="utf-8")
    print(tab.to_string(index=False))
    print(f"\n→ {out / 'stage2.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
