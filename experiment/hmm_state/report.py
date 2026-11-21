"""把网格结果整理成一份可读报告，并与规则状态在同一段 OOS 上对照。

    python -m experiment.hmm_state.report

## 选型的硬约束（不是"哪个 BIC 最低"）

一个 HMM 配置要"可用"，必须同时满足：

| 约束 | 为什么 |
|---|---|
| 最窄状态 ≥ 5% | 否则那个状态是几个极端日凑出来的，画像不可信 |
| 中位持续 ≥ 5 个交易日 | 1 天就换的状态，实盘来不及反应 |
| ES5 极差 ≥ 1 个百分点 | 状态之间连风险都分不开，等于白分 |

在此之上再看 ES5 极差（越大越有用）。**BIC 只在同一特征组内比较**——
不同特征组的维度不同，BIC 不可跨组比。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parents[1] / "out" / "hmm_state"

MIN_SHARE = 0.05
MIN_DUR = 5
MIN_ES5_SPREAD = 1.0


def rule_baseline(dates: pd.DatetimeIndex) -> dict:
    """规则状态（9 格）在同一段日期上的风险分层。"""
    from experiment.state_lab.cross_tab import load_matrix, trend_bucket, vol_bucket
    from experiment.state_lab.matrix import _index_panel
    from experiment.state_lab.risk_profile import forward_metrics

    m = load_matrix().join(_index_panel()[["close", "low"]], how="left")
    tr, vo = trend_bucket(m), vol_bucket(m)
    cell = (tr.astype(object) + "-" + vo.astype(object)).where(tr.notna() & vo.notna())
    risk = forward_metrics(m["close"], m["low"])
    df = risk.join(cell.rename("cell"), how="left").loc[dates].dropna(subset=["cell", "fwd_pct"])

    g = df.groupby("cell")["fwd_pct"]
    es5 = g.apply(lambda x: float(np.percentile(x, 5)))
    n = g.size()
    ok = n[n >= 30].index
    states = df[df["cell"].isin(ok)]
    dur = states["cell"].groupby((states["cell"] != states["cell"].shift()).cumsum()).size()
    return {
        "格数": len(ok),
        "ES5极差": round(float(es5[ok].max() - es5[ok].min()), 2) if len(ok) > 1 else 0.0,
        "中位持续": float(dur.median()),
        "切换/年": round(float((states["cell"] != states["cell"].shift()).sum()) /
                       (len(states) / 252), 1),
        "最窄占比": round(float(n[ok].min() / n[ok].sum()), 3),
    }


def pick(grid: pd.DataFrame) -> pd.DataFrame:
    """按硬约束筛，再按 ES5 极差排。"""
    g = grid.dropna(subset=["ES5极差"]).copy()
    g["可用"] = ((g["最窄状态占比"] >= MIN_SHARE) & (g["中位持续"] >= MIN_DUR)
                 & (g["ES5极差"] >= MIN_ES5_SPREAD))
    return g.sort_values(["可用", "ES5极差"], ascending=[False, False])


def main() -> int:
    grid = pd.read_csv(OUT / "hmm_grid.csv")
    ranked = pick(grid)
    ranked.to_csv(OUT / "hmm_ranked.csv", index=False, encoding="utf-8-sig")

    # 同一段 OOS 日期（Fast+Slow K=3 那组有存 states 吗？没存 → 用面板末尾 30%）
    from experiment.state_lab.matrix import _index_panel

    px = _index_panel()
    n = len(px)
    oos_dates = px.index[int(n * 0.7):]
    rule = rule_baseline(oos_dates)

    best = ranked[ranked["可用"]].head(1)
    md = ["# 多时间尺度 HMM：四组特征 × K=2..6\n",
          "训练/测试按时间 7:3 切分；评估只用 **过滤概率** `P(S_t|X_1:t)`。\n",
          "\n## 选型约束\n",
          f"- 最窄状态 ≥ {MIN_SHARE:.0%}　- 中位持续 ≥ {MIN_DUR} 天　- ES5 极差 ≥ {MIN_ES5_SPREAD} 个百分点\n",
          "\n## 结果（按可用性 + ES5 极差排序）\n",
          ranked.to_string(index=False), "\n",
          "\n## 与规则状态对照（同一段 OOS 日期）\n",
          pd.DataFrame([{"方案": "规则状态 9 格", **rule}]).to_string(index=False), "\n"]
    if len(best):
        b = best.iloc[0]
        md += [pd.DataFrame([{"方案": f"HMM {b['特征组']} K={int(b['K'])}",
                              "格数": int(b["K"]), "ES5极差": b["ES5极差"],
                              "中位持续": b["中位持续"], "切换/年": b["切换/年"],
                              "最窄占比": b["最窄状态占比"]}]).to_string(index=False), "\n"]
    (OUT / "report.md").write_text("\n".join(md), encoding="utf-8")

    print("=== 排序后的网格（可用性优先）===")
    print(ranked[["特征组", "K", "中位持续", "切换/年", "ES5极差",
                  "最窄状态占比", "可用"]].to_string(index=False))
    print("\n=== 规则状态（同段 OOS）===")
    print(rule)
    print(f"\n→ {OUT / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
