"""冗余诊断：PCA **只是诊断，不是闸门**。

## PCA 能回答什么、不能回答什么

**能**：这批指标里，有多少是"同一个东西的不同写法"。
把 20 个指标压到 3 个主成分还解释 90% 方差，那说明它们高度共线——
**但不说明"该只保留 3 个"**。

**不能**：决定保留几个。原因三条：

1. PCA 找的是**方差最大方向**，不是"对交易最有用的方向"。
   一个低方差但方向意义明确的指标（比如 `ma_pos`）会被判成"次要成分"。
2. 两个指标可以高度相关却**不可互换**：
   `er`（顺不顺）和 `rv`（幅度大不大）在牛市里常同向，
   但"顺畅地小涨"和"震荡地大涨"是两种状态，PCA 会把它们混成"活跃度"一个成分。
3. 主成分**没有经济含义**，而状态命名的第一要求是可解释。

所以这里的用法是：**用 PCA 找重复，用经济含义决定留谁。**

## 判定谁该出局（按这个顺序问）

1. 它与组内某指标 |r| ≥ 0.9？（几乎重复）
2. 它的信息能不能被同组其它指标**线性**解释（VIF 高、增量 R² ≈ 0）？
3. 丢掉它之后，**它原本想表达的那个经济含义**还有别的指标承载吗？
   —— 第 3 条是最后一道闸：PCA 说重复，但经济含义没人接，就得留。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

#: 每个指标属于哪一层、想表达什么（**先有语义，再看统计**）
SEMANTICS: dict[str, tuple[str, str]] = {
    "er_20": ("趋势", "中期方向效率：走得顺不顺"),
    "er_120": ("趋势", "长期方向效率"),
    "drift_snr_20": ("趋势", "中期漂移是否显著（有标准误）"),
    "drift_snr_120": ("趋势", "长期漂移是否显著"),
    "cum_ret_20_pct": ("趋势", "中期方向（涨跌幅度）"),
    "cum_ret_120_pct": ("趋势", "长期方向"),
    "ma_pos_120_pct": ("趋势", "价格相对长期均线的位置"),
    "drawdown_250_pct": ("趋势", "距一年高点的回撤"),
    "autocorr_20": ("趋势", "收益的持续性（时域）"),
    "vr_5_100": ("趋势", "方差比（等价于自相关的加权和）"),
    "hurst_250": ("趋势", "长期依赖（仅诊断，不进模型）"),
    "rv_20_pct": ("波动", "20 日已实现波动"),
    "rv_60_pct": ("波动", "60 日已实现波动"),
    "parkinson_20_pct": ("波动", "高低价波动（含日内幅度）"),
    "gk_20_pct": ("波动", "四价波动（理论上最有效）"),
    "atr_14_pct": ("波动", "真实波幅（含跳空）"),
    "rv_pct_rank_250": ("波动", "当前波动在一年中的分位"),
    "vol_of_vol_20": ("波动", "波动率的变化速度"),
    "amount_z_250": ("量能", "成交额相对一年水平"),
    "adv_ratio": ("宽度", "上涨家数占比"),
    "limit_net": ("宽度", "涨跌停净占比"),
    "xs_std_pct": ("宽度", "个股收益的截面离散度（内部分歧）"),
    "margin_chg_5_pct": ("资金", "两融余额 5 日变化"),
    "margin_z_250": ("资金", "两融余额的相对水平"),
    "etf_shares_chg_20_pct": ("资金", "宽基 ETF 份额 20 日变化"),
}


def _z(df: pd.DataFrame) -> pd.DataFrame:
    """诊断用标准化：**全样本**替换成滚动 z 会让"重复性"看不准，
    所以这里刻意用全样本，并明确标注「仅诊断」。
    """
    return (df - df.mean()) / df.std(ddof=0).replace(0, np.nan)


def corr_clusters(df: pd.DataFrame, threshold: float = 0.9) -> list[list[str]]:
    """按 |r| ≥ threshold 做单链接聚类，返回近重复分组（只返回长度 ≥2 的组）。"""
    c = df.corr().abs()
    cols = list(df.columns)
    seen: set[str] = set()
    groups: list[list[str]] = []
    for col in cols:
        if col in seen:
            continue
        stack, group = [col], []
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            group.append(cur)
            for other in cols:
                if other not in seen and c.loc[cur, other] >= threshold:
                    stack.append(other)
        if len(group) > 1:
            groups.append(sorted(group))
    return groups


def pca_report(df: pd.DataFrame, prefix: str = "") -> pd.DataFrame:
    """主成分方差解释（每个维度各做一次）。"""
    from sklearn.decomposition import PCA

    x = _z(df).dropna()
    if len(x) < 50 or x.shape[1] < 2:
        return pd.DataFrame()
    p = PCA().fit(x.to_numpy())
    cum = np.cumsum(p.explained_variance_ratio_)
    rows = []
    for i, (ratio, c) in enumerate(zip(p.explained_variance_ratio_, cum), 1):
        rows.append({"维度": prefix, "主成分": f"PC{i}",
                     "解释方差": round(float(ratio), 4),
                     "累计": round(float(c), 4)})
    return pd.DataFrame(rows)


def vif_table(df: pd.DataFrame) -> pd.DataFrame:
    """方差膨胀因子。> 10 说明该指标基本能被其它指标的线性组合替代。"""
    x = _z(df).dropna()
    if len(x) < 50:
        return pd.DataFrame()
    rows = []
    corr = x.corr().to_numpy()
    try:
        inv = np.linalg.inv(corr)
    except np.linalg.LinAlgError:
        return pd.DataFrame()
    for i, col in enumerate(x.columns):
        rows.append({"指标": col, "VIF": round(float(inv[i, i]), 2)})
    return pd.DataFrame(rows).sort_values("VIF", ascending=False)


def report(df: pd.DataFrame) -> str:
    """把 PCA / 聚类 / VIF 拼成一段人读的报告。"""
    lines: list[str] = []
    layers: dict[str, list[str]] = {}
    for col in df.columns:
        layer = SEMANTICS.get(col, ("未登记", ""))[0]
        layers.setdefault(layer, []).append(col)

    lines.append("## 每层的主成分方差解释\n")
    for layer, cols in layers.items():
        sub = df[cols].dropna()
        if sub.shape[1] < 2:
            continue
        p = pca_report(sub, layer)
        if p.empty:
            continue
        top = p.head(3)
        lines.append(f"**{layer}**（{len(cols)} 个指标）")
        for _, r in top.iterrows():
            lines.append(f"  - {r['主成分']}: {r['解释方差']:.1%}（累计 {r['累计']:.1%}）")
        lines.append("")

    lines.append("\n## 近重复分组（|r| ≥ 0.9）\n")
    groups = corr_clusters(df)
    if not groups:
        lines.append("无——所有指标两两相关性都低于 0.9。")
    for g in groups:
        lines.append("- " + " / ".join(g))
        for col in g:
            sem = SEMANTICS.get(col, ("?", ""))[1]
            lines.append(f"    - `{col}`：{sem}")

    lines.append("\n## 高 VIF（> 10，说明能被其它指标线性替代）\n")
    v = vif_table(df)
    if v.empty:
        lines.append("（样本不足，跳过）")
    else:
        high = v[v["VIF"] > 10]
        if high.empty:
            lines.append("无——最大 VIF 为 %.1f" % v["VIF"].max())
        else:
            for _, r in high.iterrows():
                lines.append(f"  - `{r['指标']}`: VIF={r['VIF']}")
    return "\n".join(lines)
