"""前视不变量检查：`t` 时刻的指标值，不能因为「未来数据存在」而变化。

这是整个指标体系的地基。ETR/VR/RV 这类滚动指标**很容易**在实现里
不小心混进全样本统计（标准化的均值、分位的样本、Hurst 的路径长度……），
而这类错误**不会报错，也不会让指标看起来异常**——只会让回测好得离谱。

做法：取若干个截断日 T，把输入序列截到 T 之前，重算指标，
断言**最后一行与全量版本在 T 那天完全相同**。

    python -m experiment.state_lab.lookahead
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from experiment.state_lab import indicators as I

#: (名字, 用到的列, 构造函数) —— 构造函数接收四个价格序列
CASES: list[tuple[str, callable]] = [
    ("er_20", lambda c, h, l, o: I.er(c, 20)),
    ("er_120", lambda c, h, l, o: I.er(c, 120)),
    ("drift_snr_20", lambda c, h, l, o: I.drift_snr(c, 20)),
    ("cum_ret_20_pct", lambda c, h, l, o: I.cum_ret_pct(c, 20)),
    ("ma_pos_120_pct", lambda c, h, l, o: I.ma_pos_pct(c, 120)),
    ("drawdown_250_pct", lambda c, h, l, o: I.drawdown_pct(c, 250)),
    ("autocorr_20", lambda c, h, l, o: I.autocorr(c, 20)),
    ("vr_5_100", lambda c, h, l, o: I.variance_ratio(c, 5, 100)),
    ("hurst_250", lambda c, h, l, o: I.hurst_diag(c, 250)),
    ("rv_20_pct", lambda c, h, l, o: I.rv_pct(c, 20)),
    ("parkinson_20_pct", lambda c, h, l, o: I.parkinson_pct(h, l, 20)),
    ("gk_20_pct", lambda c, h, l, o: I.garman_klass_pct(o, h, l, c, 20)),
    ("atr_14_pct", lambda c, h, l, o: I.atr_pct(h, l, c, 14)),
    ("vol_of_vol_20", lambda c, h, l, o: I.vol_of_vol(I.rv_pct(c, 20), 20)),
    ("level_z_250", lambda c, h, l, o: I.level_z(c, 250)),
]


def check(panel: pd.DataFrame, n_dates: int = 5, seed: int = 7) -> pd.DataFrame:
    """返回逐指标的最大相对偏差（应全部 ≈ 0）。"""
    close, high, low = panel["close"], panel["high"], panel["low"]
    open_ = panel.get("open", close)
    # 取若干个足够靠后的截断日（保证窗口内有数据）
    candidates = close.dropna().index[300:]
    rng = np.random.default_rng(seed)
    picks = sorted(rng.choice(np.arange(len(candidates)), size=min(n_dates, len(candidates)),
                              replace=False))
    dates = [candidates[i] for i in picks]

    rows = []
    for name, fn in CASES:
        full = fn(close, high, low, open_)
        worst = 0.0
        for d in dates:
            part = fn(close.loc[:d], high.loc[:d], low.loc[:d], open_.loc[:d])
            a = full.loc[d]
            b = part.loc[d]
            if pd.isna(a) and pd.isna(b):
                continue
            if pd.isna(a) or pd.isna(b):
                worst = float("inf")
                break
            denom = max(abs(float(a)), 1e-9)
            worst = max(worst, abs(float(a) - float(b)) / denom)
        rows.append({"指标": name, "最大相对偏差": worst,
                     "通过": bool(worst < 1e-9)})
    return pd.DataFrame(rows)


def main() -> int:
    from experiment.state_lab.matrix import _index_panel

    panel = _index_panel()
    df = check(panel)
    print(df.to_string(index=False))
    bad = df[~df["通过"]]
    if len(bad):
        print(f"\n❌ {len(bad)} 个指标有前视：{list(bad['指标'])}")
        return 1
    print(f"\n✅ {len(df)} 个指标全部通过前视检查")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
