"""Student-t HMM 对照 Gaussian HMM（修复版，2026-11-21 第二轮）。

    python -m experiment.hmm_state.stage3_t

## 这一版修了六处（都来自代码审查，逐条对应）

| # | 问题 | 修法 |
|---|---|---|
| 1 | t 的尺度矩阵更新分母用了 `Σγu` | 改为 `Σγ`（均值仍用 `Σγu`） |
| 2 | 训练集过滤第一天先做了一次状态转移 | 统一为"先给 S_1 的先验，再观测"；训练用 `pi` |
| 3 | Gaussian / t 的 OOS 第一天口径不同 | 共用 `filtered_predict`，OOS 首日先验 = `α_T|T · A` |
| 4 | Gaussian NLL 首日是 NaN | 统一递推，**所有 OOS 日期都计入** |
| 5 | 状态排序用了 OOS 数据 | 只在**训练集**画像上定顺序，再套到 OOS |
| 6 | 只有绝对似然判收敛 / 直接求逆 | 相对变化 + 参数变化；Cholesky 求逆并记录修复次数 |

另外：持续时间统计把**最后一段**单独记为右删失。

## 递推定义（两个模型只差 `log_b`）

```
训练: prior_1 = pi            → post_t ∝ prior_t ⊙ b_t, prior_{t+1} = post_t · A
OOS : prior_1 = α_T|T · A     → 同上
log p(x_t | x_{1:t-1}) = log Σ_k prior_t(k) b_k(x_t)      （含 OOS 第一天）
```
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.linalg import cho_factor, cho_solve
from scipy.special import gammaln

TRAIN_FRAC = 0.7
K = 3
NU = 5.0
MAX_ITER = 400
RTOL = 1e-7


# ---------------------------------------------------------------- 密度


def _chol(cov: np.ndarray) -> tuple[np.ndarray, float, bool]:
    """返回 (cho_factor, logdet, 是否修过正定性)。"""
    fixed = False
    jitter = 0.0
    for _ in range(6):
        try:
            c = cho_factor(cov + np.eye(cov.shape[0]) * jitter, lower=True)
            logdet = 2.0 * np.sum(np.log(np.diag(c[0])))
            if not np.isfinite(logdet):
                raise np.linalg.LinAlgError
            return c, logdet, fixed
        except np.linalg.LinAlgError:
            jitter = max(jitter * 10, 1e-8)
            fixed = True
    raise np.linalg.LinAlgError("协方差始终非正定")


def t_logpdf(x: np.ndarray, mu: np.ndarray, cov: np.ndarray, nu: float,
             stats: dict | None = None) -> np.ndarray:
    """多元 t 的对数密度（Cholesky 求逆，避免显式逆矩阵）。"""
    d = x.shape[1]
    c, logdet, fixed = _chol(cov)
    if stats is not None and fixed:
        stats["pd_fix"] = stats.get("pd_fix", 0) + 1
    diff = x - mu
    sol = cho_solve(c, diff.T).T
    delta = np.einsum("ij,ij->i", diff, sol)
    return (gammaln((nu + d) / 2) - gammaln(nu / 2)
            - 0.5 * d * np.log(nu * np.pi) - 0.5 * logdet
            - 0.5 * (nu + d) * np.log1p(delta / nu))


def gauss_logpdf(x: np.ndarray, mu: np.ndarray, cov: np.ndarray) -> np.ndarray:
    d = x.shape[1]
    c, logdet, _ = _chol(cov)
    diff = x - mu
    sol = cho_solve(c, diff.T).T
    delta = np.einsum("ij,ij->i", diff, sol)
    return -0.5 * (d * np.log(2 * np.pi) + logdet + delta)


def _lse(a: np.ndarray, axis=None):
    m = np.max(a, axis=axis, keepdims=True)
    m = np.where(np.isfinite(m), m, 0.0)
    out = np.log(np.sum(np.exp(a - m), axis=axis, keepdims=True)) + m
    return float(np.ravel(out)[0]) if axis is None else np.squeeze(out, axis=axis)


# ---------------------------------------------------------------- 递推


def filtered_predict(log_b: np.ndarray, a: np.ndarray,
                     prior0: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """统一的前向过滤。

    `prior0` 是**观测第一个点之前**的状态分布：
      - 训练集：`prior0 = pi`
      - OOS：`prior0 = α_T|T · A`

    返回 (过滤概率, 每一步的一步预测对数似然)。**第一步也计入**。
    """
    n, k = log_b.shape
    post = np.empty((n, k))
    logpred = np.empty(n)
    prior = prior0 / max(prior0.sum(), 1e-300)
    for t in range(n):
        shift = float(np.max(log_b[t]))
        un = prior * np.exp(log_b[t] - shift)
        s = un.sum()
        logpred[t] = np.log(max(s, 1e-300)) + shift
        post[t] = un / s
        prior = post[t] @ a
    return post, logpred


def forward_backward(log_b: np.ndarray, a: np.ndarray, pi: np.ndarray):
    """对数域前向-后向（仅训练用）。返回 (gamma, xi_sum, loglik)。"""
    n, k = log_b.shape
    la = np.log(np.clip(a, 1e-300, None))
    lpi = np.log(np.clip(pi, 1e-300, None))

    la_f = np.empty((n, k))
    la_f[0] = lpi + log_b[0]
    c = float(_lse(la_f[0]))
    la_f[0] -= c
    loglik = c
    for t in range(1, n):
        cur = _lse(la_f[t - 1][:, None] + la, 0) + log_b[t]
        c = float(_lse(cur))
        la_f[t] = cur - c
        loglik += c

    lb_b = np.zeros((n, k))
    for t in range(n - 2, -1, -1):
        cur = _lse(la + log_b[t + 1] + lb_b[t + 1][None, :], 1)
        lb_b[t] = cur - _lse(cur)

    gamma = la_f + lb_b
    gamma -= _lse(gamma, 1)[:, None]
    gamma = np.exp(gamma)

    xi = np.zeros((k, k))
    for t in range(n - 1):
        m = la_f[t][:, None] + la + (log_b[t + 1] + lb_b[t + 1])[None, :]
        m -= _lse(m.reshape(-1))
        xi += np.exp(m)
    return gamma, xi, loglik


# ---------------------------------------------------------------- 拟合


def fit_t_hmm(x: np.ndarray, k: int = K, nu: float = NU,
              init: dict | None = None, seed: int = 42,
              max_iter: int = MAX_ITER) -> dict:
    """Student-t HMM 的 EM（ν 固定）。

    ⚠️ 尺度矩阵更新：**分母是 Σγ，不是 Σγu**。
    `covars` 是 t 分布的**尺度矩阵** Σ，不是协方差；真实协方差 = ν/(ν-2)·Σ。
    """
    from sklearn.cluster import KMeans

    n, d = x.shape
    if init is None:
        km = KMeans(n_clusters=k, n_init=10, random_state=seed).fit(x)
        means = km.cluster_centers_.copy()
        covars = np.array([np.cov(x[km.labels_ == i].T) + np.eye(d) * 1e-3
                           if (km.labels_ == i).sum() > 1 else np.eye(d)
                           for i in range(k)])
        pi = np.full(k, 1.0 / k)
        a = np.full((k, k), 0.1 / (k - 1))
        np.fill_diagonal(a, 0.9)
    else:
        means = np.asarray(init["means"], dtype=float).copy()
        # 高斯协方差 → t 的**尺度**：Cov = ν/(ν-2)Σ ⇒ Σ = Cov·(ν-2)/ν
        scale = (nu - 2.0) / nu if nu > 2 else 1.0
        covars = np.asarray(init["covars"], dtype=float).copy() * scale
        pi = np.asarray(init["pi"], dtype=float).copy()
        a = np.asarray(init["a"], dtype=float).copy()

    stats = {"pd_fix": 0}
    hist: list[float] = []
    last = -np.inf
    for it in range(max_iter):
        log_b = np.column_stack([t_logpdf(x, means[i], covars[i], nu, stats)
                                 for i in range(k)])
        gamma, xi, ll = forward_backward(log_b, a, pi)
        hist.append(ll)

        u = np.empty_like(gamma)
        for i in range(k):
            c, _, _ = _chol(covars[i])
            diff = x - means[i]
            delta = np.einsum("ij,ij->i", diff, cho_solve(c, diff.T).T)
            u[:, i] = (nu + d) / (nu + delta)

        w = gamma * u
        wsum = w.sum(axis=0)
        means = (w.T @ x) / wsum[:, None]
        gsum = np.clip(gamma.sum(axis=0), 1e-12, None)
        for i in range(k):
            diff = x - means[i]
            covars[i] = ((diff * w[:, i:i + 1]).T @ diff) / gsum[i] + np.eye(d) * 1e-8
        pi = gamma[0] / gamma[0].sum()
        a = xi / np.clip(xi.sum(axis=1, keepdims=True), 1e-300, None)

        if it > 0:
            rel = abs(ll - last) / max(abs(last), 1e-9)
            if rel < RTOL:
                break
        last = ll

    return {"pi": pi, "a": a, "means": means, "covars": covars, "nu": nu,
            "iters": it + 1, "loglik": float(hist[-1]), "hist": hist,
            "pd_fix": stats["pd_fix"],
            # 真实协方差（报告用）
            "true_cov": np.array(covars) * (nu / (nu - 2.0) if nu > 2 else np.nan)}


# ---------------------------------------------------------------- 评价


def _entropy_maxp(prob: np.ndarray) -> tuple[float, float]:
    p = np.clip(prob, 1e-12, 1.0)
    return float(np.mean(-(p * np.log(p)).sum(axis=1))), float(np.mean(prob.max(axis=1)))


def _train_order(train_states: pd.Series, feat: pd.Series) -> dict:
    """在**训练集**上按画像排状态（0 = 最小）。OOS 只套这个映射。"""
    prof = feat.groupby(train_states).mean().sort_values()
    return {int(s): i for i, s in enumerate(prof.index)}


def _dur(states: pd.Series) -> dict:
    v = states.to_numpy()
    runs, cur, ln = [], v[0], 1
    for x in v[1:]:
        if x == cur:
            ln += 1
        else:
            runs.append(ln)
            cur, ln = int(x), 1
    tail = ln                      # 右删失
    arr = np.array(runs) if runs else np.array([1])
    return {"游程数": len(arr), "中位": float(np.median(arr)),
            "均值": round(float(arr.mean()), 1), "右删失": int(tail)}


def run_group(name: str, feats: pd.DataFrame, order_col: str, px: pd.DataFrame) -> dict:
    from experiment.hmm_state.run import fit_hmm

    df = feats.dropna()
    cut = int(len(df) * TRAIN_FRAC)
    tr, te = df.iloc[:cut], df.iloc[cut:]
    mu, sd = tr.mean(), tr.std(ddof=0).replace(0, np.nan)
    xtr_df = ((tr - mu) / sd).dropna()
    xte_df = ((te - mu) / sd).dropna()
    xtr, xte = xtr_df.to_numpy(), xte_df.to_numpy()

    # ---- 高斯 HMM
    g = fit_hmm(xtr, K)
    lb_tr_g = np.column_stack([gauss_logpdf(xtr, g.means_[i], g.covars_[i])
                               for i in range(K)])
    post_tr_g, _ = filtered_predict(lb_tr_g, g.transmat_, g.startprob_)
    prior_oos_g = post_tr_g[-1] @ g.transmat_
    lb_te_g = np.column_stack([gauss_logpdf(xte, g.means_[i], g.covars_[i])
                               for i in range(K)])
    post_te_g, ll_g = filtered_predict(lb_te_g, g.transmat_, prior_oos_g)

    # ---- Student-t HMM（从高斯解出发：只改发射分布）
    t = fit_t_hmm(xtr, K, init={"means": g.means_, "covars": g.covars_,
                                "pi": g.startprob_, "a": g.transmat_})
    lb_tr_t = np.column_stack([t_logpdf(xtr, t["means"][i], t["covars"][i], t["nu"])
                               for i in range(K)])
    post_tr_t, _ = filtered_predict(lb_tr_t, t["a"], t["pi"])
    prior_oos_t = post_tr_t[-1] @ t["a"]
    lb_te_t = np.column_stack([t_logpdf(xte, t["means"][i], t["covars"][i], t["nu"])
                               for i in range(K)])
    post_te_t, ll_t = filtered_predict(lb_te_t, t["a"], prior_oos_t)

    # ---- 状态排序：**只用训练集画像**
    tr_states_g = pd.Series(post_tr_g.argmax(1), index=xtr_df.index)
    tr_states_t = pd.Series(post_tr_t.argmax(1), index=xtr_df.index)
    order_g = _train_order(tr_states_g, tr.loc[xtr_df.index, order_col])
    order_t = _train_order(tr_states_t, tr.loc[xtr_df.index, order_col])

    s_g = pd.Series(post_te_g.argmax(1), index=xte_df.index).map(order_g)
    s_t = pd.Series(post_te_t.argmax(1), index=xte_df.index).map(order_t)

    ent_g, mp_g = _entropy_maxp(post_te_g)
    ent_t, mp_t = _entropy_maxp(post_te_t)

    return {
        "name": name, "nll_g": float(np.mean(-ll_g)), "nll_t": float(np.mean(-ll_t)),
        "states_g": s_g, "states_t": s_t,
        "profile_g": te.loc[xte_df.index].groupby(s_g).mean().round(2),
        "profile_t": te.loc[xte_df.index].groupby(s_t).mean().round(2),
        "share_g": s_g.value_counts(normalize=True).sort_index().round(3).to_dict(),
        "share_t": s_t.value_counts(normalize=True).sort_index().round(3).to_dict(),
        "ent_g": ent_g, "ent_t": ent_t, "mp_g": mp_g, "mp_t": mp_t,
        "dur_g": _dur(s_g), "dur_t": _dur(s_t),
        "iters_t": t["iters"], "pd_fix": t["pd_fix"],
        "monotone": bool(all(np.diff(t["hist"]) > -1e-6)),
    }


def main() -> int:
    from pathlib import Path

    from experiment.state_lab.matrix import _index_panel
    from experiment.hmm_state.stage1 import (ORDER_COL, build_groups, nonoverlap_check,
                                             risk_by, yearly_robust)

    out = Path(__file__).resolve().parents[1] / "out" / "hmm_state"
    out.mkdir(parents=True, exist_ok=True)

    px = _index_panel()
    groups = build_groups(px)

    rows, detail = [], []
    for name, feats in groups.items():
        r = run_group(name, feats, ORDER_COL[name], px)
        rt_g, rt_t = risk_by(r["states_g"], px), risk_by(r["states_t"], px)

        def spread(rt):
            ok = rt[rt["可用"]]["ES5"]
            return round(float(ok.max() - ok.min()), 2) if len(ok) > 1 else np.nan

        def worst_year(st):
            v = yearly_robust(st, px)["ES5极差"].dropna()
            return round(float(v.min()), 1) if len(v) else np.nan

        rows.append({
            "维度": name,
            "NLL_HMM": round(r["nll_g"], 4), "NLL_t": round(r["nll_t"], 4),
            "NLL更优": "t" if r["nll_t"] < r["nll_g"] else "HMM",
            "相对改善%": round((r["nll_g"] - r["nll_t"]) / r["nll_g"] * 100, 1),
            "熵_HMM": round(r["ent_g"], 3), "熵_t": round(r["ent_t"], 3),
            "最大后验_HMM": round(r["mp_g"], 3), "最大后验_t": round(r["mp_t"], 3),
            "游程_HMM": r["dur_g"]["游程数"], "游程_t": r["dur_t"]["游程数"],
            "中位持续_HMM": r["dur_g"]["中位"], "中位持续_t": r["dur_t"]["中位"],
            "ES5极差_HMM": spread(rt_g), "ES5极差_t": spread(rt_t),
            "逐年最差_HMM": worst_year(r["states_g"]),
            "逐年最差_t": worst_year(r["states_t"]),
            "非重叠_HMM": nonoverlap_check(r["states_g"], px)["ES5极差"],
            "非重叠_t": nonoverlap_check(r["states_t"], px)["ES5极差"],
            "EM轮次": r["iters_t"], "PD修复": r["pd_fix"], "似然单调": r["monotone"],
        })
        detail += [f"\n## {name}\n",
                   "\n**HMM 画像（状态按训练画像排序）**\n", r["profile_g"].to_string(), "\n",
                   "\n**t-HMM 画像**\n", r["profile_t"].to_string(), "\n",
                   "\n**状态占比**　HMM: ", str(r["share_g"]), "\n",
                   "　t-HMM: ", str(r["share_t"]), "\n",
                   "\n**持续时间**\n",
                   pd.DataFrame({"HMM": r["dur_g"], "t-HMM": r["dur_t"]}).to_string(), "\n",
                   "\n**风险（HMM）**\n", rt_g.to_string(), "\n",
                   "\n**风险（t-HMM）**\n", rt_t.to_string(), "\n"]

    tab = pd.DataFrame(rows)
    tab.to_csv(out / "stage3_t.csv", index=False, encoding="utf-8-sig")
    (out / "stage3_t.md").write_text(
        "\n".join(["# Student-t HMM vs Gaussian HMM（修复版）\n",
                   "同特征、K=3、7:3、OOS 只用过滤概率；OOS 首日先验 = α_T|T·A；"
                   "状态顺序只在训练集画像上定。ν=5 固定。\n",
                   "\n## 对照总表\n", tab.to_string(index=False), "\n"] + detail),
        encoding="utf-8")
    print(tab.to_string(index=False))
    print(f"\n→ {out / 'stage3_t.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
