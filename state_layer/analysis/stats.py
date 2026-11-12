"""状态画像：把 HMM 标签和观测矩阵拼起来，看每个状态"长什么样"。

这一层只做**描述**，不做判断。目的是回答一个问题：

    现有状态在资金维度上到底有没有区分度？

如果「趋势上行」和「趋势下行」在换手率、涨跌家数上几乎一样，
说明这套状态对资金诊断没用——那要换的是输入，不是模型。
"""

from __future__ import annotations

import pandas as pd

from state_layer import store

# 标签 → 中文名（与 causual 的命名保持一致）
LABEL_NAME = {
    "trend_up": "趋势上行",
    "range": "震荡",
    "trend_down": "趋势下行",
    "extreme_up": "极端上行",
    "extreme_down": "极端下行",
    "extreme_vol": "极端波动",
}


def _current(asset: str, features=None) -> dict:
    """延迟导入 port：port 会 import stats，模块级互相引用会成环。"""
    from state_layer.port import get_port

    return get_port().current_state(asset, features)


def load_labels(asset: str) -> pd.Series:
    """读状态标签（走 inference，P2 之后由它统一提供）。"""
    return store.load_labels(asset)


def current(asset: str) -> dict:
    """当前 spec（模型产物统一在 state_layer/saved_models）。"""
    try:
        return store.load_spec(asset)
    except FileNotFoundError:
        return {}


def profile(asset: str, obs: pd.DataFrame) -> pd.DataFrame:
    """各状态的天数、持续、收益、波动、成交、换手、宽度、两融变化。"""
    labels = load_labels(asset)
    if labels.empty or obs.empty:
        return pd.DataFrame()

    df = obs.reindex(labels.index).copy()
    df["label"] = labels
    df = df.dropna(subset=["label"])
    if df.empty:
        return pd.DataFrame()

    # 平均持续：把连续同标签的段切开
    seg = (df["label"] != df["label"].shift()).cumsum()
    durations = df.groupby([seg, "label"]).size().groupby(level=1).mean()

    g = df.groupby("label")
    out = pd.DataFrame({
        "n": g.size(),
        "share": g.size() / len(df),
        "avg_duration": durations,
        "mean_ret": g["ret"].mean(),
        "vol": g["ret"].std(),
        "amount": g["amount"].mean() if "amount" in df else float("nan"),
        "turnover": g["turnover"].mean() if "turnover" in df else float("nan"),
        "advance_ratio": g["advance_ratio"].mean() if "advance_ratio" in df else float("nan"),
        "limit_up": g["limit_up"].mean() if "limit_up" in df else float("nan"),
        "small_large": g["small_large"].mean() if "small_large" in df else float("nan"),
    }).sort_values("n", ascending=False)

    out.insert(0, "name", [LABEL_NAME.get(i, i) for i in out.index])
    return out.reset_index().rename(columns={"index": "label"})


def timeline(asset: str, obs: pd.DataFrame, days: int = 250) -> list[dict]:
    """时间轴用的数据：日期、收盘、成交额、宽度、状态。"""
    labels = load_labels(asset)
    if obs.empty:
        return []
    df = obs.tail(days).copy()
    df["label"] = labels.reindex(df.index).fillna("")
    close = obs["close"] if "close" in obs else None
    rows = []
    for d, r in df.iterrows():
        rows.append({
            "date": str(d.date()),
            "close": None if close is None or pd.isna(close.get(d)) else float(close.get(d)),
            "amount": None if pd.isna(r.get("amount")) else float(r.get("amount")),
            "advance_ratio": None if pd.isna(r.get("advance_ratio")) else float(r.get("advance_ratio")),
            "label": str(r.get("label") or ""),
        })
    return rows
