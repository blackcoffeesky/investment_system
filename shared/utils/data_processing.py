"""数据处理：标准化、滚动 z、**单位原语**。纯函数，不读库、不 import 任何业务模块。

三个来源合并而来（原来分散在三处，各写各的窗口口径）：

- `state_layer/prep.py`：winsorize(原始分位) + standardize，HMM 训练/解码/在线共用
- `causual_core/utils/zscore.py`：边频率上的滚动 z，估计层与信号层共用
- 两者本质都是"把原始读数变成可比尺度"，放一起才能保证同一份口径

顺序约定：**先按原始分位 winsorize，再标准化**。反了会把几乎所有点压到边界（v5.8 的 bug）。

单位原语（2026-11-16 加，**名字里带单位**）
----------------------------------------
`SHIBOR_CHG_BP` / `MARGIN_CHG` / `VOLUME_Z` 这三个公式原本在
`causual/.../pipeline.py::prepare()` 和 `state_layer/.../features.py::_derive()`
**各写了一遍**，靠一条测试守着"两边算得一样"——那是"复制两份"的老形状。
现在收成三个原语，两个模块都调它们：

| 原语 | 等价写法 | 单位 |
|---|---|---|
| `diff_bp(s, periods=1)` | `s.diff(p) * 100` | **bp**（利率/利差类） |
| `pct_change_pct(s, periods=1)` | `s.pct_change(p) * 100` | **百分数**（2.5 表示 2.5%） |
| `rolling_z(s, window=60, min_periods=20)` | `(s - rolling_mean) / rolling_std`，标准差为 0 记 NaN | 无量纲 |

名字里的 `bp` / `pct` 不是装饰：**单位错一个数量级，卡片上的数就全错**，
所以宁可名字长一点，也不要用 `chg()` 这种看不出单位的名字。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def diff_bp(s: pd.Series, periods: int = 1) -> pd.Series:
    """差分 × 100，单位 **bp**（利率 / 利差类：`SHIBOR3M`、`FED_RATE`、`REAL_YIELD_10Y`）。

    等价于 `s.diff(periods) * 100.0`——**不是**"百分比变化"，别和 `pct_change_pct` 混用：
    利差从 1.00% 到 1.10% 是 **+10 bp**，而 pct_change 会给出 +10%。
    """
    return s.diff(periods) * 100.0


def pct_change_pct(s: pd.Series, periods: int = 1) -> pd.Series:
    """百分比变化 × 100，单位 **百分数**（`MARGIN_CHG`：2.5 表示 +2.5%）。

    等价于 `s.pct_change(periods) * 100.0`。
    """
    return s.pct_change(periods) * 100.0


def rolling_z(s: pd.Series, window: int = 60, min_periods: int = 20) -> pd.Series:
    """滚动 z 分数：`(s - 均值) / 标准差`，**标准差为 0 时记 NaN**（不是 0、不是 inf）。

    等价于 `VOLUME_Z` 的手写版本：
    `(s - s.rolling(w, min_periods=mp).mean()) / s.rolling(w, min_periods=mp).std().replace(0, np.nan)`。
    保留 `.replace(0, np.nan)` 这一层：整段横盘时标准差会是 0，直接除会得到 `±inf`，
    而 `inf` 进 HMM / 分位会污染整列。
    """
    mu = s.rolling(window, min_periods=min_periods).mean()
    sd = s.rolling(window, min_periods=min_periods).std().replace(0, np.nan)
    return (s - mu) / sd



def prep_features(
    raw: pd.DataFrame,
    scaler,
    lo: pd.Series,
    hi: pd.Series,
) -> np.ndarray:
    """winsorize(原始量纲) → standardize。返回 (n_samples, n_features)。"""
    clipped = raw.clip(lower=lo, upper=hi, axis=1)
    if hasattr(scaler, "transform"):
        return np.asarray(scaler.transform(clipped), dtype=float)
    # 也接受直接给 mean/scale（在线重建时用）
    mean = np.asarray(getattr(scaler, "mean_", scaler["mean"]), dtype=float)
    scale = np.asarray(getattr(scaler, "scale_", scaler["scale"]), dtype=float)
    return (np.asarray(clipped, dtype=float) - mean) / scale


def fit_bounds(feat: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """训练窗口的 1% / 99% 分位，作为 winsorize 边界。"""
    hist = feat.dropna()
    if hist.empty:
        raise ValueError("训练窗口内没有有效特征行")
    return hist.quantile(0.01), hist.quantile(0.99)



# 各频率的默认回看窗口（期数）
WINDOW_BY_FREQ: dict[str, int] = {"D": 252, "W": 104, "M": 24, "Q": 8}


def _window_for(freq: str, window: int | None) -> int:
    if window:
        return int(window)
    key = str(freq)[0].upper()
    return WINDOW_BY_FREQ.get(key, 252)


def periodic_z(
    series: pd.Series,
    freq: str = "D",
    window: int | None = None,
    min_periods: int | None = None,
) -> pd.Series:
    """在 series 自己的索引上做滚动标准化（无前视）。"""
    w = _window_for(freq, window)
    mp = min_periods if min_periods is not None else max(12, w // 3)
    mu = series.rolling(w, min_periods=mp).mean()
    sd = series.rolling(w, min_periods=mp).std().replace(0, np.nan)
    return (series - mu) / sd


def edge_z(
    df: pd.DataFrame,
    col: str,
    freq: str = "D",
    window: int | None = None,
    min_periods: int | None = None,
) -> pd.Series:
    """按边自己的频率算滚动 z，再对齐回日频。

    估计层用 `periodic_z`（在重采样后的面板上），信号层用本函数，
    两者窗口与 min_periods 必须一致，否则又会对不上。
    """
    if str(freq) == "D":
        return periodic_z(df[col], freq, window, min_periods)
    s = df[col].resample(freq).last()
    z = periodic_z(s, freq, window, min_periods)
    return z.reindex(df.index, method="ffill")
