"""两个时间尺度**分开**建模：价格（快，5–10 日）× 波动（慢，20–60 日）。

    python -m experiment.hmm_state.multi_scale

## 为什么分开

上一轮已经验证：把快变量和慢变量塞进同一个 HMM，快的那一维会主导后验，
慢周期好不容易得到的持续性被吃掉（中位持续 29.5 天 → 1 天）。
所以这里**两个尺度各建一个模型**，各自输出状态，再组合成 3×3。

## 特征（窗口按决策周期定，不按"能算什么"定）

| 尺度 | 窗口 | 特征 |
|---|---|---|
| 价格（快） | 5–10 日 | `ret_5` / `ret_10` / `er_10` / `drift_snr_10` |
| 波动（慢） | 20–60 日 | `gk_20` / `atr_20` / `rv_40` / `vol_rank_250` |

## 三步分析（顺序即报告顺序）

1. **各自维度的区分度** —— 每个尺度在 K=2/3/4 下：状态画像、Bhattacharyya 最小成对距离、
   OOS 风险差异、中位持续
2. **3×3 = 9 格** —— 两个 K=3 的模型交叉，看 9 格的风险分层有没有更细
3. **转移矩阵** —— 价格 3×3、波动 3×3、9 格，各一份（都用**过滤状态**算）

## 纪律

- 只用过滤概率 `P(S_t|X_1:t)`；训练/测试按时间 7:3
- 标准化只用训练集
- 状态编号按画像排序（价格按累计收益、波动按波动水平）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRAIN_FRAC = 0.7
KS = (2, 3, 4)
K_MAIN = 3


# ---------------------------------------------------------------- 特征


def build_fast(m: pd.DataFrame) -> pd.DataFrame:
    """价格维度：5–10 日窗口。"""
    c = m["close"]
    out = pd.DataFrame({
        "ret_5": (c / c.shift(5) - 1.0) * 100.0,
        "ret_10": (c / c.shift(10) - 1.0) * 100.0,
        "er_10": _er(c, 10),
        "drift_snr_10": _snr(c, 10),
    })
    return out


def build_slow(m: pd.DataFrame) -> pd.DataFrame:
    """波动维度：20–60 日窗口。"""
    c, h, l = m["close"], m["high"], m["low"]
    r = c.pct_change()
    out = pd.DataFrame({
        "gk_20": _gk(c, h, l, m["open"], 20),
        "atr_20": (_tr(h, l, c).rolling(20, min_periods=10).mean() / c * 100.0),
        "rv_40": r.rolling(40, min_periods=20).std() * np.sqrt(252) * 100.0,
        "vol_rank_250": r.rolling(20, min_periods=10).std().rolling(
            250, min_periods=120).apply(lambda x: float((x[:-1] < x[-1]).mean()), raw=True),
    })
    return out


def _er(c: pd.Series, n: int) -> pd.Series:
    change = (c - c.shift(n)).abs()
    path = c.diff().abs().rolling(n, min_periods=n).sum()
    return (change / path.replace(0, np.nan)).clip(0, 1)


def _snr(c: pd.Series, n: int) -> pd.Series:
    r = c.pct_change()
    mu = r.rolling(n, min_periods=max(5, n // 2)).mean()
    se = r.rolling(n, min_periods=max(5, n // 2)).std() / np.sqrt(n)
    return mu / se.replace(0, np.nan)


def _tr(h, l, c) -> pd.Series:
    pc = c.shift(1)
    return pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)


def _gk(c, h, l, o, n) -> pd.Series:
    hl = np.log(h / l) ** 2
    co = np.log(c / o) ** 2
    var = (0.5 * hl - (2 * np.log(2) - 1) * co).rolling(n, min_periods=5).mean()
    return np.sqrt(var.clip(lower=0)) * np.sqrt(252) * 100.0


# ---------------------------------------------------------------- 拟合


def fit_scale(feats: pd.DataFrame, k: int, order_col: str) -> dict:
    """单尺度：切分、拟合、过滤状态、按画像排序。"""
    from experiment.hmm_state.run import fit_hmm, filtered_proba, sort_states_by_risk

    df = feats.dropna()
    cut = int(len(df) * TRAIN_FRAC)
    tr, te = df.iloc[:cut], df.iloc[cut:]
    mu, sd = tr.mean(), tr.std(ddof=0).replace(0, np.nan)
    xtr = ((tr - mu) / sd).dropna()
    xte = ((te - mu) / sd).dropna()

    model = fit_hmm(xtr.to_numpy(), k)
    prob = filtered_proba(model, xte.to_numpy())
    state = sort_states_by_risk(prob.argmax(axis=1),
                                te.loc[xte.index, order_col].to_numpy())
    states = pd.Series(state, index=xte.index, name="state")

    # 画像用原始量纲（未标准化），人群才看得懂
    prof = te.loc[xte.index].groupby(states).mean().round(2)
    prof["n"] = states.value_counts().sort_index()

    from state_layer import metrics
    sep = metrics.score("bhattacharyya",
                        {"means": model.means_, "covars": model.covars_},
                        threshold=0.4)

    dur = states.groupby((states != states.shift()).cumsum()).size()
    return {"states": states, "prob": prob, "profile": prof,
            "sep": sep["min"], "median_dur": float(dur.median()),
            "switch_per_year": round(float((states != states.shift()).sum()) /
                                     (len(states) / 252), 1)}


def risk_table(states: pd.Series, px: pd.DataFrame) -> pd.DataFrame:
    """每个状态在未来 20 日的收益 / 尾部 / 回撤。"""
    from experiment.state_lab.risk_profile import forward_metrics

    m = px[["close", "low"]].reindex(states.index)
    lab = states.rename("state")
    risk = forward_metrics(m["close"], m["low"]).join(lab, how="inner").dropna(subset=["fwd_pct"])
    rows = []
    for s, g in risk.groupby("state"):
        rows.append({"状态": s, "n": len(g),
                     "未来均值%": round(float(g["fwd_pct"].mean()), 2),
                     "ES5": round(float(np.percentile(g["fwd_pct"], 5)), 2),
                     "MAE中位%": round(float(g["mae_pct"].median()), 2),
                     "MDD中位%": round(float(g["mdd_pct"].median()), 2)})
    return pd.DataFrame(rows).set_index("状态")


def transition(states: pd.Series) -> pd.DataFrame:
    """一步转移（行内概率），断言行和 = 1。"""
    c = pd.crosstab(states, states.shift(-1)).dropna(how="all")
    p = c.div(c.sum(axis=1).replace(0, np.nan), axis=0)
    bad = p.sum(axis=1).dropna()
    assert ((bad - 1).abs() < 1e-9).all(), "转移矩阵行和不为 1"
    return p.round(3)


# ---------------------------------------------------------------- 主流程


def main() -> int:
    from pathlib import Path

    from experiment.state_lab.matrix import _index_panel

    out = Path(__file__).resolve().parents[1] / "out" / "hmm_state"
    out.mkdir(parents=True, exist_ok=True)

    px = _index_panel()
    fast = build_fast(px)
    slow = build_slow(px)

    md = ["# 双尺度分开建模：价格（5–10 日）× 波动（20–60 日）\n",
          "训练/测试按时间 7:3；状态用**过滤概率**定；编号按画像排序。\n"]

    # ---- 步骤 1：各自维度的区分度
    md.append("\n## 1. 各自维度的区分度\n")
    picked: dict[str, dict] = {}
    for name, feats, order in (("价格（快 5–10 日）", fast, "ret_10"),
                               ("波动（慢 20–60 日）", slow, "gk_20")):
        md.append(f"\n### {name}\n")
        lines = []
        for k in KS:
            res = fit_scale(feats, k, order)
            rt = risk_table(res["states"], px)
            spread = float(rt["ES5"].max() - rt["ES5"].min()) if len(rt) > 1 else 0.0
            lines.append({"K": k, "最小成对距离": round(float(res["sep"]), 3),
                          "ES5极差": round(spread, 2),
                          "中位持续": res["median_dur"],
                          "切换/年": res["switch_per_year"],
                          "最窄占比": round(float(rt["n"].min() / rt["n"].sum()), 3)})
            if k == K_MAIN:
                picked[name] = res
        tab = pd.DataFrame(lines)
        md += [tab.to_string(index=False), "\n"]

    # ---- 步骤 2：3×3 九格
    key_f = "价格（快 5–10 日）"
    key_s = "波动（慢 20–60 日）"
    sf = picked[key_f]["states"]
    ss = picked[key_s]["states"]
    grid = pd.DataFrame({"价格": sf, "波动": ss}).dropna()
    grid["格"] = grid["价格"].astype(str) + "-" + grid["波动"].astype(str)

    rt9 = risk_table(grid["格"], px)
    md += ["\n## 2. 3×3 = 9 格（两个 K=3 模型交叉）\n",
           "\n### 9 格风险画像\n", rt9.to_string(), "\n",
           "\n### 交叉频数（看两个维度是否真的不同）\n",
           pd.crosstab(grid["价格"], grid["波动"]).to_string(), "\n"]

    # ---- 步骤 3：三个转移矩阵
    t_fast = transition(sf)
    t_slow = transition(ss)
    t_9 = transition(grid["格"])
    md += ["\n## 3. 转移矩阵（过滤状态，一步）\n",
           "\n### 价格 3×3\n", t_fast.to_string(), "\n",
           "\n### 波动 3×3\n", t_slow.to_string(), "\n",
           "\n### 9 格\n", t_9.to_string(), "\n"]

    (out / "multi_scale.md").write_text("\n".join(md), encoding="utf-8")
    rt9.to_csv(out / "multi_scale_9cells.csv", encoding="utf-8-sig")
    t_9.to_csv(out / "multi_scale_transition_9.csv", encoding="utf-8-sig")
    t_fast.to_csv(out / "multi_scale_transition_fast.csv", encoding="utf-8-sig")
    t_slow.to_csv(out / "multi_scale_transition_slow.csv", encoding="utf-8-sig")

    print("=== 价格维度 ===")
    print(picked[key_f]["profile"].to_string())
    print("\n=== 波动维度 ===")
    print(picked[key_s]["profile"].to_string())
    print("\n=== 9 格风险 ===")
    print(rt9.to_string())
    print("\n=== 9 格转移 ===")
    print(t_9.to_string())
    print(f"\n→ {out / 'multi_scale.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
