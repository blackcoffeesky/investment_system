"""纯派生列：**输入是面板（DataFrame），输出是 Series / DataFrame**。

规则
----
1. 不读库、不碰文件、不看 `state` / `spec`——只吃调用方给的 DataFrame；
2. 依赖外部参数（标签体系、payoff、边模型）的派生**不进这里**，留在模块里；
3. 输入列名是**语义名**（`close` / `etf_sse50`），由 `shared.adapter` 保证；
4. 算不出来时**返回全 NaN 并告警**——"列还在、只是没值"是最难查的失效方式。

⚠️ ETF 分组份额列是**原生观测日**（非观测日为 NaN）。所以这里统一先
`.dropna()` 还原成原生序列再算变化率，最后才 ffill 回面板索引——
和改造前 `panel.py` 里的算法逐位一致。
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

# 宽基 ETF 分组列（组定义在 shared/adapter/_ETF_GROUPS）
ETF_GROUPS = ("etf_sse50", "etf_csi300", "etf_csi1000")


def _warn(msg: str) -> None:
    print(f"[features] {msg}", file=sys.stderr)


def _finite(s: pd.Series) -> pd.Series:
    """±inf → NaN。

    ETF 上市前份额是 0，`pct_change()` 会给出 inf（首日 0 → 正数）。
    inf 本身不报错，但进 z-score / 分位 / 相关系数就污染整列。
    """
    return s.replace([np.inf, -np.inf], np.nan)


# ---------------------------------------------------------------- 通用
def pct_change(s: pd.Series, periods: int = 1) -> pd.Series:
    """变化率（%）。`periods` 按**行数**——低频序列请先聚合或用 `log_return`。"""
    return _finite(s.pct_change(periods) * 100.0)


def log_return(close: pd.Series) -> pd.Series:
    """对数收益（%）：`100 * ln(P_t / P_{t-1})`。"""
    return _finite(np.log(close / close.shift(1)) * 100.0)


def zscore(s: pd.Series, window: int | None = None) -> pd.Series:
    """标准化。`window=None` 用全样本；给了 window 就是滚动 z。"""
    if window:
        mu = s.rolling(window, min_periods=max(2, window // 3)).mean()
        sd = s.rolling(window, min_periods=max(2, window // 3)).std()
    else:
        mu, sd = s.mean(), s.std()
    return _finite((s - mu) / sd.replace(0, np.nan))


def ma_slope(s: pd.Series, window: int = 20) -> pd.Series:
    """均线斜率：`均线 / 均线.shift(1) - 1`（%，逐日）。"""
    ma = s.rolling(window, min_periods=max(2, window // 2)).mean()
    return _finite((ma / ma.shift(1) - 1.0) * 100.0)


# ---------------------------------------------------------------- ETF 派生
def _native(panel: pd.DataFrame, col: str) -> pd.Series | None:
    """取原生观测序列（去掉 NaN）。列不在 / 全空返回 None。"""
    if col not in panel.columns:
        return None
    s = panel[col].dropna()
    return s if len(s) else None


def etf_dispersion(panel: pd.DataFrame, groups: tuple[str, ...] = ETF_GROUPS) -> pd.Series:
    """宽基 ETF 申赎的分歧度：各组份额变化的截面标准差。

    ⚠️ **顺序不能动**：`dropna()` → `pct_change()` → `std(axis=1)` → 最后才
    `reindex(index, method="ffill")`。**ffill 只能作用在结果上，绝不能先作用在输入上**
    ——组份额是存量，先 ffill 再算变化率，非观测日的变化就变成 0，
    整条特征会静默换掉（实测近 250 日：均值 0.0478 → 0.0134（−72%），
    零值占比 0% → 66.4%，只有观测日那 33.6% 的日期还相同）。
    不报错、不抛异常，但特征换了一条 → 标签跟着变 → 边快照全部失效。

    组数 < 2 就整列 NaN 并告警（整列 NaN 会让 HMM 一行都不认领——曾经就是这么坏的）。
    """
    parts = [s.pct_change() for g in groups
             if (s := _native(panel, g)) is not None]
    if len(parts) < 2:
        _warn(f"ETF 分组份额只有 {len(parts)} 组可用，etf_dispersion 整列 NaN")
        return pd.Series(index=panel.index, dtype=float)
    return _finite(pd.concat(parts, axis=1).std(axis=1).reindex(
        panel.index, method="ffill"))


def size_flow(panel: pd.DataFrame, small: str = "etf_csi1000",
              large: str = "etf_csi300") -> pd.Series:
    """大小盘 ETF 份额变化之差（小盘 − 大盘），衡量资金在风格间的迁移。"""
    a, b = _native(panel, small), _native(panel, large)
    if a is None or b is None:
        _warn(f"缺少 {small} / {large}，size_flow 整列 NaN")
        return pd.Series(index=panel.index, dtype=float)
    return _finite((a.pct_change() - b.pct_change()).reindex(
        panel.index, method="ffill"))


def _national_native(panel: pd.DataFrame,
                     groups: tuple[str, ...] = ("etf_sse50", "etf_csi300")) -> pd.Series | None:
    """国家队口径的份额合计（**原生观测日**，未 ffill）。"""
    parts = [s for g in groups if (s := _native(panel, g)) is not None]
    if not parts:
        return None
    return sum(parts)


def national_etf(panel: pd.DataFrame,
                 groups: tuple[str, ...] = ("etf_sse50", "etf_csi300")) -> pd.Series:
    """国家队净申购：宽基（上证50 + 沪深300）份额合计的差分。"""
    nat = _national_native(panel, groups)
    if nat is None:
        _warn(f"缺少 {groups}，national_etf 整列 NaN")
        return pd.Series(index=panel.index, dtype=float)
    return _finite(nat.diff().reindex(panel.index, method="ffill"))


def national_etf_event(panel: pd.DataFrame, window: int = 60, q: float = 0.95,
                       groups: tuple[str, ...] = ("etf_sse50", "etf_csi300")) -> pd.Series:
    """国家队异动：净申购超过滚动 `window` 日 `q` 分位 → 1，否则 0。

    ⚠️ 分位必须在**原生观测日**上算，再 ffill 回日频；先在日频 ffill 过的
    序列上滚分位会把同一天的值重复计入，阈值就偏了。
    """
    nat = _national_native(panel, groups)
    if nat is None:
        _warn(f"缺少 {groups}，national_etf_event 整列 NaN")
        return pd.Series(index=panel.index, dtype=float)
    dn = nat.diff()
    # min_periods 用 2/3 窗口：window=60 时就是原来的 40，不能改成 20（阈值会变）
    thr = dn.rolling(window, min_periods=max(2, int(window * 2 / 3))).quantile(q)
    return (dn > thr).astype(float).reindex(panel.index, method="ffill").fillna(0.0)
