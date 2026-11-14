"""观测矩阵：诊断层到底看哪些变量。

三组划分
--------
1. **市场结果**（组 1）：收益、波动、成交额、换手率 —— 市场的"体温"
2. **资金活动代理**（组 2）：大小盘分化 —— 钱的"痕迹"
3. **近主体观测**（组 3）：两融、ETF 份额、大股东增持 —— **归 causual，本层不读**

第三组被 `EXCLUDED_TABLES` 显式挡住。不是注释提醒，是运行时会跳过的硬约束：
想拿它做诊断，必须先删掉常量，这就逼着人停下来想一次"为什么要共用输入"。

**市场宽度已摘除**（2026-11）：数据层的 `breadth_adv` / `breadth_dec` /
`breadth_limitup` 三张表被删，宽度改由 `datahub.fin.derive.breadth()` 现算。
状态页的时间轴暂不展示宽度。要恢复的话在这里加回观测列即可，
**不需要重拟合**——`config.FEATURES` 里从来没有宽度类特征。
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# ---------------------------------------------------------------- 组 3：刻意排除
# 这些表是 causual 的信号输入。diagnose 读了就会和 causual 互相印证，
# 失去交叉校验的价值。**不要在诊断里使用**。
EXCLUDED_TABLES = ("margin_balance", "etf_shares", "insider_net",
                   "equity_fin_monthly")


@dataclass(frozen=True)
class Obs:
    """一个观测列。"""
    key: str            # 面板列名
    group: int          # 1=市场结果 2=资金活动代理
    label: str          # 展示名
    sources: tuple[str, ...]   # 依赖的数据表
    unit: str = ""
    note: str = ""


OBSERVATIONS: tuple[Obs, ...] = (
    # ---- 组 1：市场结果
    Obs("ret", 1, "日收益", ("csi_all",), "%"),
    Obs("vol20", 1, "20日波动", ("csi_all",), "%"),
    Obs("amount", 1, "成交额", ("csi_all", "amount"), "亿元"),
    Obs("turnover", 1, "换手率", ("csi_all", "turnover"), "%"),
    # ---- 组 2：资金活动代理
    Obs("small_large", 2, "大小盘分化", ("csi1000", "csi300"), "%",
         note="中证1000 收益 − 沪深300 收益；正值说明钱往小票走"),
)

GROUPS = {1: "市场结果", 2: "资金活动代理", 3: "近主体观测（归 causual）"}


def panel(series: dict) -> pd.DataFrame:
    """把 {表名: Series} 拼成观测面板（索引=交易日）。"""
    close = _pick(series, "csi_all", "close")
    idx = pd.DatetimeIndex(close.index)
    out = pd.DataFrame(index=idx)
    out["close"] = close          # 时间轴画图用，不参与诊断
    out["ret"] = close.pct_change() * 100
    out["vol20"] = out["ret"].rolling(20, min_periods=10).std() * 100
    # 成交额/换手率现在和 OHLCV 同一张表（csi_all），列名 amount / turnover
    out["amount"] = _align(_pick_opt(series, "csi_all", "amount"), out.index)
    out["turnover"] = _align(_pick_opt(series, "csi_all", "turnover"), out.index)

    small = _align(_pick_opt(series, "csi1000"), out.index).pct_change() * 100
    large = _align(_pick_opt(series, "csi300"), out.index).pct_change() * 100
    out["small_large"] = small - large
    return out


def availability(series: dict) -> list[dict]:
    """每个观测列"有没有数据、覆盖到哪天"。页面用它显示缺口。"""
    p = panel(series)
    rows = []
    for obs in OBSERVATIONS:
        if set(obs.sources) & set(EXCLUDED_TABLES):
            continue                     # 理论上不会发生，双保险
        col = p.get(obs.key)
        if col is None or col.dropna().empty:
            rows.append({"key": obs.key, "label": obs.label, "group": obs.group,
                         "status": "缺", "start": "", "end": "", "n": 0,
                         "note": obs.note})
            continue
        s = col.dropna()
        rows.append({"key": obs.key, "label": obs.label, "group": obs.group,
                     "status": "有" if len(s) > 60 else f"仅 {len(s)} 天",
                     "start": str(s.index[0].date()), "end": str(s.index[-1].date()),
                     "n": len(s), "note": obs.note})
    return rows


# ---------------------------------------------------------------- 内部
def _pick(series: dict, table: str, col: str | None = None) -> pd.Series:
    s = _pick_opt(series, table, col)
    if s is None:
        raise KeyError(f"观测数据缺表：{table}")
    return s


def _pick_opt(series: dict, table: str, col: str | None = None) -> pd.Series | None:
    """按 "表名:列名" → "表名" 的顺序找；多列表默认取 close。"""
    for key in ((f"{table}:{col}",) if col else ()) + (f"{table}:close", table):
        if key in series and isinstance(series[key], pd.Series):
            return series[key]
    hits = [k for k in series if k.startswith(f"{table}:")]
    if hits:
        return series[sorted(hits)[0]]
    return None


def _align(s: pd.Series | None, idx: pd.Index) -> pd.Series:
    if s is None:
        return pd.Series(index=pd.DatetimeIndex(idx), dtype=float)
    return s.reindex(pd.DatetimeIndex(idx))
