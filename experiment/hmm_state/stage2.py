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


def duration_pmf(train_states: pd.Series, d_max: int) -> tuple[np.ndarray, np.ndarray]:
    """训练集上每个状态的持续时间经验分布 f_k(d)，以及对数风险率。

    右删失处理：**最后一个游程是删失的**（还没结束），不计入持续时间分布。
    """
    s = train_states.to_numpy()
    runs: list[tuple[int, int]] = []
    cur, length = s[0], 1
    for v in s[1:]:
        if v == cur:
            length += 1
        else:
            runs.append((int(cur), length))
            cur, length = int(v), 1
    # 最后一段是删失，丢掉
    pmf = np.zeros((K, d_max + 1))
    for k, length in runs:
        if k < K:
            pmf[k, min(length, d_max)] += 1
    # 平滑 + 归一化（避免某状态样本极少时出现零风险率）
    pmf += 1e-3
    pmf /= pmf.sum(axis=1, keepdims=True)
    return pmf, runs


def hazard(pmf: np.ndarray) -> np.ndarray:
    """风险率 h_k(d) = f_k(d) / S_k(d)，S = 存活函数。"""
    surv = 1.0 - np.cumsum(pmf, axis=1)           # S(d) = P(D > d)
    surv = np.clip(surv, 1e-6, None)
    return np.clip(pmf / surv, 1e-6, 1 - 1e-6)


# ---------------------------------------------------------------- 前向

def hmm_forward(log_b: np.ndarray, A: np.ndarray, pi: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """普通 HMM 前向：返回 (过滤状态概率, 每步的一步预测对数似然)。"""
    n = len(log_b)
    alpha = np.exp(log_pi(pi) + log_b[0])
    pred = np.full(n, np.nan)
    out = np.empty((n, A.shape[0]))
    s = alpha.sum()
    out[0] = alpha / s
    for t in range(1, n):
        un = (out[t - 1] @ A) * np.exp(log_b[t])
        s = un.sum()
        pred[t] = np.log(max(s, 1e-300))
        out[t] = un / s
    return out, pred


def hsmm_forward(log_b: np.ndarray, A: np.ndarray, pi: np.ndarray,
                 hz: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """HSMM 前向：状态空间 (k, d)，d = 已持续天数。

    `hz[k, d]` 是风险率 h_k(d)。d 超过上限后并入最后一格（不再延长）。
    """
    n = len(log_b)
    k, dmax = hz.shape
    a = np.zeros((k, dmax))
    b0 = np.exp(log_b[0] - log_b[0].max())
    a[:, 0] = pi * b0
    a /= a.sum()

    pred = np.full(n, np.nan)
    out = np.empty((n, k))
    out[0] = a.sum(axis=1)
    for t in range(1, n):
        shift = float(np.max(log_b[t]))
        b = np.exp(log_b[t] - shift)      # 只为数值稳定；算似然时补回 shift
        # 转移质量：从 (i,d) 按风险率离开 → 落到 (j,1)
        leave = (a * hz[:, :dmax]).sum(axis=1)          # (k,)
        new = np.zeros_like(a)
        # 落到各状态的 d=1
        new[:, 0] = (leave @ A) * b
        # 未离开的：d → d+1
        stay = a * (1.0 - hz)
        # d → d+1（保持同一状态），形状：(k, dmax)
        new[:, 1:] = stay[:, :-1] * b[:, None]
        # d 到上限后并入最后一格（不再继续延长）
        new[:, -1] += stay[:, -1] * b
        # 用未归一化的预测质量算一步预测似然
        pred[t] = np.log(max(new.sum(), 1e-300)) + shift
        a = new / new.sum()
        out[t] = a.sum(axis=1)
    return out, pred


def log_pi(pi: np.ndarray) -> np.ndarray:
    return np.log(np.clip(pi, 1e-12, None))


# ---------------------------------------------------------------- 单组对照


def run_group(name: str, feats: pd.DataFrame, order_col: str, px: pd.DataFrame) -> dict:
    from scipy.stats import multivariate_normal

    from experiment.hmm_state.run import fit_hmm, filtered_proba, sort_states_by_risk

    df = feats.dropna()
    cut = int(len(df) * TRAIN_FRAC)
    tr, te = df.iloc[:cut], df.iloc[cut:]
    mu, sd = tr.mean(), tr.std(ddof=0).replace(0, np.nan)
    xtr = ((tr - mu) / sd).dropna()
    xte = ((te - mu) / sd).dropna()

    model = fit_hmm(xtr.to_numpy(), K)
    A, pi = model.transmat_, model.startprob_

    # 训练集状态（用于估持续时间）
    ptr = filtered_proba(model, xtr.to_numpy())
    tr_states = pd.Series(ptr.argmax(axis=1), index=xtr.index)
    pmf, runs = duration_pmf(tr_states, D_MAX_CAP)
    hz = hazard(pmf)

    # 发射对数似然（OOS）
    x = xte.to_numpy()
    log_b = np.column_stack([
        multivariate_normal.logpdf(x, mean=model.means_[i], cov=model.covars_[i],
                                   allow_singular=True) for i in range(K)])

    # HMM vs HSMM 的一步预测
    p_hmm, ll_hmm = hmm_forward(log_b, A, pi)
    p_hsmm, ll_hsmm = hsmm_forward(log_b, A, pi, hz)

    score_hmm = float(np.nanmean(-ll_hmm[1:]))
    score_hsmm = float(np.nanmean(-ll_hsmm[1:]))

    def states_of(p):
        st = pd.Series(p.argmax(axis=1), index=xte.index)
        risk = te.loc[xte.index, order_col].to_numpy()
        return pd.Series(sort_states_by_risk(st.to_numpy(), risk), index=xte.index)

    s_hmm, s_hsmm = states_of(p_hmm), states_of(p_hsmm)

    return {"name": name, "model": model, "pmf": pmf, "runs": runs,
            "score_hmm": score_hmm, "score_hsmm": score_hsmm,
            "states_hmm": s_hmm, "states_hsmm": s_hsmm,
            "profile_hmm": te.loc[xte.index].groupby(s_hmm).mean().round(2),
            "profile_hsmm": te.loc[xte.index].groupby(s_hsmm).mean().round(2)}


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
