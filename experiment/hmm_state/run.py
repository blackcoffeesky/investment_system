"""多时间尺度 HMM：快周期 / 慢周期 / 联合 / +市场宽度 四组特征。

    python -m experiment.hmm_state.run

## 和规则状态的关系

**规则状态（9 格）保持不动**，作为基准；这里只新增独立的 HMM 实验模块，
不改 `state_layer/` 的任何生产代码。

## 特征分组（为什么这么分）

| 组 | 特征 | 想捕捉 |
|---|---|---|
| **Fast** | `abs_ret` / `tr_pct` / `amplitude_pct` / `rv_5` | 当日冲击、短期风险突变 |
| **Slow** | `gk_20` / `atr_14` / `rv_60` / `vol_rank_250` | 中期风险环境 |
| **Breadth** | `adv_ratio` / `limit_net` / `xs_std` | 市场内部一致性（辅助，最后加） |

分成快慢两组的理由不是"特征多"，而是**决策周期不同**：
快周期对应隔夜/日内风险，慢周期对应仓位与止损宽度。硬塞进一个高斯分布，
模型会在两者之间取平均，得到"谁都不像"的状态。

## 三条实现纪律

1. **只用过滤概率** `P(S_t | X_1:t)`，不用平滑概率 `P(S_t | X_1:T)`。
   平滑概率用到了未来数据，当实时识别用会**系统性高估识别能力**。
   这里用单次前向递推算过滤概率（O(n·K²)），不是生产里那个逐日重跑的 O(n²)。
2. **标准化只用训练集**的均值/标准差，测试段用它做变换。
3. **状态编号无语义**，跨模型比较一律按画像排序（这里用 `abs_ret` 的均值排）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import multivariate_normal

#: 训练/测试切分（按时间，不打乱）
TRAIN_FRAC = 0.7
SEED = 42
KS = (2, 3, 4, 5, 6)


# ---------------------------------------------------------------- 特征


def build_groups(m: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """四组特征。全部只用当日及之前的数据。"""
    close, high, low = m["close"], m["high"], m["low"]

    r = close.pct_change()
    pc = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - pc).abs(), (low - pc).abs()],
                   axis=1).max(axis=1)

    fast = pd.DataFrame({
        "abs_ret": r.abs() * 100.0,
        "tr_pct": (tr / close * 100.0),
        "amplitude_pct": ((high - low) / close * 100.0),
        "rv_5": r.rolling(5, min_periods=3).std() * np.sqrt(252) * 100.0,
    })
    slow = pd.DataFrame({
        "gk_20": _gk(close, high, low, m["open"], 20),
        "atr_14": (tr.rolling(14, min_periods=7).mean() / close * 100.0),
        "rv_60": r.rolling(60, min_periods=30).std() * np.sqrt(252) * 100.0,
        "vol_rank_250": r.rolling(20, min_periods=10).std().rolling(
            250, min_periods=120).apply(lambda x: float((x[:-1] < x[-1]).mean()), raw=True),
    })
    return {"fast": fast, "slow": slow}


def _gk(close, high, low, open_, n) -> pd.Series:
    hl = np.log(high / low) ** 2
    co = np.log(close / open_) ** 2
    var = (0.5 * hl - (2 * np.log(2) - 1) * co).rolling(n, min_periods=5).mean()
    return np.sqrt(var.clip(lower=0)) * np.sqrt(252) * 100.0


def breadth_group() -> pd.DataFrame:
    """市场宽度：上涨家数占比 / 涨跌停净额 / 个股截面离散度。"""
    from datahub.port import get_port

    got = get_port().series_map(["breadth"])
    cols = {k.split(":", 1)[1]: v for k, v in got.items() if k.startswith("breadth:")}
    adv, dec = cols["adv"], cols["dec"]
    lu, ld, total = cols["limit_up"], cols["limit_down"], cols["total"]
    out = pd.DataFrame({
        "adv_ratio": (adv / (adv + dec).replace(0, np.nan)).clip(0, 1),
        "limit_net": ((lu - ld) / total.replace(0, np.nan)).clip(-1, 1),
    })
    return out


# ---------------------------------------------------------------- 模型


def fit_hmm(x: np.ndarray, k: int, seed: int = SEED):
    """高斯 HMM（全协方差）。数据已经标准化过。"""
    from hmmlearn.hmm import GaussianHMM

    best, best_ll = None, -np.inf
    for cov in ("full", "diag"):
        for s in (seed, seed + 1, seed + 2):
            try:
                model = GaussianHMM(n_components=k, covariance_type=cov,
                                    n_iter=500, tol=1e-4, random_state=s)
                model.fit(x)
            except Exception:  # noqa: BLE001  退化数据可能让协方差奇异
                continue
            ll = float(model.score(x))
            if ll > best_ll:
                best, best_ll = model, ll
    if best is None:
        raise RuntimeError(f"K={k} 拟合失败（协方差奇异？）")
    return best


def filtered_proba(model, x: np.ndarray) -> np.ndarray:
    """**过滤概率** `P(S_t | X_1:t)` —— 只用过去和现在。

    前向递推（对数域 + 归一化），一次算完；不做后向递推，所以是过滤而非平滑。
    """
    n, _ = x.shape
    k = model.n_components
    log_b = np.empty((n, k))
    for i in range(k):
        cov = model.covars_[i]
        log_b[:, i] = multivariate_normal.logpdf(
            x, mean=model.means_[i], cov=cov, allow_singular=True)

    with np.errstate(divide="ignore"):
        log_a = np.log(model.transmat_ + 1e-300)
        log_pi = np.log(model.startprob_ + 1e-300)

    out = np.empty((n, k))
    cur = log_pi + log_b[0]
    cur -= _lse(cur)
    out[0] = np.exp(cur)
    for t in range(1, n):
        cur = _lse(cur[:, None] + log_a, axis=0) + log_b[t]
        cur -= _lse(cur)
        out[t] = np.exp(cur)
    return out


def _lse(a, axis=None):
    """log-sum-exp。`axis=None` 时返回标量。"""
    a = np.asarray(a, dtype=float)
    m = np.max(a, axis=axis, keepdims=True)
    m = np.where(np.isfinite(m), m, 0.0)
    out = np.log(np.sum(np.exp(a - m), axis=axis, keepdims=True)) + m
    if axis is None:
        return float(np.ravel(out)[0])
    return np.squeeze(out, axis=axis)


def sort_states_by_risk(states: np.ndarray, risk: np.ndarray) -> np.ndarray:
    """按风险特征把状态重编号（0 = 最平静）。**状态编号本身没有语义。**"""
    means = pd.Series(risk).groupby(states).mean().sort_values()
    mapping = {old: new for new, old in enumerate(means.index)}
    return np.vectorize(mapping.get)(states)


# ---------------------------------------------------------------- 单次实验


def run_one(feats: pd.DataFrame, k: int, fwd_win: int = 20) -> dict:
    """切训练/测试、拟合、算过滤概率、输出 OOS 指标。"""
    from experiment.state_lab.risk_profile import forward_metrics

    df = feats.dropna()
    if len(df) < 400:
        return {}
    cut = int(len(df) * TRAIN_FRAC)
    train, test = df.iloc[:cut], df.iloc[cut:]

    mu, sd = train.mean(), train.std(ddof=0).replace(0, np.nan)
    xtr = ((train - mu) / sd).dropna()
    xte = ((test - mu) / sd).dropna()
    if len(xte) < 60 or len(xtr) < 200:
        return {}

    model = fit_hmm(xtr.to_numpy(), k)
    prob = filtered_proba(model, xte.to_numpy())
    raw_state = prob.argmax(axis=1)

    # 用测试段的 |收益| 均值给状态排序，便于跨 K 比较
    risk = test.loc[xte.index].iloc[:, 0].to_numpy()   # 第一列都是"越大越危险"
    state = sort_states_by_risk(raw_state, risk)

    states = pd.Series(state, index=xte.index, name="state")

    # 风险画像（OOS）
    from experiment.state_lab.matrix import _index_panel
    px = _index_panel()
    m = px[["close", "low"]].reindex(states.index)
    risk_m = forward_metrics(m["close"], m["low"]).dropna()
    joined = risk_m.join(states, how="inner")

    per_state = []
    for s, grp in joined.groupby("state"):
        per_state.append({
            "state": int(s), "n": len(grp),
            "fwd均%": round(float(grp["fwd_pct"].mean()), 2),
            "ES5": round(float(np.percentile(grp["fwd_pct"], 5)), 2),
            "MAE中位%": round(float(grp["mae_pct"].median()), 2),
            "MDD中位%": round(float(grp["mdd_pct"].median()), 2),
        })
    prof = pd.DataFrame(per_state).set_index("state")

    dur = states.groupby((states != states.shift()).cumsum()).size()
    return {
        "k": k,
        "bic": float(-2 * model.score(xtr.to_numpy()) +
                     (k * len(df.columns) + k * len(df.columns) * (len(df.columns) + 1) / 2 +
                      k * (k - 1) + (k - 1)) * np.log(len(xtr))),
        "n_oos": len(xte),
        "中位持续": float(dur.median()),
        "切换/年": round(float((states != states.shift()).sum()) / (len(states) / 252), 1),
        "ES5_极差": round(float(prof["ES5"].max() - prof["ES5"].min()), 2) if len(prof) > 1 else 0.0,
        "MAE_极差": round(float(prof["MAE中位%"].max() - prof["MAE中位%"].min()), 2) if len(prof) > 1 else 0.0,
        "最窄状态占比": round(float(prof["n"].min() / prof["n"].sum()), 3),
        "profile": prof,
        "states": states,
    }


def main() -> int:
    from pathlib import Path

    from experiment.state_lab.matrix import _index_panel

    out = Path(__file__).resolve().parents[1] / "out" / "hmm_state"
    out.mkdir(parents=True, exist_ok=True)

    px = _index_panel()
    groups = build_groups(px)
    br = breadth_group()

    sets = {
        "Fast": groups["fast"],
        "Slow": groups["slow"],
        "Fast+Slow": pd.concat([groups["fast"], groups["slow"]], axis=1),
    }
    sets["Fast+Slow+Breadth"] = pd.concat(
        [sets["Fast+Slow"], br.reindex(px.index)], axis=1)

    rows = []
    for name, feats in sets.items():
        for k in KS:
            try:
                res = run_one(feats, k)
            except Exception as e:  # noqa: BLE001
                rows.append({"特征组": name, "K": k, "误差": f"{type(e).__name__}", })
                continue
            if not res:
                continue
            rows.append({"特征组": name, "K": k, "BIC": round(res["bic"], 0),
                         "OOS天": res["n_oos"], "中位持续": res["中位持续"],
                         "切换/年": res["切换/年"],
                         "ES5极差": res["ES5_极差"], "MAE极差": res["MAE_极差"],
                         "最窄状态占比": res["最窄状态占比"]})
            res["profile"].to_csv(out / f"profile_{name.replace('+', '_')}_k{k}.csv",
                                 encoding="utf-8-sig")

    tab = pd.DataFrame(rows)
    tab.to_csv(out / "hmm_grid.csv", index=False, encoding="utf-8-sig")
    print(tab.to_string(index=False))
    print(f"\n→ {out / 'hmm_grid.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
