"""市场状态指标库（研究用，纯函数）。

设计约束（**不是风格偏好，是正确性要求**）：

1. **全部只能用 t 及之前的数据**。任何 `zscore` / `percentile` 都必须是**滚动窗口**
   且带 `min_periods`；不许用全样本均值/标准差。理由见 `tests` 里那条
   "窗口不变量"的同类思路——全样本标准化会让 t 时刻的值随未来数据变化。
2. **不要用同一统计量换皮**。趋势层用「方向效率」，波动层用「幅度」，
   两者都从收益率出发但回答不同问题。混用会让 PCA 看到一个虚假的"主导因子"。
3. **单位进名字**。`_pct` / `_bp` / `_ratio` 一眼能看出来，避免 100 倍错误。

命名里 `n` 是窗口（交易日数）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# ---------------------------------------------------------------- 基础


def _clean(s: pd.Series) -> pd.Series:
    """去掉非正价格（停牌/坏数据会让 log 和比值炸掉）。"""
    return s.where(s > 0)


def ret(close: pd.Series) -> pd.Series:
    """简单收益率（不是对数），后面所有指标都以它为基准。"""
    return _clean(close).pct_change()


def log_ret(close: pd.Series) -> pd.Series:
    return np.log(_clean(close)).diff()


# ---------------------------------------------------------------- 趋势：方向效率


def er(close: pd.Series, n: int = 20) -> pd.Series:
    """Efficiency Ratio（Kaufman）：净位移 / 总路径长度，0~1。

    接近 1 = 一路走到底（趋势）；接近 0 = 来回走（震荡）。
    
    **它不含方向**：`er` 高只说明"是有方向的运动"，涨跌要看 `drift`。
    这两个必须一起用，否则会把"顺畅的下跌"当成长得一样的状态。
    """
    p = _clean(close)
    change = (p - p.shift(n)).abs()
    path = p.diff().abs().rolling(n, min_periods=n).sum()
    return (change / path.replace(0, np.nan)).clip(0, 1)


def drift_snr(close: pd.Series, n: int = 20) -> pd.Series:
    """漂移 / 噪声：n 日平均收益除以它的标准误。

    有标准误 ⇒ 可以判显著性；`er` 没有这个性质。
    两者放一起：`er` 说"顺不顺"，`snr` 说"这个方向是不是显著"。
    """
    r = ret(close)
    mu = r.rolling(n, min_periods=max(5, n // 2)).mean()
    se = r.rolling(n, min_periods=max(5, n // 2)).std() / np.sqrt(n)
    return mu / se.replace(0, np.nan)


def cum_ret_pct(close: pd.Series, n: int = 20) -> pd.Series:
    """n 日累计收益（%）。状态命名用：牛/熊靠它，不靠 er。"""
    p = _clean(close)
    return (p / p.shift(n) - 1.0) * 100.0


def ma_pos_pct(close: pd.Series, n: int = 120) -> pd.Series:
    """价格相对 n 日均线的位置（%）。长期方向的粗判。"""
    p = _clean(close)
    ma = p.rolling(n, min_periods=n // 2).mean()
    return (p / ma - 1.0) * 100.0


def drawdown_pct(close: pd.Series, n: int = 250) -> pd.Series:
    """距 n 日高点的回撤（%，负值）。

    ⚠️ 只用**过去** n 日的最高点，不是未来——这一行决定了它有没有前视。
    """
    p = _clean(close)
    peak = p.rolling(n, min_periods=n // 2).max()
    return (p / peak - 1.0) * 100.0


def autocorr(close: pd.Series, n: int = 20, lag: int = 1) -> pd.Series:
    """n 日窗口内 lag 阶自相关。VR 的时域等价物，二选一即可。"""
    r = ret(close)
    return r.rolling(n, min_periods=max(10, n // 2)).apply(
        lambda x: pd.Series(x).autocorr(lag=lag), raw=False)


def variance_ratio(close: pd.Series, q: int = 5, n: int = 100) -> pd.Series:
    """VR(q)：q 期收益方差 / (q × 1 期方差)。随机游走 ≈ 1。

    > 1 动量、< 1 均值回复。与 `autocorr` 高度相关（数学上 VR-1 是各阶自相关的
    加权和），所以**两者只留一个**。
    """
    r = ret(close)
    rq = _clean(close).pct_change(q)
    var1 = r.rolling(n, min_periods=max(20, n // 2)).var()
    varq = rq.rolling(n, min_periods=max(20, n // 2)).var()
    return varq / (q * var1.replace(0, np.nan))


def hurst_diag(close: pd.Series, n: int = 250) -> pd.Series:
    """Hurst 指数（**仅诊断，不进模型**）。

    用 R/S 的简化估计。有限样本偏差大、非平稳序列上会给出假信号——
    放在这里是为了让你能"看到"它和其它指标的关系，不是让你把它塞进 HMM。
    """
    def _h(x: np.ndarray) -> float:
        x = np.asarray(x, dtype=float)
        if len(x) < 20 or not np.isfinite(x).all():
            return np.nan
        y = x - x.mean()
        cum = np.cumsum(y)
        rng = cum.max() - cum.min()
        sd = y.std()
        if sd <= 0 or rng <= 0:
            return np.nan
        return float(np.log(rng / sd) / np.log(len(x) / 2.0))

    return np.log(_clean(close)).diff().rolling(
        n, min_periods=n // 2).apply(_h, raw=True)


# ---------------------------------------------------------------- 波动：幅度


def rv_pct(close: pd.Series, n: int = 20, ann: int = 252) -> pd.Series:
    """已实现波动率（年化 %）。最直观的幅度指标。"""
    return ret(close).rolling(n, min_periods=max(5, n // 2)).std() * np.sqrt(ann) * 100.0


def parkinson_pct(high: pd.Series, low: pd.Series, n: int = 20,
                  ann: int = 252) -> pd.Series:
    """Parkinson 波动率：只用高低价，比收盘价法效率高（样本内更少噪声）。

    σ² = (1/(4ln2)) · mean(ln(H/L)²)
    """
    hl = np.log(_clean(high) / _clean(low))
    var = (hl ** 2).rolling(n, min_periods=max(5, n // 2)).mean() / (4 * np.log(2))
    return np.sqrt(var) * np.sqrt(ann) * 100.0


def garman_klass_pct(open_: pd.Series, high: pd.Series, low: pd.Series,
                     close: pd.Series, n: int = 20, ann: int = 252) -> pd.Series:
    """Garman-Klass：用四个价，理论上比 Parkinson 更有效。

    σ² = mean(0.5·ln(H/L)² − (2ln2−1)·ln(C/O)²)
    """
    hl = np.log(_clean(high) / _clean(low)) ** 2
    co = np.log(_clean(close) / _clean(open_)) ** 2
    var = (0.5 * hl - (2 * np.log(2) - 1) * co).rolling(
        n, min_periods=max(5, n // 2)).mean()
    return np.sqrt(var.clip(lower=0)) * np.sqrt(ann) * 100.0


def atr_pct(high: pd.Series, low: pd.Series, close: pd.Series,
            n: int = 14) -> pd.Series:
    """ATR / 收盘价（%）。和 RV 的区别：它含跳空（用前收算 TR）。"""
    pc = _clean(close).shift(1)
    tr = pd.concat([(_clean(high) - _clean(low)).abs(),
                    (_clean(high) - pc).abs(),
                    (low - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=max(5, n // 2)).mean() / _clean(close) * 100.0


def vol_pct_rank(vol: pd.Series, n: int = 250) -> pd.Series:
    """波动率的历史分位（0~1）。

    **只看过去 n 期**：`rolling(n).rank` 的等价写法是
    `rolling.apply(lambda x: (x < x[-1]).mean())`，这里用后者以免依赖 pandas 版本。
    """
    return vol.rolling(n, min_periods=max(60, n // 3)).apply(
        lambda x: float((x[:-1] < x[-1]).mean()) if len(x) > 1 else np.nan, raw=True)


def vol_of_vol(vol: pd.Series, n: int = 20) -> pd.Series:
    """波动率的变化速度：vol 的 n 日变化（%/日）。

    单纯"高波动"和"波动在上升"是两件事——后者更接近"风险正在恶化"。
    """
    return vol.pct_change(n) * 100.0


# ---------------------------------------------------------------- 宽度 / 结构


def xs_dispersion_pct(stock_daily: pd.DataFrame, n: int = 1) -> pd.DataFrame:
    """个股收益的截面离散度（%）。

    输入 `stock_daily` 长表（`code / date / close`），输出按日的
    `xs_std_pct`（横截面标准差）与 `xs_iqr_pct`（横截面四分位距）。

    这是「资金分歧 / 市场内部一致性」的**不依赖行业分类**的度量：
    指数在涨但个股离散度很高 = 涨的是少数票。
    """
    d = stock_daily[["code", "date", "close"]].copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values(["code", "date"])
    d["r"] = d.groupby("code")["close"].pct_change(n)
    g = d.dropna(subset=["r"]).groupby("date")["r"]
    out = pd.DataFrame({
        "xs_std_pct": g.std() * 100.0,
        "xs_iqr_pct": (g.quantile(0.75) - g.quantile(0.25)) * 100.0,
    })
    return out


def adv_ratio(adv: pd.Series, dec: pd.Series) -> pd.Series:
    """上涨家数占比（0~1）。`breadth` 派生量给了 adv/dec。"""
    return (adv / (adv + dec).replace(0, np.nan)).clip(0, 1)


def limit_net(limit_up: pd.Series, limit_down: pd.Series,
              total: pd.Series) -> pd.Series:
    """涨跌停净占比（−1~1）。"""
    return ((limit_up - limit_down) / total.replace(0, np.nan)).clip(-1, 1)


# ---------------------------------------------------------------- 资金


def chg_pct(s: pd.Series, n: int = 5) -> pd.Series:
    """n 期变化率（%）。资金这类快变量用短窗口。"""
    return s.pct_change(n) * 100.0


def level_z(s: pd.Series, n: int = 250) -> pd.Series:
    """滚动 z（水平值），窗口默认一年。**滚动，不是全样本。**"""
    mu = s.rolling(n, min_periods=n // 3).mean()
    sd = s.rolling(n, min_periods=n // 3).std()
    return (s - mu) / sd.replace(0, np.nan)
