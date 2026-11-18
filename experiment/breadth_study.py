"""加入「市场宽度」特征后，HMM 有没有变好？

用法：

    python -m experiment.breadth_study            # 全部
    python -m experiment.breadth_study --asset ashare

## 为什么单独做这个实验

生产的状态特征（`config.FEATURES`）全是**价格/波动/资金价格**派生的：
`log_return / realized_vol_20 / volume_z / ma20_slope / size_mom /
margin_chg_z / rv_ratio / shibor_chg_z / etf_dispersion`。

**没有一条直接描述"多少股票在涨"**。`breadth` 早就算好了
（`adv / dec / flat / limit_up / limit_down / total`，见 `datahub/fin/derive.py`），
但一直没进面板、也没进特征。

## 候选特征

| 名字 | 算法 | 想表达 |
|---|---|---|
| `adv_ratio` | `adv / (adv + dec)` | 上涨家数占比（0~1） |
| `limit_net` | `(limit_up − limit_down) / total` | 涨跌停净占比（−1~1） |

两个都是**无量纲**的，不用和价格量纲对齐就能进 HMM 的标准化。

## 判据（按重要性排）

1. **分离度**（`metrics.separation`，越高越好）——状态的均值有没有分开
2. **BIC**（越低越好）——同样拟合能力下，多两个特征要付出代价
3. **与基线的 ARI**（`adjusted_rand_score`）——**标签变了多少**
   - ARI > 0.9：基本没变，加了也白加
   - 0.5 ~ 0.9：有实质变化，要人工看分割图才知道是变好还是变坏
   - < 0.5：换了一套划分，**不能只看指标就采纳**
4. **多 seed 稳定性**（seed = 42 / 7 / 2024 两两 ARI）——比单次 ARI 更能说明"这套特征是不是稳"

## 一个必须遵守的细节

**所有特征组必须在同一个行集合上拟合**。否则样本量不同，BIC 不可比。
所以代码里取四组的共同非空索引，再各自拟合。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

SEEDS = (42, 7, 2024)
K = 3


def breadth_features(start: str | None = None) -> pd.DataFrame:
    """从数据层取 `breadth` 派生量，折成两个无量纲特征。

    ⚠️ 试验场允许直接取数（`tests/cross/test_layering.py` 跳过 `experiment/`）。
    **生产代码不能这么写**——那边必须走 `get_data` / 装配点传参。
    """
    from datahub.port import get_port

    got = get_port().series_map(["breadth"], start=start)
    cols = {k.split(":", 1)[1]: v for k, v in got.items() if k.startswith("breadth:")}
    adv, dec = cols["adv"], cols["dec"]
    lu, ld, total = cols["limit_up"], cols["limit_down"], cols["total"]

    out = pd.DataFrame(index=adv.index)
    denom = (adv + dec).replace(0, np.nan)
    out["adv_ratio"] = (adv / denom).clip(0, 1)
    out["limit_net"] = ((lu - ld) / total.replace(0, np.nan)).clip(-1, 1)

    # 另外两种**形态**：水平值 vs 变化量 vs 滚动 z。
    # 水平值可能和已有特征共线（ARI 0.958 就是疑似冗余），变化量/z 才可能带来新信息。
    out["adv_ratio_chg"] = out["adv_ratio"] - out["adv_ratio"].shift(5)
    mu = out["limit_net"].rolling(60, min_periods=20).mean()
    sd = out["limit_net"].rolling(60, min_periods=20).std()
    out["limit_net_z"] = (out["limit_net"] - mu) / sd.replace(0, np.nan)
    return out


def build_sets(asset: str) -> dict[str, pd.DataFrame]:
    """四个特征组。**只在共同索引上比较**（见模块 docstring）。"""
    from experiment.k_study import load_features

    base, _close = load_features(asset)
    br = breadth_features()

    sets = {
        "baseline": base,
        "+adv_ratio": base.join(br[["adv_ratio"]], how="left"),
        "+limit_net": base.join(br[["limit_net"]], how="left"),
        "+both": base.join(br[["adv_ratio", "limit_net"]], how="left"),
        "+adv_ratio_chg": base.join(br[["adv_ratio_chg"]], how="left"),
        "+limit_net_z": base.join(br[["limit_net_z"]], how="left"),
    }

    # 同一行集合：四组都非空的行
    common = None
    for df in sets.values():
        idx = df.dropna().index
        common = idx if common is None else common.intersection(idx)
    if common is None or len(common) == 0:
        raise RuntimeError("四个特征组没有共同非空区间")
    return {k: v.loc[common] for k, v in sets.items()}


def run(asset: str = "ashare") -> pd.DataFrame:
    from sklearn.metrics import adjusted_rand_score

    from experiment.k_study import fit_k

    sets = build_sets(asset)
    rows = []
    base_states = None

    for name, feats in sets.items():
        res = fit_k(feats, K, seed=SEEDS[0])
        if name == "baseline":
            base_states = res["states"]

        # 多 seed 稳定性
        others = [fit_k(feats, K, seed=s)["states"] for s in SEEDS[1:]]
        seeds_ari = []
        for a, b in ((res["states"], others[0]), (res["states"], others[1]),
                     (others[0], others[1])):
            seeds_ari.append(adjusted_rand_score(a, b))

        dist = res["states"].value_counts(normalize=True).sort_index()
        rows.append({
            "特征组": name,
            "维度": feats.shape[1],
            "样本": feats.shape[0],
            "BIC": res["bic"],
            "分离度": round(float(res["sep"]["min"]), 3),
            "最窄状态占比": round(float(dist.min()), 3),
            "与基线ARI": 1.0 if name == "baseline"
                          else round(adjusted_rand_score(base_states, res["states"]), 3),
            "seed稳定性": round(float(np.mean(seeds_ari)), 3),
        })

    return pd.DataFrame(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="ashare", choices=("ashare", "gold"))
    a = ap.parse_args(argv)

    if a.asset != "ashare":
        print("目前只做 ashare：黄金的状态特征里没有市场宽度这回事（它不看个股）",
              file=sys.stderr)
        return 2

    df = run(a.asset)
    path = OUT / f"breadth_study_{a.asset}.csv"
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(df.to_string(index=False))
    print(f"\n→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
