"""阶段三（新模型）：Student-t HMM 对照 Gaussian HMM。

    python -m experiment.hmm_state.stage3_t

条件与 HMM 完全一致：同样四组特征、K=3、7:3 时间切分、只用过滤概率、
状态按画像排序、OOS 起点继承训练集末尾的过滤分布。

## 为什么用 t 分布

金融收益的尾部比正态厚。高斯 HMM 为了让极端日"讲得通"，容易分裂出
一个专门装极端日的状态——这也是早前 fast 特征上出现"每天换状态"的原因之一。
t 分布自带厚尾参数 ν，极端日由**同一个状态内的厚尾**解释，不必单开状态。

## 实现要点

- 发射分布：多元 t（ν 固定，默认 5），EM 用**尺度混合**表示：
  `u_i = (ν+d)/(ν+δ²)` 作为隐尺度 `1/τ` 的期望，M 步按 `γ·u` 加权
- E 步用**对数域前向-后向**（训练用平滑是 EM 的标准做法；**OOS 评测仍只用过滤**）
- ν 不估（固定），避免在小样本上估一个不稳的尾部参数
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.special import gammaln

TRAIN_FRAC = 0.7
K = 3
NU = 5.0
MAX_ITER = 300
TOL = 1e-4


# ---------------------------------------------------------------- 密度


def t_logpdf(x: np.ndarray, mu: np.ndarray, cov: np.ndarray, nu: float) -> np.ndarray:
    """多元 t 的对数密度（逐行）。"""
    d = x.shape[1]
    sign, logdet = np.linalg.slogdet(cov)
    if sign <= 0:
        cov = cov + np.eye(d) * 1e-6
        sign, logdet = np.linalg.slogdet(cov)
    inv = np.linalg.inv(cov)
    diff = x - mu
    delta = np.einsum("ij,jk,ik->i", diff, inv, diff)
    return (gammaln((nu + d) / 2) - gammaln(nu / 2)
            - 0.5 * d * np.log(nu * np.pi) - 0.5 * logdet
            - 0.5 * (nu + d) * np.log1p(delta / nu))


def _lse(a: np.ndarray, axis: int) -> np.ndarray:
    m = np.max(a, axis=axis, keepdims=True)
    m = np.where(np.isfinite(m), m, 0.0)
    return np.squeeze(np.log(np.sum(np.exp(a - m), axis=axis, keepdims=True)) + m, axis=axis)


def forward_backward(log_b: np.ndarray, a: np.ndarray, pi: np.ndarray):
    """对数域前向-后向。返回 (gamma, xi_sum, loglik)。

    ⚠️ 2026-11-21 修：原来把 `loglik` 算成了 `log Σ_t P(x_t)`（把各步似然加起来再取对数），
    量级完全错，导致 EM 的收敛判据在 3~16 轮就误判收敛，状态因此退化。
    正确做法是**累加每一步前向归一化因子**：`loglik = Σ_t log c_t`。
    """
    n, k = log_b.shape
    la = np.log(np.clip(a, 1e-300, None))
    lpi = np.log(np.clip(pi, 1e-300, None))

    la_f = np.empty((n, k))
    la_f[0] = lpi + log_b[0]
    c = float(_lse(la_f[0], 0))
    la_f[0] -= c
    loglik = c
    for t in range(1, n):
        cur = _lse(la_f[t - 1][:, None] + la, 0) + log_b[t]
        c = float(_lse(cur, 0))
        la_f[t] = cur - c
        loglik += c

    lb_b = np.zeros((n, k))
    for t in range(n - 2, -1, -1):
        cur = _lse(la + log_b[t + 1] + lb_b[t + 1][None, :], 1)
        lb_b[t] = cur - _lse(cur, 0)

    gamma = la_f + lb_b
    gamma -= _lse(gamma, 1)[:, None]
    gamma = np.exp(gamma)

    xi = np.zeros((k, k))
    for t in range(n - 1):
        m = la_f[t][:, None] + la + (log_b[t + 1] + lb_b[t + 1])[None, :]
        m -= _lse(m.reshape(-1), 0)
        xi += np.exp(m)
    return gamma, xi, loglik


# ---------------------------------------------------------------- 拟合


def fit_t_hmm(x: np.ndarray, k: int = K, nu: float = NU,
              seed: int = 42, init: dict | None = None) -> dict:
    """Student-t HMM 的 EM（ν 固定）。

    `init` 给了就用它当起点（通常传高斯 HMM 的解）。
    **这是对照实验的关键**：起点相同，两者的差别才只来自发射分布；
    否则 KMeans 起点不同会把"分布差异"和"局部最优差异"混在一起。
    """
    from sklearn.cluster import KMeans

    n, d = x.shape
    if init is not None:
        means = np.asarray(init["means"], dtype=float).copy()
        covars = np.asarray(init["covars"], dtype=float).copy()
        pi = np.asarray(init["pi"], dtype=float).copy()
        a = np.asarray(init["a"], dtype=float).copy()
    else:
        km = KMeans(n_clusters=k, n_init=10, random_state=seed).fit(x)
        means = km.cluster_centers_.copy()
        covars = np.array([np.cov(x[km.labels_ == i].T) + np.eye(d) * 1e-4
                           if (km.labels_ == i).sum() > 1 else np.eye(d)
                           for i in range(k)])
        pi = np.full(k, 1.0 / k)
        a = np.full((k, k), 0.1 / (k - 1))
        np.fill_diagonal(a, 0.9)

    last = -np.inf
    for it in range(MAX_ITER):
        log_b = np.column_stack([t_logpdf(x, means[i], covars[i], nu) for i in range(k)])
        gamma, xi, ll = forward_backward(log_b, a, pi)

        # 尺度权重 u_i = (ν+d)/(ν+δ²)
        u = np.empty_like(gamma)
        for i in range(k):
            inv = np.linalg.inv(covars[i])
            diff = x - means[i]
            delta = np.einsum("ij,jk,ik->i", diff, inv, diff)
            u[:, i] = (nu + d) / (nu + delta)

        w = gamma * u
        wsum = w.sum(axis=0)
        means = (w.T @ x) / wsum[:, None]
        for i in range(k):
            diff = x - means[i]
            covars[i] = (diff * w[:, i:i + 1]).T @ diff / wsum[i] + np.eye(d) * 1e-6
        pi = gamma[0] / gamma[0].sum()
        a = xi / xi.sum(axis=1, keepdims=True)
        a = np.clip(a, 1e-12, None)
        a = a / a.sum(axis=1, keepdims=True)

        if abs(ll - last) < TOL:
            break
        last = ll

    return {"pi": pi, "a": a, "means": means, "covars": covars,
            "nu": nu, "iters": it + 1, "loglik": ll}


def t_filtered(x: np.ndarray, params: dict, init_state: np.ndarray):
    """OOS 过滤概率（不做后向）。返回 (过滤概率, 逐步对数预测密度)。"""
    k = params["a"].shape[0]
    log_b = np.column_stack([t_logpdf(x, params["means"][i], params["covars"][i],
                                      params["nu"]) for i in range(k)])
    n = len(x)
    out = np.empty((n, k))
    pred = np.empty(n)
    state = init_state
    for t in range(n):
        prior = state @ params["a"]
        un = prior * np.exp(log_b[t] - log_b[t].max())
        s = un.sum()
        pred[t] = np.log(max(s, 1e-300)) + log_b[t].max()
        state = un / s
        out[t] = state
    return out, pred


# ---------------------------------------------------------------- 评价


def entropy_and_max(prob: np.ndarray) -> tuple[float, float]:
    """过滤概率的平均熵（越小越确定）与平均最大后验。"""
    p = np.clip(prob, 1e-12, 1.0)
    ent = float(np.mean(-(p * np.log(p)).sum(axis=1)))
    return ent, float(np.mean(prob.max(axis=1)))


def run_group(name: str, feats: pd.DataFrame, order_col: str, px: pd.DataFrame) -> dict:
    from experiment.hmm_state.run import fit_hmm, filtered_proba, sort_states_by_risk

    df = feats.dropna()
    cut = int(len(df) * TRAIN_FRAC)
    tr, te = df.iloc[:cut], df.iloc[cut:]
    mu, sd = tr.mean(), tr.std(ddof=0).replace(0, np.nan)
    xtr = ((tr - mu) / sd).dropna().to_numpy()
    xte_df = ((te - mu) / sd).dropna()
    xte = xte_df.to_numpy()

    # --- 高斯 HMM（基线，与阶段一同一套代码）
    g = fit_hmm(xtr, K)
    p_tr = filtered_proba(g, xtr)
    tp = t_filtered  # 哨兵：避免误用
    from scipy.stats import multivariate_normal

    def gauss_logb(x):
        return np.column_stack([
            multivariate_normal.logpdf(x, mean=g.means_[i], cov=g.covars_[i],
                                       allow_singular=True) for i in range(K)])

    # 训练集末尾分布 → OOS 先验
    from experiment.hmm_state.stage2 import hmm_forward
    lb_tr = gauss_logb(xtr)
    _, _ = hmm_forward(lb_tr, g.transmat_, g.startprob_)
    p_tr2, _ = hmm_forward(lb_tr, g.transmat_, g.startprob_)
    init_state = p_tr2[-1]

    p_g, ll_g = hmm_forward(gauss_logb(xte), g.transmat_, init_state)

    # --- Student-t HMM：**从同一个高斯解出发**
    t = fit_t_hmm(xtr, K, init={"means": g.means_, "covars": g.covars_,
                                "pi": g.startprob_, "a": g.transmat_})
    p_tr_t, _ = t_filtered(xtr, t, np.full(K, 1.0 / K))
    init_t = p_tr_t[-1]
    p_t, ll_t = t_filtered(xte, t, init_t)

    def states_of(p):
        st = pd.Series(p.argmax(axis=1), index=xte_df.index)
        risk = te.loc[xte_df.index, order_col].to_numpy()
        return pd.Series(sort_states_by_risk(st.to_numpy(), risk), index=xte_df.index)

    s_g, s_t = states_of(p_g), states_of(p_t)

    def dur_stats(s):
        v = s.to_numpy()
        runs, cur, ln = [], v[0], 1
        for x in v[1:]:
            if x == cur:
                ln += 1
            else:
                runs.append(ln); cur, ln = int(x), 1
        arr = np.array(runs)
        return {"游程数": len(arr), "中位": float(np.median(arr)),
                "均值": round(float(arr.mean()), 1)}

    return {
        "name": name,
        "nll_g": float(np.mean(-ll_g)), "nll_t": float(np.mean(-ll_t)),
        "states_g": s_g, "states_t": s_t,
        "profile_g": te.loc[xte_df.index].groupby(s_g).mean().round(2),
        "profile_t": te.loc[xte_df.index].groupby(s_t).mean().round(2),
        "share_g": s_g.value_counts(normalize=True).sort_index().round(3).to_dict(),
        "share_t": s_t.value_counts(normalize=True).sort_index().round(3).to_dict(),
        "ent_g": entropy_and_max(p_g)[0], "maxp_g": entropy_and_max(p_g)[1],
        "ent_t": entropy_and_max(p_t)[0], "maxp_t": entropy_and_max(p_t)[1],
        "dur_g": dur_stats(s_g), "dur_t": dur_stats(s_t),
        "iters_t": t["iters"],
    }


def main() -> int:
    from pathlib import Path

    from experiment.state_lab.matrix import _index_panel
    from experiment.hmm_state.stage1 import ORDER_COL, build_groups, risk_by

    out = Path(__file__).resolve().parents[1] / "out" / "hmm_state"
    out.mkdir(parents=True, exist_ok=True)

    px = _index_panel()
    groups = build_groups(px)

    rows, detail = [], []
    for name, feats in groups.items():
        r = run_group(name, feats, ORDER_COL[name], px)
        rt_g = risk_by(r["states_g"], px)
        rt_t = risk_by(r["states_t"], px)

        def spread(rt):
            ok = rt[rt["可用"]]["ES5"]
            return round(float(ok.max() - ok.min()), 2) if len(ok) > 1 else np.nan

        rows.append({
            "维度": name,
            "NLL_HMM": round(r["nll_g"], 4), "NLL_t": round(r["nll_t"], 4),
            "NLL更优": "t" if r["nll_t"] < r["nll_g"] else "HMM",
            "熵_HMM": round(r["ent_g"], 3), "熵_t": round(r["ent_t"], 3),
            "最大后验_HMM": round(r["maxp_g"], 3), "最大后验_t": round(r["maxp_t"], 3),
            "游程_HMM": r["dur_g"]["游程数"], "游程_t": r["dur_t"]["游程数"],
            "中位持续_HMM": r["dur_g"]["中位"], "中位持续_t": r["dur_t"]["中位"],
            "ES5极差_HMM": spread(rt_g), "ES5极差_t": spread(rt_t),
            "EM迭代": r["iters_t"],
        })
        detail += [f"\n## {name}\n",
                   "\n**HMM 画像**\n", r["profile_g"].to_string(), "\n",
                   "\n**t-HMM 画像**\n", r["profile_t"].to_string(), "\n",
                   "\n**状态占比**　HMM: ", str(r["share_g"]), "\n",
                   "　t-HMM: ", str(r["share_t"]), "\n",
                   "\n**风险（HMM）**\n", rt_g.to_string(), "\n",
                   "\n**风险（t-HMM）**\n", rt_t.to_string(), "\n"]

    tab = pd.DataFrame(rows)
    tab.to_csv(out / "stage3_t.csv", index=False, encoding="utf-8-sig")
    (out / "stage3_t.md").write_text(
        "\n".join(["# Student-t HMM vs Gaussian HMM\n",
                   "同特征、K=3、7:3 切分、OOS 只用过滤概率；ν=5 固定。\n",
                   "\n## 对照总表\n", tab.to_string(index=False), "\n"] + detail),
        encoding="utf-8")
    print(tab.to_string(index=False))
    print(f"\n→ {out / 'stage3_t.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
