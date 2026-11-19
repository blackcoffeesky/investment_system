"""趋势窗口 / 阈值 的敏感性对照（A/B/C/D 四组）。

    python -m experiment.state_lab.trend_window

| 方案 | 趋势指标 | 阈值 |
|---|---|---|
| A | `drift_snr_120` | ±1.0 |
| B | `drift_snr_60`  | ±1.0 |
| C | `drift_snr_120` | ±0.5 |
| D | `drift_snr_60`  | ±0.5 |

**不看"下行样本有没有变多"就下结论。** 下面五组数字必须一起看：

1. 三类占比（下行变多是不是以碎片化为代价）
2. 游程数与**按游程的中位持续**（不是按天加权）
3. 逐年分布（分类结果在不同年代是否一致）
4. 切换频率（每年切换多少次）
5. 未来 20 日已实现波动中位数（分类有没有风险区分力）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

SCHEMES = {
    "A snr120 ±1.0": ("drift_snr_120", 1.0),
    "B snr60 ±1.0": ("drift_snr_60", 1.0),
    "C snr120 ±0.5": ("drift_snr_120", 0.5),
    "D snr60 ±0.5": ("drift_snr_60", 0.5),
}


def trend_labels(m: pd.DataFrame, col: str, thr: float) -> pd.Series:
    s = m[col]
    out = pd.Series("震荡", index=m.index, dtype=object)
    out[s >= thr] = "上行"
    out[s <= -thr] = "下行"
    out[s.isna()] = np.nan
    return out


def run_stats(labels: pd.Series, target: str) -> dict:
    """某个状态标签的游程统计。

    ⚠️ 必须在**完整标签序列**上先算游程，再挑出目标标签的游程。
    先 `where(labels == target).dropna()` 会把中间夹着的其它状态抹掉，
    让两段同标签的游程**首尾相接**（结果是 runs=1、中位持续=整段长度）。
    2026-11-19 踩过这个坑。
    """
    s = labels.dropna()
    run_id = (s != s.shift()).cumsum()
    sizes = s.groupby(run_id).size()
    name = s.groupby(run_id).first()
    durs = sizes.loc[name[name == target].index]
    if durs.empty:
        return {"days": 0, "runs": 0, "median": float("nan"), "mean": float("nan")}
    return {"days": int(durs.sum()), "runs": int(len(durs)),
            "median": float(durs.median()), "mean": float(durs.mean())}


def main() -> int:
    from pathlib import Path

    from experiment.state_lab import indicators as I
    from experiment.state_lab.cross_tab import load_matrix
    from experiment.state_lab.matrix import _index_panel

    m = load_matrix()
    px = _index_panel()
    m = m.join(px[["close"]], how="left")
    m["drift_snr_60"] = I.drift_snr(px["close"], 60)
    r = m["close"].pct_change()
    fvol = r.rolling(20).std().shift(-20) * np.sqrt(252) * 100.0
    m["fvol"] = fvol

    rows = []
    year_rows: dict[str, pd.Series] = {}
    for name, (col, thr) in SCHEMES.items():
        lab = trend_labels(m, col, thr)
        sub = lab.dropna()
        n = len(sub)
        rec = {"方案": name, "有效日": n,
               "上行%": round(float((sub == "上行").mean() * 100), 1),
               "震荡%": round(float((sub == "震荡").mean() * 100), 1),
               "下行%": round(float((sub == "下行").mean() * 100), 1)}
        for side in ("上行", "震荡", "下行"):
            st = run_stats(lab, side)
            rec[f"{side}游程"] = st["runs"]
            rec[f"{side}中位"] = st["median"]
        # 切换频率：每年标签变化的次数
        flips = (sub != sub.shift()).sum()
        rec["切换/年"] = round(float(flips) / (n / 252.0), 1)
        # 风险区分力：下行 vs 上行的未来波动差
        fv = pd.DataFrame({"lab": sub, "fvol": m["fvol"].reindex(sub.index)}).dropna()
        med = fv.groupby("lab")["fvol"].median()
        rec["波动_上"] = round(float(med.get("上行", np.nan)), 1)
        rec["波动_下"] = round(float(med.get("下行", np.nan)), 1)
        rec["波动_差"] = round(rec["波动_下"] - rec["波动_上"], 1)
        rows.append(rec)
        # 逐年：下行天数占比
        year_rows[name] = (sub == "下行").groupby(sub.index.year).mean().round(2)

    tab = pd.DataFrame(rows).set_index("方案")
    yearly = pd.DataFrame(year_rows)

    out = Path(__file__).resolve().parents[1] / "out" / "state_lab"
    out.mkdir(parents=True, exist_ok=True)
    tab.to_csv(out / "trend_window.csv", encoding="utf-8-sig")
    md = ["# 趋势窗口 / 阈值 敏感性（A/B/C/D）\n", tab.to_string(), "\n",
          "## 逐年「下行」天数占比（看分类跨年是否稳定）\n", yearly.to_string(), "\n"]
    (out / "trend_window.md").write_text("\n".join(md), encoding="utf-8")

    print(tab.to_string())
    print()
    print("逐年下行占比：")
    print(yearly.to_string())
    print(f"\n→ {out / 'trend_window.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
