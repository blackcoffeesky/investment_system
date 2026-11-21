"""第 4 项：状态的转移结构（含"已持续天数"条件）。

    python -m experiment.state_lab.transitions

## 回答三个问题

1. **通用转移矩阵** `P(S_{t+1}=j | S_t=i)` —— 状态之间怎么流动
2. **持续时间依赖** `P(S_{t+1}=j | S_t=i, 已持续 d 天)` —— 待得越久越容易走，还是越难走？
   这是上 HSMM 的依据：若转移概率随 d 单调上升，几何分布（普通 HMM）就是错的假设。
3. **过渡态假设** —— `震荡-中波` 是不是低波↔高波之间的中转站

## 持续天数的分档

用户给的是「1~3 / 5~10 / 10~30 / 30+」，但 **10 出现了两次、4 没有归属**。
这里按无重叠的方式切：

| 档 | 范围 |
|---|---|
| D1 | 1–3 天 |
| D2 | **4–9 天**（4 天原本无归属，并进来；10 归到下一档） |
| D3 | 10–30 天 |
| D4 | 31 天以上 |

要改成严格 `(5,10)` / `(10,30)`，只改 `BUCKETS` 一行。

## 口径

- 转移只在**相邻交易日**之间统计（`t → t+1`），不跨缺失
- 每行给 `n`：样本太少的行不下结论（阈值 30）
- 两种归一化都存：行内概率（看流向）与全样本占比（看总量）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

BUCKETS: list[tuple[str, int, int]] = [
    ("D1 1-3天", 1, 3),
    ("D2 4-9天", 4, 9),
    ("D3 10-30天", 10, 30),
    ("D4 31天+", 31, 10**9),
]

MIN_N = 30


def durations(labels: pd.Series) -> pd.Series:
    """每一天处于当前状态的第几天（游程内 1 起）。"""
    s = labels.dropna()
    run_id = (s != s.shift()).cumsum()
    return s.groupby(run_id).cumcount() + 1


def transition_counts(labels: pd.Series) -> pd.DataFrame:
    """`t → t+1` 的计数矩阵（只统计相邻且都非空的行）。"""
    s = labels.dropna()
    nxt = s.shift(-1)
    ok = nxt.notna()
    return pd.crosstab(s[ok], nxt[ok])


def row_norm(counts: pd.DataFrame) -> pd.DataFrame:
    denom = counts.sum(axis=1).replace(0, np.nan)
    return counts.div(denom, axis=0).round(3)


def by_bucket(labels: pd.Series, buckets=BUCKETS) -> dict[str, pd.DataFrame]:
    s = labels.dropna()
    dur = durations(s)
    nxt = s.shift(-1)
    ok = nxt.notna()
    out: dict[str, pd.DataFrame] = {}
    for name, lo, hi in buckets:
        mask = ok & (dur >= lo) & (dur <= hi)
        if mask.sum() < MIN_N:
            continue
        out[name] = row_norm(pd.crosstab(s[mask], nxt[mask]))
    return out


def self_persistence(labels: pd.Series, buckets=BUCKETS) -> pd.DataFrame:
    """每个状态在每一档持续天数下的"留下来"概率。"""
    s = labels.dropna()
    dur = durations(s)
    nxt = s.shift(-1)
    ok = nxt.notna()
    rows = []
    for name, lo, hi in buckets:
        mask = ok & (dur >= lo) & (dur <= hi)
        if mask.sum() == 0:
            continue
        sub = pd.DataFrame({"cur": s[mask], "nxt": nxt[mask]})
        g = sub.groupby("cur")["nxt"]
        stay = g.apply(lambda x: float((x == x.name).mean())).round(3)
        n = g.size()
        for state in stay.index:
            rows.append({"档": name, "状态": state,
                         "留下概率": stay[state], "n": int(n[state])})
    return pd.DataFrame(rows).pivot(index="状态", columns="档", values="留下概率")


def main() -> int:
    from pathlib import Path

    from experiment.state_lab.cross_tab import load_matrix, trend_bucket, vol_bucket

    out = Path(__file__).resolve().parents[1] / "out" / "state_lab"
    out.mkdir(parents=True, exist_ok=True)

    m = load_matrix()
    tr, vo = trend_bucket(m), vol_bucket(m)
    cell = (tr.astype(object) + "-" + vo.astype(object)).where(tr.notna() & vo.notna())

    # ---- ① 通用转移矩阵（9×9）
    counts = transition_counts(cell)
    prob = row_norm(counts)
    prob.to_csv(out / "transitions_9x9.csv", encoding="utf-8-sig")

    # ---- ② 按持续天数分档（9×9 每档）
    bucketed = by_bucket(cell)
    for name, t in bucketed.items():
        t.to_csv(out / f"transitions_{name.split()[0]}.csv", encoding="utf-8-sig")

    # ---- ③ 自持续概率（按档）
    persist = self_persistence(cell)

    # ---- ④ 只在波动层 / 趋势层上看（3×3，可读性好得多）
    vol_t = by_bucket(vo)
    tr_t = by_bucket(tr)

    # ---- ⑤ 过渡态检验：震荡-中波的进出
    s = cell.dropna()
    nxt = s.shift(-1)
    ok = nxt.notna() & (s == "震荡-中波")
    into_mid = cell.dropna().shift(1)[ok].value_counts()
    out_of_mid = nxt[ok].value_counts()

    md = ["# 状态转移（第 4 项）\n",
          f"有效转移 {int(counts.values.sum())} 次；行内概率，`n` 小的行只看方向不看幅度\n",
          "\n## ① 通用转移矩阵 9×9（行内概率）\n", prob.fillna(0).to_string(),
          "\n\n### 每行的样本数\n", counts.sum(axis=1).to_string(), "\n",
          "\n## ② 自持续概率：待得越久越容易走？\n", persist.fillna(np.nan).to_string(), "\n",
          "\n## ③ 只在波动层（低/中/高）\n"]
    for name, t in vol_t.items():
        md += [f"**{name}**\n", t.fillna(0).to_string(), "\n"]
    md += ["\n## ④ 只在趋势层（上行/震荡/下行）\n"]
    for name, t in tr_t.items():
        md += [f"**{name}**\n", t.fillna(0).to_string(), "\n"]
    md += ["\n## ⑤ 震荡-中波：它是过渡态吗？\n",
           "**从哪里来：**\n", into_mid.to_string(), "\n",
           "\n**到哪里去：**\n", out_of_mid.to_string(), "\n",
           "\n（若「低波→中波→高波」这条链占主导，它就是过渡态；"
           "若进出很分散，它更可能是独立状态）\n"]
    (out / "transitions.md").write_text("\n".join(md), encoding="utf-8")

    print("=== 通用转移矩阵（9×9，行内概率）===")
    print(prob.fillna(0).to_string())
    print("\n=== 自持续概率（按已持续天数）===")
    print(persist.fillna(np.nan).to_string())
    print(f"\n→ {out / 'transitions.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
