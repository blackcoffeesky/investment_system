"""阶段一（完整版）：**四个** HMM + **两套**九宫格。

    python -m experiment.hmm_state.stage1

架构（用户 2026-11-21 修正）：

```
短周期 5–10 日    价格 HMM(K=3) × 波动 HMM(K=3)  → 九宫格 A
中长期 20–60 日   价格 HMM(K=3) × 波动 HMM(K=3)  → 九宫格 B
Extreme           独立事件模块，不参与状态划分
```

**关键是四个模型各有自己的特征窗口**，不是换个名字：

| 模型 | 特征（窗口全部落在本尺度内） |
|---|---|
| 短价格 | `ret_5` `ret_10` `er_10` `snr_10` |
| 短波动 | `rv_5` `atr_5` `gk_10` `abs_ret_5` |
| 长价格 | `ret_20` `ret_60` `er_20` `er_60` `snr_20` |
| 长波动 | `gk_20` `atr_20` `rv_40` `vol_rank_250` |

`vol_rank_250` 是**标准化的历史分位**，窗口长是因为"分位"本来就需要长历史，
它不把未来信息带进来（滚动计算，已由 `lookahead.py` 同类检查覆盖）。

## 本文件覆盖的交付项（对应任务的 1–9 条）

1. 四个 HMM 的样本数 / 占比 / 画像
2. 两套九宫格的格数与占比
3. 九宫格的未来收益 / ES5 / MAE / MDD
4. **ES5 的时间区块 bootstrap 置信区间**
5. 按年份的稳健性
6. 非重叠窗口（每 20 天取 1 点）复核
7. 四个 HMM 的完整转移矩阵 + 计数
8. 两套九宫格的 9×9 转移矩阵 + 计数
9. 每个矩阵的行样本量与持续时间统计

**小样本格子不给结论**：n < 30 的格子标 `样本不足`。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRAIN_FRAC = 0.7
K = 3
MIN_N = 30
FWD = 20


# ---------------------------------------------------------------- 特征


def _er(c: pd.Series, n: int) -> pd.Series:
    change = (c - c.shift(n)).abs()
    path = c.diff().abs().rolling(n, min_periods=n).sum()
    return (change / path.replace(0, np.nan)).clip(0, 1)


def _snr(c: pd.Series, n: int) -> pd.Series:
    r = c.pct_change()
    mu = r.rolling(n, min_periods=max(3, n // 2)).mean()
    se = r.rolling(n, min_periods=max(3, n // 2)).std() / np.sqrt(n)
    return mu / se.replace(0, np.nan)


def _tr(h, l, c) -> pd.Series:
    pc = c.shift(1)
    return pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)


def _gk(c, h, l, o, n) -> pd.Series:
    hl = np.log(h / l) ** 2
    co = np.log(c / o) ** 2
    var = (0.5 * hl - (2 * np.log(2) - 1) * co).rolling(n, min_periods=3).mean()
    return np.sqrt(var.clip(lower=0)) * np.sqrt(252) * 100.0


def _rv(c: pd.Series, n: int) -> pd.Series:
    return c.pct_change().rolling(n, min_periods=max(3, n // 2)).std() * np.sqrt(252) * 100.0


def _vol_rank(c: pd.Series, n_sig: int = 20, n_rank: int = 250) -> pd.Series:
    v = _rv(c, n_sig)
    return v.rolling(n_rank, min_periods=120).apply(
        lambda x: float((x[:-1] < x[-1]).mean()), raw=True)


def build_groups(m: pd.DataFrame) -> dict[str, pd.DataFrame]:
    c, h, l, o = m["close"], m["high"], m["low"], m["open"]
    return {
        "短价格": pd.DataFrame({
            "ret_5": (c / c.shift(5) - 1.0) * 100.0,
            "ret_10": (c / c.shift(10) - 1.0) * 100.0,
            "er_10": _er(c, 10),
            "snr_10": _snr(c, 10),
        }),
        "短波动": pd.DataFrame({
            "rv_5": _rv(c, 5),
            "atr_5": (_tr(h, l, c).rolling(5, min_periods=3).mean() / c * 100.0),
            "gk_10": _gk(c, h, l, o, 10),
            "abs_ret_5": c.pct_change().abs().rolling(5, min_periods=3).mean() * 100.0,
        }),
        "长价格": pd.DataFrame({
            "ret_20": (c / c.shift(20) - 1.0) * 100.0,
            "ret_60": (c / c.shift(60) - 1.0) * 100.0,
            "er_20": _er(c, 20),
            "er_60": _er(c, 60),
            "snr_20": _snr(c, 20),
        }),
        "长波动": pd.DataFrame({
            "gk_20": _gk(c, h, l, o, 20),
            "atr_20": (_tr(h, l, c).rolling(20, min_periods=10).mean() / c * 100.0),
            "rv_40": _rv(c, 40),
            "vol_rank_250": _vol_rank(c, 20, 250),
        }),
    }


#: 状态排序用的列（越大越"危险/越强"）
ORDER_COL = {"短价格": "ret_10", "短波动": "rv_5", "长价格": "ret_60", "长波动": "gk_20"}


# ---------------------------------------------------------------- 拟合


def fit_group(feats: pd.DataFrame, order_col: str) -> dict:
    from experiment.hmm_state.run import fit_hmm, filtered_proba, sort_states_by_risk

    df = feats.dropna()
    cut = int(len(df) * TRAIN_FRAC)
    tr, te = df.iloc[:cut], df.iloc[cut:]
    mu, sd = tr.mean(), tr.std(ddof=0).replace(0, np.nan)
    xtr = ((tr - mu) / sd).dropna()
    xte = ((te - mu) / sd).dropna()

    model = fit_hmm(xtr.to_numpy(), K)
    prob = filtered_proba(model, xte.to_numpy())
    state = sort_states_by_risk(prob.argmax(axis=1),
                                te.loc[xte.index, order_col].to_numpy())
    states = pd.Series(state, index=xte.index, name="state")

    from state_layer import metrics
    sep = metrics.score("bhattacharyya",
                        {"means": model.means_, "covars": model.covars_},
                        threshold=0.4)["min"]

    rid = (states != states.shift()).cumsum()
    runs = pd.DataFrame({"state": states.to_numpy(), "run": rid.to_numpy()}) \
        .groupby("run").agg(state=("state", "first"), length=("state", "size"))
    dur = runs.groupby("state")["length"].agg(["median", "mean", "max", "size"])
    return {"states": states, "profile": te.loc[xte.index].groupby(states).mean().round(2),
            "sep": float(sep), "dur": dur, "runs": runs}


def transition(states: pd.Series) -> tuple[pd.DataFrame, pd.DataFrame]:
    """返回 (行内概率, 计数)。断言行和 = 1。"""
    c = pd.crosstab(states, states.shift(-1))
    c = c.loc[:, c.columns.notna()]
    p = c.div(c.sum(axis=1).replace(0, np.nan), axis=0)
    bad = p.sum(axis=1).dropna()
    assert ((bad - 1).abs() < 1e-9).all(), "转移矩阵行和不为 1"
    return p.round(3), c.astype(int)


# ---------------------------------------------------------------- 风险


def risk_by(states: pd.Series, px: pd.DataFrame) -> pd.DataFrame:
    from experiment.state_lab.risk_profile import es5, forward_metrics

    m = px[["close", "low"]].reindex(states.index)
    r = forward_metrics(m["close"], m["low"]).join(states.rename("s"), how="inner")
    r = r.dropna(subset=["fwd_pct"]).sort_index()

    rows = []
    for name, g in r.groupby("s"):
        f = g["fwd_pct"].to_numpy()
        # 区块 bootstrap 的 ES5 区间（块长 20，保住重叠结构）
        lo_ci, hi_ci = _boot_es5(f)
        rows.append({
            "格": name, "n": len(g),
            "占比%": round(len(g) / len(r) * 100, 1),
            "未来均值%": round(float(f.mean()), 2),
            "ES5": round(es5(f), 2),
            "ES5_CI": f"[{lo_ci:.1f}, {hi_ci:.1f}]" if np.isfinite(lo_ci) else "样本不足",
            "MAE中位%": round(float(g["mae_pct"].median()), 2),
            "MDD中位%": round(float(g["mdd_pct"].median()), 2),
            "MDD_P90%": round(float(np.nanpercentile(g["mdd_pct"], 10)), 2),
        })
    tab = pd.DataFrame(rows).set_index("格")
    tab["可用"] = tab["n"] >= MIN_N
    return tab


def _boot_es5(x: np.ndarray, reps: int = 300, block: int = FWD) -> tuple[float, float]:
    from experiment.state_lab.risk_profile import es5

    x = x[np.isfinite(x)]
    if len(x) < block * 2:
        return (np.nan, np.nan)
    rng = np.random.default_rng(42)
    idx = [np.arange(i, min(i + block, len(x))) for i in range(0, len(x), block)]
    vals = []
    for _ in range(reps):
        pick = rng.integers(0, len(idx), size=len(idx))
        vals.append(es5(x[np.concatenate([idx[i] for i in pick])]))
    return (float(np.percentile(vals, 5)), float(np.percentile(vals, 95)))


def yearly_robust(states: pd.Series, px: pd.DataFrame) -> pd.DataFrame:
    """逐年：ES5 的跨格极差（风险分层在每个年份是否成立）。"""
    from experiment.state_lab.risk_profile import forward_metrics

    m = px[["close", "low"]].reindex(states.index)
    r = forward_metrics(m["close"], m["low"]).join(states.rename("s"), how="inner")
    r = r.dropna(subset=["fwd_pct"])
    rows = {}
    for year, g in r.groupby(r.index.year):
        per = g.groupby("s")["fwd_pct"].apply(
            lambda x: float(np.percentile(x, 5)) if len(x) >= 10 else np.nan).dropna()
        rows[year] = {"格数": int(len(per)),
                      "ES5极差": round(float(per.max() - per.min()), 1) if len(per) > 1 else np.nan}
    return pd.DataFrame(rows).T


def nonoverlap_check(states: pd.Series, px: pd.DataFrame) -> dict:
    from experiment.state_lab.risk_profile import forward_metrics

    m = px[["close", "low"]].reindex(states.index)
    r = forward_metrics(m["close"], m["low"]).join(states.rename("s"), how="inner")
    r = r.dropna(subset=["fwd_pct"]).iloc[::FWD]
    per = r.groupby("s")["fwd_pct"].apply(
        lambda x: float(np.percentile(x, 5)) if len(x) >= 10 else np.nan).dropna()
    return {"样本": len(r), "格数": int(len(per)),
            "ES5极差": round(float(per.max() - per.min()), 1) if len(per) > 1 else np.nan}


# ---------------------------------------------------------------- 主流程


def main() -> int:
    from pathlib import Path

    from experiment.state_lab.matrix import _index_panel

    out = Path(__file__).resolve().parents[1] / "out" / "hmm_state"
    out.mkdir(parents=True, exist_ok=True)

    px = _index_panel()
    groups = build_groups(px)

    md = ["# 阶段一：四个 HMM + 两套九宫格\n",
          f"K={K}；训练/测试 7:3；状态用**过滤概率**定；小样本阈值 n ≥ {MIN_N}。\n"]

    fitted: dict[str, dict] = {}
    md.append("\n## 1. 四个 HMM 的画像与持续\n")
    for name, feats in groups.items():
        res = fit_group(feats, ORDER_COL[name])
        fitted[name] = res
        cnt = res["states"].value_counts().sort_index()
        md += [f"\n### {name}（最小成对距离 {res['sep']:.3f}）\n",
               "\n**样本数 / 占比**\n", cnt.to_frame("n").assign(
                   占比=lambda d: (d["n"] / d["n"].sum() * 100).round(1)).to_string(), "\n",
               "\n**状态画像（原始量纲）**\n", res["profile"].to_string(), "\n",
               "\n**持续时间（按游程）**\n", res["dur"].round(1).to_string(), "\n"]

    md.append("\n## 7. 四个 HMM 的转移矩阵（行内概率 / 计数）\n")
    for name, res in fitted.items():
        p, c = transition(res["states"])
        md += [f"\n### {name}\n", p.to_string(), "\n\n计数：\n", c.to_string(),
               "\n\n行样本量：", c.sum(axis=1).to_dict().__str__(), "\n"]
        p.to_csv(out / f"stage1_{name}_prob.csv", encoding="utf-8-sig")
        c.to_csv(out / f"stage1_{name}_counts.csv", encoding="utf-8-sig")

    # ---- 两套九宫格
    for label, (pkey, vkey) in {"短周期": ("短价格", "短波动"),
                                "中长期": ("长价格", "长波动")}.items():
        sp, sv = fitted[pkey]["states"], fitted[vkey]["states"]
        grid = pd.DataFrame({"p": sp, "v": sv}).dropna()
        grid["格"] = grid["p"].astype(str) + "-" + grid["v"].astype(str)

        rt = risk_by(grid["格"], px)
        rt.to_csv(out / f"stage1_{label}_grid.csv", encoding="utf-8-sig")

        md += [f"\n## 2/3/4. {label}九宫格（{pkey} × {vkey}）\n",
               "\n### 交叉频数\n", pd.crosstab(grid["p"], grid["v"]).to_string(), "\n",
               "\n### 风险画像（ES5 带区块 bootstrap 90% 区间）\n", rt.to_string(), "\n"]

        yr = yearly_robust(grid["格"], px)
        no = nonoverlap_check(grid["格"], px)
        md += ["\n### 5. 逐年稳健性（ES5 跨格极差）\n", yr.to_string(), "\n",
               "\n### 6. 非重叠复核（每 20 天取 1 点）\n", pd.Series(no).to_string(), "\n"]
        yr.to_csv(out / f"stage1_{label}_yearly.csv", encoding="utf-8-sig")

        p9, c9 = transition(grid["格"])
        md += [f"\n## 8. {label}九宫格转移（行内概率）\n", p9.to_string(), "\n",
               "\n计数：\n", c9.to_string(), "\n",
               "\n行样本量：", c9.sum(axis=1).to_dict().__str__(), "\n"]
        p9.to_csv(out / f"stage1_{label}_trans_prob.csv", encoding="utf-8-sig")
        c9.to_csv(out / f"stage1_{label}_trans_counts.csv", encoding="utf-8-sig")

    (out / "stage1.md").write_text("\n".join(md), encoding="utf-8")

    # ---- 控制台摘要
    for name, res in fitted.items():
        print(f"== {name} == 最小成对距离 {res['sep']:.3f}")
        print(res["profile"].to_string())
        print()
    print(f"→ {out / 'stage1.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
