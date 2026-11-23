"""状态画像（核心计算）：把标签和面板拼起来，看每个状态"长什么样"。

这一层只做**描述**，不做判断。目的是回答一个问题：

    现有状态在资金维度上到底有没有区分度？

如果「趋势上行」和「趋势下行」在换手率、大小盘差上几乎一样，
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
    "low": "低波",
    "normal": "常波",
    "high": "高波",
    "weak": "弱",
    "strong": "强",
}


def load_labels(asset: str) -> pd.Series:
    """读状态标签：v2 优先（primary 组），没有 v2 回退 legacy。"""
    return store.labels_any(asset)


def observations(panel: pd.DataFrame) -> pd.DataFrame:
    """从面板自建画像用的观测列（2026-11-23 起 `analysis/observations.py` 已删）。"""
    idx = pd.DatetimeIndex(panel.index)
    out = pd.DataFrame(index=idx)
    close = panel["close"]
    out["close"] = close
    out["ret"] = close.pct_change() * 100
    out["vol20"] = out["ret"].rolling(20, min_periods=10).std() * 100
    out["amount"] = panel["amount"] if "amount" in panel else float("nan")
    out["turnover"] = panel["turnover"] if "turnover" in panel else float("nan")
    if "csi1000" in panel and "csi300" in panel:
        out["small_large"] = (panel["csi1000"].pct_change()
                              - panel["csi300"].pct_change()) * 100
    else:
        out["small_large"] = float("nan")
    return out


def profile(asset: str, obs: pd.DataFrame,
            labels: pd.Series | None = None) -> pd.DataFrame:
    """各状态的天数、持续、收益、波动、成交、换手、大小盘差。"""
    labels = (store.load_group_labels(asset) if labels is None
              else labels.dropna().astype(str))
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
        "small_large": g["small_large"].mean() if "small_large" in df else float("nan"),
    }).sort_values("n", ascending=False)

    out.insert(0, "name", [LABEL_NAME.get(i, i) for i in out.index])
    return out.reset_index().rename(columns={"index": "label"})
