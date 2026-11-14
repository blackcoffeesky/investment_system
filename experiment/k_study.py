"""K 值研究：不同状态数下的市场分类长什么样。

**这是试验代码，不是生产代码。** 可以随便 import 任何模块、打印任何东西。
生产路径（service / 网页 / 卡片）不依赖这个文件。

跑法：

    python -m experiment.k_study --asset ashare --k 2,3,4,5,6

产物写到 `experiment/out/`：每个 K 一张分割图、一张画像表，外加汇总。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

# 极端日的颜色（规则层认领的日子，不参与 HMM 状态）
EXTREME = "extreme"


# ---------------------------------------------------------------- 数据
def load_features(asset: str = "ashare") -> tuple[pd.DataFrame, pd.Series]:
    """按生产同一条路径取特征：datahub 端口 → 资产包面板 → 特征。

    同时返回收盘价——画分割图时用真实价格，比累计收益直观。
    """
    from datahub.port import get_port as data_port
    from state_layer import state_assets

    pkg = state_assets.load(asset)
    port = data_port()
    series = port.series_map(list(pkg.REQUIRED_TABLES))
    panel = pkg.panel(series, port.metadata())
    feats = pkg.features.build(panel)
    close = panel["CLOSE"].reindex(feats.dropna().index)
    return feats.dropna(), close


def split_extreme(feats: pd.DataFrame, asset: str = "ashare",
                  params: dict | None = None) -> tuple[pd.DataFrame, pd.Series]:
    """用生产同一套规则把极端日挑出来，剩下的才是 HMM 的输入。"""
    from state_layer import state_assets
    from state_layer.model.extreme import rules

    if params is None:
        params = state_assets.load(asset).config.MODEL_PARAMS.get("extreme")
    ext = rules.classify(feats, rules.from_dict(params or {}))
    return feats.loc[ext == ""], ext


# ---------------------------------------------------------------- 拟合
def fit_k(regular: pd.DataFrame, k: int, seed: int = 42) -> dict:
    """全样本拟合一个 K 状态的 HMM（研究用：不做 walk-forward，快）。"""
    from state_layer import metrics
    from shared.data_processing import prep_features
    from state_layer.model.hmm import _ops as H

    lo, hi = H.fit_window_bounds(regular)
    scaler = H.make_scaler(H.clip_frame(regular, lo, hi))
    X = prep_features(regular, scaler, lo, hi)

    model = H.fit(X, k, seed=seed)
    states = pd.Series(H.decode(model, X), index=regular.index, name="state")
    sep = metrics.score("bhattacharyya",
                        {"means": model.means_, "covars": model.covars_},
                        threshold=0.4)

    # BIC：-2·logL + p·ln(n)；p = 均值 + 协方差 + 转移 + 初始
    n, d = X.shape
    p_free = k * d + k * d * (d + 1) / 2 + k * (k - 1) + (k - 1)
    bic = -2.0 * float(model.score(X)) + p_free * np.log(n)

    # 还原到原始量纲，画像才看得懂
    raw_means = pd.DataFrame(model.means_ * scaler.scale_ + scaler.mean_,
                             columns=regular.columns, index=[f"s{i}" for i in range(k)])
    return {"k": k, "states": states, "sep": sep, "bic": bic,
            "raw_means": raw_means, "transmat": model.transmat_}


def profile(res: dict, regular: pd.DataFrame) -> pd.DataFrame:
    """每个状态的画像：天数、占比、平均持续、日均收益、波动。"""
    st = res["states"]
    ret = regular["log_return"]
    run = (st != st.shift()).cumsum()
    dur = st.groupby(run).size()
    first = st.groupby(run).first()

    rows = []
    for sid, name in enumerate(f"s{i}" for i in range(res["k"])):
        mask = st == sid
        n = int(mask.sum())
        r = ret[mask]
        rows.append({
            "state": name,
            "days": n,
            "share": n / max(len(st), 1),
            "avg_duration": float(dur[first == sid].mean()) if (first == sid).any() else 0,
            "mean_ret_pct": float(r.mean() * 100),
            "vol_pct": float(r.std() * 100),
            "stay_prob": float(res["transmat"][sid, sid]),
            "ma20_slope": float(regular.loc[mask, "ma20_slope"].mean()),
            "realized_vol": float(regular.loc[mask, "realized_vol_20"].mean()),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 画图
def plot_segments(res: dict, feats: pd.DataFrame, close: pd.Series,
                  path: Path, title: str) -> Path:
    """价格曲线 + 按状态分段上色——看"市场是怎么被切开的"。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    st = res["states"]
    colors = plt.get_cmap("tab10")
    fig, ax = plt.subplots(figsize=(13, 4.2))

    ax.plot(close.index, close.values, color="black", lw=0.9, zorder=3)
    y0, y1 = float(close.min()), float(close.max())
    pad = (y1 - y0) * 0.05

    # 连续同状态的区间画一条背景带
    run_id = (st != st.shift()).cumsum()
    for _, block in st.groupby(run_id):
        if block.empty:
            continue
        sid = int(block.iloc[0])
        ax.axvspan(block.index[0], block.index[-1],
                   color=colors(sid % 10), alpha=0.22, lw=0)
    from matplotlib.patches import Patch

    handles = [Patch(facecolor=colors(sid % 10), alpha=0.45, label=f"s{sid}")
               for sid in range(res["k"])]
    ax.legend(handles=handles, loc="upper left", fontsize=8, ncol=res["k"])

    ax.set_ylim(y0 - pad, y1 + pad)
    ax.set_title(title, fontsize=11)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


# ---------------------------------------------------------------- 主流程
def run(asset: str = "ashare", ks: tuple[int, ...] = (2, 3, 4, 5, 6),
        seed: int = 42) -> pd.DataFrame:
    feats, close = load_features(asset)
    regular, ext = split_extreme(feats, asset)

    summary = []
    for k in ks:
        res = fit_k(regular, k, seed=seed)
        prof = profile(res, regular)
        prof.to_csv(OUT / f"profile_{asset}_k{k}.csv", index=False, encoding="utf-8-sig")
        plot_segments(res, regular, close.reindex(regular.index),
                      OUT / f"segments_{asset}_k{k}.png",
                      f"{asset}  K={k}  separation={res['sep']['min']:.2f}  BIC={res['bic']:.0f}")

        summary.append({
            "k": k,
            "separation": res["sep"]["min"],
            "passed": res["sep"]["passed"],
            "bic": res["bic"],
            "min_share": prof["share"].min(),
            "max_share": prof["share"].max(),
            "avg_duration": prof["avg_duration"].mean(),
        })
        print(f"K={k}  分离度={res['sep']['min']:.3f}  "
              f"BIC={res['bic']:.0f}  最不均状态占比={prof['share'].min():.1%}")

    out = pd.DataFrame(summary)
    out.to_csv(OUT / f"summary_{asset}.csv", index=False, encoding="utf-8-sig")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="ashare")
    ap.add_argument("--k", default="2,3,4,5,6")
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args(argv)
    ks = tuple(int(x) for x in a.k.split(","))
    out = run(a.asset, ks, a.seed)
    print()
    print(out.round(3).to_string(index=False))
    print(f"\n产物：{OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
