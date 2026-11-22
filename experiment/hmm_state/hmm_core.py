"""HMM核心：Gaussian HMM、Student-t HMM EM；只输出过滤概率，禁止平滑（规避未来泄露）

"""

from __future__ import annotations

import numpy as np
import pandas as pd

from scipy.linalg import cho_factor, cho_solve

from scipy.special import gammaln

from hmmlearn.hmm import GaussianHMM

from sklearn.cluster import KMeans

from experiment.hmm_state.config import NU, MAX_ITER, RTOL, TRAIN_FRAC





def _chol(cov: np.ndarray):

    fixed = False

    jitter = 0.0

    for _ in range(6):

        try:

            cf = cho_factor(cov + np.eye(cov.shape[0])*jitter, lower=True)

            logdet = 2.0 * np.sum(np.log(np.diag(cf[0])))

            return cf, logdet, fixed

        except np.linalg.LinAlgError:

            jitter = max(jitter*10, 1e-8)

            fixed = True

    raise np.linalg.LinAlgError("协方差非正定修复失败")





def gauss_logpdf(x: np.ndarray, mu: np.ndarray, cov: np.ndarray):

    d = x.shape[1]

    c, logdet, _ = _chol(cov)

    diff = x - mu

    sol = cho_solve(c, diff.T).T

    delta = np.einsum("ij,ij->i", diff, sol)

    return -0.5*(d*np.log(2*np.pi)+logdet+delta)





def t_logpdf(x: np.ndarray, mu: np.ndarray, cov: np.ndarray, nu: float):

    d = x.shape[1]

    c, logdet, _ = _chol(cov)

    diff = x - mu

    sol = cho_solve(c, diff.T).T

    delta = np.einsum("ij,ij->i", diff, sol)

    return gammaln((nu+d)/2) - gammaln(nu/2) -0.5*d*np.log(nu*np.pi) -0.5*logdet -0.5*(nu+d)*np.log1p(delta/nu)





def log_sum_exp(a, axis=None):

    a = np.asarray(a, dtype=float)

    m = np.max(a, axis=axis, keepdims=True)

    m = np.where(np.isfinite(m), m, 0.0)

    out = np.log(np.sum(np.exp(a-m), axis=axis, keepdims=True)) + m

    if axis is None:

        return float(np.ravel(out)[0])

    return np.squeeze(out, axis=axis)





def filtered_predict(log_b: np.ndarray, transmat: np.ndarray, prior0: np.ndarray):

    """

    前向过滤：prior0 是观测第一笔之前状态分布

    返回 (过滤概率post[n,k], 每步一步预测对数似然logpred[n])

    """

    n, k = log_b.shape

    post = np.empty((n, k))

    logpred = np.empty(n)

    prior = prior0 / max(prior0.sum(), 1e-300)

    for t in range(n):

        shift = float(np.max(log_b[t]))

        un = prior * np.exp(log_b[t]-shift)

        s = un.sum()

        logpred[t] = np.log(max(s,1e-300)) + shift

        post[t] = un / s

        prior = post[t] @ transmat

    return post, logpred





def forward_backward(log_b: np.ndarray, a: np.ndarray, pi: np.ndarray):

    """训练集用前向-后向，输出gamma、xi_sum、loglik"""

    n, k = log_b.shape

    la = np.log(np.clip(a, 1e-300, None))

    lpi = np.log(np.clip(pi, 1e-300, None))

    la_f = np.empty((n, k))

    la_f[0] = lpi + log_b[0]

    c = log_sum_exp(la_f[0])

    la_f[0] -= c

    loglik = c

    for t in range(1, n):

        cur = log_sum_exp(la_f[t-1][:, None] + la, axis=0) + log_b[t]

        c = log_sum_exp(cur)

        la_f[t] = cur-c

        loglik += c

    la_b = np.zeros((n, k))

    for t in range(n-2, -1, -1):

        cur = log_sum_exp(la + log_b[t+1] + la_b[t+1][None,:], axis=1)

        la_b[t] = cur-log_sum_exp(cur)

    gamma = la_f + la_b

    gamma -= log_sum_exp(gamma, axis=1)[:, None]

    gamma = np.exp(gamma)

    xi = np.zeros((k, k))

    for t in range(n-1):

        m = la_f[t][:, None] + la + (log_b[t+1]+la_b[t+1])[None,:]

        m -= log_sum_exp(m.reshape(-1))

        xi += np.exp(m)

    return gamma, xi, loglik





def fit_t_hmm(x: np.ndarray, k:int, init:dict, nu=NU, max_iter=MAX_ITER, rtol=RTOL):

    n, d = x.shape

    means = np.copy(init["means"])

    covars = np.copy(init["covars"]) * ((nu-2.0)/nu if nu>2 else 1.0)

    pi = np.copy(init["pi"])

    a = np.copy(init["a"])

    last_ll = -np.inf

    for it in range(max_iter):

        log_b = np.column_stack([t_logpdf(x, means[i], covars[i], nu) for i in range(k)])

        gamma, xi, ll = forward_backward(log_b, a, pi)

        u = np.empty_like(gamma)

        for i in range(k):

            cf,_,_ = _chol(covars[i])

            diff = x-means[i]

            delta = np.einsum("ij,ij->i", diff, cho_solve(cf, diff.T).T)

            u[:,i] = (nu+d)/(nu+delta)

        w = gamma * u

        wsum = w.sum(axis=0)

        means = (w.T @ x) / wsum[:, None]

        gsum = np.clip(gamma.sum(axis=0), 1e-12, None)

        for i in range(k):

            diff = x-means[i]

            covars[i] = ((diff * w[:, i:i+1]).T @ diff)/gsum[i] + np.eye(d)*1e-8

        pi = gamma[0] / gamma[0].sum()

        a = xi / np.clip(xi.sum(axis=1, keepdims=True), 1e-300, None)

        if it>0:

            rel = abs(ll-last_ll)/max(abs(last_ll),1e-9)

            if rel < rtol:

                break

        last_ll = ll

    true_cov = covars * (nu/(nu-2.0) if nu>2 else np.nan)

    return {"pi":pi, "a":a, "means":means, "covars":covars, "nu":nu, "true_cov":true_cov}





def fit_gaussian_hmm(x_tr: np.ndarray, k:int, seed=42):

    best_model, best_ll = None, -np.inf

    for cov_type in ("full", "diag"):

        for s in (seed, seed+1, seed+2):

            m = GaussianHMM(n_components=k, covariance_type=cov_type, n_iter=500, tol=1e-4, random_state=s)

            try:

                m.fit(x_tr)

            except Exception:

                continue

            ll = m.score(x_tr)

            if ll>best_ll:

                best_ll = ll

                best_model = m

    if best_model is None:

        raise RuntimeError(f"Gaussian-HMM K={k}拟合失败")

    return best_model





def fit_single_model(feats_df: pd.DataFrame, k:int, dist:str, order_col:str):

    """

    :param feats_df:原始特征df，会做7:3时间切分；标准化只用训练集

    :param k:状态数

    :param dist: "g" Gaussian; "t" Student-t

    :param order_col:训练集用于状态排序的列名

    :return: dict: states(OOS状态序列), profile, nll, entropy, maxp

    """

    df = feats_df.dropna().copy()

    cut = int(len(df)*TRAIN_FRAC)

    tr, te = df.iloc[:cut], df.iloc[cut:]

    mu_tr, sd_tr = tr.mean(), tr.std(ddof=0).replace(0, np.nan)

    xtr = ((tr-mu_tr)/sd_tr).dropna()

    xte = ((te-mu_tr)/sd_tr).dropna()

    xtr_np, xte_np = xtr.to_numpy(), xte.to_numpy()



    g_model = fit_gaussian_hmm(xtr_np, k)

    # 训练集过滤概率

    lb_tr_g = np.column_stack([gauss_logpdf(xtr_np, g_model.means_[i], g_model.covars_[i]) for i in range(k)])

    post_tr_g, _ = filtered_predict(lb_tr_g, g_model.transmat_, g_model.startprob_)

    prior_oos_g = post_tr_g[-1] @ g_model.transmat_

    lb_te_g = np.column_stack([gauss_logpdf(xte_np, g_model.means_[i], g_model.covars_[i]) for i in range(k)])

    post_te_g, ll_g = filtered_predict(lb_te_g, g_model.transmat_, prior_oos_g)



    if dist == "g":

        post_tr, post_te, ll = post_tr_g, post_te_g, ll_g

    elif dist == "t":

        t_model = fit_t_hmm(xtr_np, k, init={

            "means":g_model.means_,

            "covars":g_model.covars_,

            "pi":g_model.startprob_,

            "a":g_model.transmat_

        })

        lb_tr_t = np.column_stack([t_logpdf(xtr_np, t_model["means"][i], t_model["covars"][i], t_model["nu"]) for i in range(k)])

        post_tr, _ = filtered_predict(lb_tr_t, t_model["a"], t_model["pi"])

        prior_oos_t = post_tr[-1] @ t_model["a"]

        lb_te_t = np.column_stack([t_logpdf(xte_np, t_model["means"][i], t_model["covars"][i], t_model["nu"]) for i in range(k)])

        post_te, ll = filtered_predict(lb_te_t, t_model["a"], prior_oos_t)

    else:

        raise ValueError("dist must be g or t")



    # 状态重排序：只在训练集上画像排序

    tr_states_idx = pd.Series(post_tr.argmax(axis=1), index=xtr.index)

    tr_feat = tr.loc[xtr.index, order_col]

    prof_train = tr_feat.groupby(tr_states_idx).mean().sort_values()

    mapping = {int(old):i for i, old in enumerate(prof_train.index)}

    oos_raw = post_te.argmax(axis=1)

    states_series = pd.Series(np.vectorize(mapping.get)(oos_raw), index=xte.index)



    profile_oos = te.loc[xte.index].groupby(states_series).mean().round(2)

    entropy = float(np.mean(-np.sum(np.clip(post_te,1e-12,1)*np.log(np.clip(post_te,1e-12,1)), axis=1)))

    maxp = float(np.mean(post_te.max(axis=1)))

    nll = float(np.mean(-ll))

    share = states_series.value_counts(normalize=True).sort_index().round(3).to_dict()

    return {

        "states": states_series,

        "profile": profile_oos,

        "nll": nll,

        "entropy": entropy,

        "maxp": maxp,

        "share": share

    }





def grid_join(s1: pd.Series, s2: pd.Series) -> pd.Series:

    """两个状态序列拼接成网格字符串 "a-b"，生成九宫格"""

    df = pd.DataFrame({"s1":s1, "s2":s2}).dropna()

    return (df["s1"].astype(str)+"-"+df["s2"].astype(str)).rename("grid_cell")

