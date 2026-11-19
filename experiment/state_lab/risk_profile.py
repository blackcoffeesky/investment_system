"""第 3 项：波动档的风险差异（MAE / MDD / 左尾），不止"未来波动"。

    python -m experiment.state_lab.risk_profile

## 指标定义（**三个不是一回事，别混**）

| 指标 | 定义（站在 t 日入场） | 回答 |
|---|---|---|
| `MAE` | `min(低点[t+1..t+20]) / close[t] − 1` | 持仓过程中**先亏多少** |
| `MDD` | 未来 20 日**收盘路径**的峰谷最大跌幅 | 区间内**路径风险** |
| `P5` / `P10` | 未来 20 日收益的 5% / 10% 分位 | 较差行情下的损失 |
| `ES5` | 最差 5% 收益的均值 | 极端损失的平均严重程度 |
| 胜率 | `P(fwd_ret > 0)` | 上涨概率 |

`MAE` 用**日低点**（含日内），`MDD` 用**收盘路径**——前者是入场者的浮亏，后者是路径回撤。
两个都算，不合并。

## 两层

1. **只看波动档**（低/中/高）—— 波动档本身有没有风险分层力
2. **趋势 × 波动 9 格** —— 波动档的作用是否依赖趋势

## 三条统计约束（这次必须做到）

- **前后半段分开报**：全样本结论可能是某一段撑起来的
- **重叠窗口**：20 日收益在相邻日高度重叠，所以置信区间用**时间区块 bootstrap**
  （块长 = 20，保住重叠结构），不能用独立同分布假设
- **非重叠抽样复核**：每 20 天取一个点，样本量小但独立，用来交叉验证
- 尾部指标（P5/ES5）**同时给样本量和 CI**，样本 < 60 的不下结论
"""

from __future__ import annotations

import numpy as np
import pandas as pd

FWD = 20
BLOCK = 20
REPS = 400
SEED = 42


# ---------------------------------------------------------------- 指标


def forward_metrics(close: pd.Series, low: pd.Series) -> pd.DataFrame:
    """逐日算未来 20 日的收益 / MAE / MDD。全部只用 t 之后 20 天，t 当天不用。"""
    c = close.to_numpy(dtype=float)
    lo = low.reindex(close.index).to_numpy(dtype=float)
    n = len(c)
    mae = np.full(n, np.nan)
    mdd = np.full(n, np.nan)
    fwd = np.full(n, np.nan)

    for t in range(n - FWD):
        c0 = c[t]
        if not np.isfinite(c0) or c0 <= 0:
            continue
        path_c = c[t + 1: t + 1 + FWD]
        path_l = lo[t + 1: t + 1 + FWD]
        if not np.isfinite(path_c).all():
            continue
        fwd[t] = (path_c[-1] / c0 - 1.0) * 100.0
        if np.isfinite(path_l).all():
            mae[t] = (np.nanmin(path_l) / c0 - 1.0) * 100.0
        peak = np.maximum.accumulate(np.concatenate([[c0], path_c]))
        dd = np.concatenate([[c0], path_c]) / peak - 1.0
        mdd[t] = float(np.nanmin(dd)) * 100.0

    return pd.DataFrame({"fwd_pct": fwd, "mae_pct": mae, "mdd_pct": mdd},
                        index=close.index)


def tail_stats(x: np.ndarray) -> dict:
    x = x[np.isfinite(x)]
    if len(x) == 0:
        return {}
    q05, q10 = np.percentile(x, [5, 10])
    worst = x[x <= q05]
    return {"n": len(x),
            "均值": float(np.mean(x)),
            "P5": float(q05), "P10": float(q10),
            "ES5": float(np.mean(worst)) if len(worst) else float("nan"),
            "胜率%": float((x > 0).mean() * 100)}


def blocks(n: int, size: int = BLOCK) -> list[np.ndarray]:
    return [np.arange(i, min(i + size, n)) for i in range(0, n, size)]


def boot_ci(values: np.ndarray, stat, reps: int = REPS, seed: int = SEED) -> tuple[float, float]:
    """时间区块 bootstrap 的 5%~95% 区间。values 必须**按时间排序**。"""
    rng = np.random.default_rng(seed)
    v = values[np.isfinite(values)]
    if len(v) < BLOCK * 2:
        return (float("nan"), float("nan"))
    idx_blocks = blocks(len(v))
    out = []
    for _ in range(reps):
        pick = rng.integers(0, len(idx_blocks), size=len(idx_blocks))
        sample = np.concatenate([idx_blocks[i] for i in pick])
        out.append(stat(v[sample]))
    return (float(np.percentile(out, 5)), float(np.percentile(out, 95)))


def es5(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) == 0:
        return float("nan")
    q = np.percentile(x, 5)
    return float(np.mean(x[x <= q]))


# ---------------------------------------------------------------- 报表


def summarize(df: pd.DataFrame, by: str) -> pd.DataFrame:
    rows = []
    for name, grp in df.groupby(by):
        f = grp["fwd_pct"].to_numpy()
        s = tail_stats(f)
        if not s:
            continue
        lo, hi = boot_ci(f, es5)
        rows.append({
            "组": name, "n": s["n"],
            "均值%": round(s["均值"], 2),
            "P5": round(s["P5"], 2), "P10": round(s["P10"], 2),
            "ES5": round(s["ES5"], 2),
            "ES5_CI": f"[{lo:.1f}, {hi:.1f}]" if np.isfinite(lo) else "样本不足",
            "MAE中位%": round(float(np.nanmedian(grp["mae_pct"])), 2),
            "MAE_P80%": round(float(np.nanpercentile(grp["mae_pct"], 20)), 2),
            "MDD中位%": round(float(np.nanmedian(grp["mdd_pct"])), 2),
            "MDD_P90%": round(float(np.nanpercentile(grp["mdd_pct"], 10)), 2),
            "胜率%": round(s["胜率%"], 1),
        })
    return pd.DataFrame(rows).set_index("组")


def main() -> int:
    from pathlib import Path

    from experiment.state_lab.cross_tab import load_matrix, trend_bucket, vol_bucket
    from experiment.state_lab.matrix import _index_panel

    out = Path(__file__).resolve().parents[1] / "out" / "state_lab"
    out.mkdir(parents=True, exist_ok=True)

    m = load_matrix()
    px = _index_panel()
    m = m.join(px[["close", "low"]], how="left").dropna(subset=["close"])
    risk = forward_metrics(m["close"], m["low"])

    tr = trend_bucket(m)
    vo = vol_bucket(m)
    cell = (tr.astype(object) + "-" + vo.astype(object)).where(tr.notna() & vo.notna())
    df = risk.join(pd.DataFrame({"趋势": tr, "波动": vo, "格子": cell}), how="left")
    df["半段"] = ["前段(<=2018)" if d.year <= 2018 else "后段(>=2019)" for d in df.index]
    df = df.dropna(subset=["fwd_pct", "波动"])

    layer1 = summarize(df, "波动")
    layer1_half = {h: summarize(g, "波动") for h, g in df.groupby("半段")}

    # 非重叠抽样复核（每 20 天取一个点）
    nonoverlap = df.iloc[::FWD]
    layer1_no = summarize(nonoverlap, "波动")

    layer2 = summarize(df[df["趋势"].notna()], "格子")
    layer2_half = {h: summarize(g, "格子") for h, g in df[df["趋势"].notna()].groupby("半段")}

    md = ["# 波动档的风险差异（MAE / MDD / 左尾）\n",
          f"未来 {FWD} 日；置信区间为时间区块 bootstrap（块长 {BLOCK}，{REPS} 次）\n",
          "\n## 第一层：只看波动档\n", layer1.to_string(), "\n",
          "\n### 前后半段\n"]
    for h, t in layer1_half.items():
        md += [f"**{h}**\n", t.to_string(), "\n"]
    md += ["\n### 非重叠抽样（每 20 天取 1 点，独立样本复核）\n", layer1_no.to_string(), "\n",
           "\n## 第二层：趋势 × 波动\n", layer2.to_string(), "\n",
           "\n### 前后半段\n"]
    for h, t in layer2_half.items():
        md += [f"**{h}**\n", t.to_string(), "\n"]

    (out / "risk_profile.md").write_text("\n".join(md), encoding="utf-8")
    layer1.to_csv(out / "risk_profile_l1.csv", encoding="utf-8-sig")
    layer2.to_csv(out / "risk_profile_l2.csv", encoding="utf-8-sig")

    print("=== 第一层：波动档 ===")
    print(layer1.to_string())
    print("\n=== 前后半段（波动档）===")
    for h, t in layer1_half.items():
        print(f"\n[{h}]")
        print(t[["n", "均值%", "ES5", "MAE中位%", "MDD中位%", "MDD_P90%", "胜率%"]].to_string())
    print(f"\n→ {out / 'risk_profile.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
