"""最终状态系统雏形：**价格维度用 Student-t HMM，波动维度用 Gaussian HMM**。

    python -m experiment.hmm_state.final

## 为什么按维度选发射分布

阶段三的结论是分维度的：

| 维度 | NLL（t vs 高斯） | 风险分层 | 结论 |
|---|---|---|---|
| 短价格 | **−38.9%** | t 更好（1.94→2.56，非重叠 3.7→6.3） | 用 t |
| 长价格 | **−22.0%** | 持平（4.04→4.30） | 用 t |
| 短波动 | −1.0% | t 塌了（9.83→2.98） | 用高斯 |
| 长波动 | **−10.0%** | t 塌了（6.57→3.27） | 用高斯 |

判据是**应用指标**（风险分层），不是 NLL：NLL 说明"密度拟合更好"，
但状态识别器的用途是把风险分层，所以波动维度保留高斯。

## 输出

- 四个维度的状态（OOS，过滤概率 + 训练集画像排序）
- 两套九宫格的风险画像（ES5 / MAE / MDD + 逐年 + 非重叠）
- 与"全高斯"版本的对照
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRAIN_FRAC = 0.7
K = 3
NU = 5.0

#: 维度 → 发射分布
#: 中长期只保留价格维度：60–120 窗口下的波动率状态既没有可视化意义，
#: 也没有资金分析价值（用户 2026-11-21 决定），所以不建长波动模型。
MODEL = {"短价格": "t", "短波动": "g", "长价格": "t"}

#: 维度 → 状态数。长期用 2：60–120 窗口下第三态只占 1.7%~5.1%，
#: 并且长期状态比短期少本身也合理（窗口越长，可分辨的regime越少）。
K_OF = {"短价格": 3, "短波动": 3, "长价格": 2}


def _prep(feats: pd.DataFrame):
    df = feats.dropna()
    cut = int(len(df) * TRAIN_FRAC)
    tr, te = df.iloc[:cut], df.iloc[cut:]
    mu, sd = tr.mean(), tr.std(ddof=0).replace(0, np.nan)
    return ((tr - mu) / sd).dropna(), ((te - mu) / sd).dropna(), tr, te


def fit_dim(name: str, feats: pd.DataFrame, order_col: str, k: int = K) -> dict:
    """按 `MODEL[name]` 选发射分布。状态顺序只在**训练集画像**上定。"""
    from experiment.hmm_state.run import fit_hmm
    from experiment.hmm_state.stage3_t import (filtered_predict, fit_t_hmm,
                                               gauss_logpdf, t_logpdf)

    xtr_df, xte_df, tr, te = _prep(feats)
    xtr, xte = xtr_df.to_numpy(), xte_df.to_numpy()

    g = fit_hmm(xtr, k)          # 高斯解：既是基线，也是 t 的初始化
    lb_tr_g = np.column_stack([gauss_logpdf(xtr, g.means_[i], g.covars_[i])
                               for i in range(k)])
    post_tr_g, _ = filtered_predict(lb_tr_g, g.transmat_, g.startprob_)
    lb_te_g = np.column_stack([gauss_logpdf(xte, g.means_[i], g.covars_[i])
                               for i in range(k)])
    post_te_g, ll_g = filtered_predict(lb_te_g, g.transmat_, post_tr_g[-1] @ g.transmat_)

    if MODEL[name] == "g":
        post_tr, post_te, ll = post_tr_g, post_te_g, ll_g
        used = "Gaussian"
    else:
        t = fit_t_hmm(xtr, k, init={"means": g.means_, "covars": g.covars_,
                                    "pi": g.startprob_, "a": g.transmat_})
        lb_tr_t = np.column_stack([t_logpdf(xtr, t["means"][i], t["covars"][i], t["nu"])
                                   for i in range(k)])
        post_tr, _ = filtered_predict(lb_tr_t, t["a"], t["pi"])
        lb_te_t = np.column_stack([t_logpdf(xte, t["means"][i], t["covars"][i], t["nu"])
                                   for i in range(k)])
        post_te, ll = filtered_predict(lb_te_t, t["a"], post_tr[-1] @ t["a"])
        used = f"Student-t(ν={t['nu']:.0f})"

    # 训练集画像定顺序
    tr_states = pd.Series(post_tr.argmax(1), index=xtr_df.index)
    prof = tr.loc[xtr_df.index, order_col].groupby(tr_states).mean().sort_values()
    order = {int(s): i for i, s in enumerate(prof.index)}
    states = pd.Series(post_te.argmax(1), index=xte_df.index).map(order)

    return {"name": name, "model": used, "states": states,
            "profile": te.loc[xte_df.index].groupby(states).mean().round(2),
            "share": states.value_counts(normalize=True).sort_index().round(3).to_dict(),
            "nll": float(np.mean(-ll)),
            "entropy": float(np.mean(-(np.clip(post_te, 1e-12, 1) *
                                      np.log(np.clip(post_te, 1e-12, 1))).sum(1))),
            "maxp": float(np.mean(post_te.max(1)))}


def grid(states_a: pd.Series, states_b: pd.Series) -> pd.Series:
    """两维状态交叉成九宫格；标签统一成整数。"""
    g = pd.DataFrame({"a": states_a, "b": states_b}).dropna()
    return (g["a"].astype(int).astype(str) + "-" + g["b"].astype(int).astype(str)).rename("格")


def main() -> int:
    from pathlib import Path

    from experiment.state_lab.matrix import _index_panel
    from experiment.hmm_state.features import LONG_DEFAULT, build_groups, order_col_for
    from experiment.hmm_state.stage1 import nonoverlap_check, risk_by, yearly_robust

    out = Path(__file__).resolve().parents[1] / "out" / "hmm_state"
    out.mkdir(parents=True, exist_ok=True)

    px = _index_panel()
    groups = build_groups(px, LONG_DEFAULT)
    # 只保留 MODEL 里有的维度（中长期不建波动模型）
    groups = {n: f for n, f in groups.items() if n in MODEL}

    fitted = {name: fit_dim(name, feats, order_col_for(name, LONG_DEFAULT),
                            k=K_OF[name])
              for name, feats in groups.items()}

    rows = []
    for name, r in fitted.items():
        rows.append({"维度": name, "K": K_OF[name], "发射分布": r["model"],
                     "OOS NLL": round(r["nll"], 4),
                     "熵": round(r["entropy"], 3), "最大后验": round(r["maxp"], 3),
                     "状态占比": str(r["share"])})
    md = ["# 最终状态系统雏形（价格=Student-t，波动=Gaussian）\n",
          "\n## 四个维度\n", pd.DataFrame(rows).to_string(index=False), "\n"]

    print(pd.DataFrame(rows).to_string(index=False))
    print()

    for label, (pk, vk) in {"短周期": ("短价格", "短波动")}.items():
        cell = grid(fitted[pk]["states"], fitted[vk]["states"])
        rt = risk_by(cell, px)
        yr = yearly_robust(cell, px)
        no = nonoverlap_check(cell, px)
        rt.to_csv(out / f"final_{label}_grid.csv", encoding="utf-8-sig")
        md += [f"\n## {label}九宫格（{pk}={fitted[pk]['model']} × {vk}={fitted[vk]['model']}）\n",
               "\n", rt.to_string(), "\n",
               "\n**逐年 ES5 极差**\n", yr.to_string(), "\n",
               "\n**非重叠复核**\n", pd.Series(no).to_string(), "\n"]
        print(f"=== {label} 九宫格 ===")
        print(rt[["n", "占比%", "未来均值%", "ES5", "MAE中位%", "MDD中位%", "可用"]].to_string())
        print(f"  非重叠 ES5 极差 = {no['ES5极差']}")
        print()

    (out / "final.md").write_text("\n".join(md), encoding="utf-8")
    print(f"→ {out / 'final.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
