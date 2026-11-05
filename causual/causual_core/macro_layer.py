"""按时间尺度分层的展示模块。

三层的分工
----------
| 层 | 内容 | 频率 | 是否进 α |
|---|---|---|---|
| `structure` | 制度、政策态度 | 年度/事件驱动 | 否（只有定性） |
| `supply` | 供需（股票/资金）、价值（估值、股东回报）、大股东态度 | 月/季 | 否（描述统计） |
| `signal` | VIX、汇率、Shibor、两融、国家队、放量 | 日/周 | **只有这层进 α** |

为什么分层
----------
把"PMI 变化"和"VIX 跳升"放进同一个 DAG 是范畴错误：两者的传导时间、
数据频率、样本长度、稳定性都差一个量级。混在一起算 α，等于假装它们是
可通约的"边"。

结构层为什么只有定性
--------------------
制度与政策的变化样本极少（一年几次），且不可重复，用回归估 β 没有意义。
这里只记录**判断 + 证据 + 证伪条件**，由人写进 `data/manual/structural_state.csv`。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

STRUCTURE = "structure"
SUPPLY = "supply"
SIGNAL = "signal"
TIERS: tuple[str, ...] = (STRUCTURE, SUPPLY, SIGNAL)

TIER_NAME = {STRUCTURE: "结构层", SUPPLY: "供需层", SIGNAL: "信号层"}

ROOT = Path(__file__).resolve().parents[2]
MANUAL = ROOT / "data" / "manual"
STRUCTURAL_CSV = MANUAL / "structural_state.csv"


def structural_state(path: Path | None = None, asof: pd.Timestamp | None = None) -> pd.DataFrame:
    """读结构层的定性判断（人工维护）。

    文件不存在时返回空表——卡片会显示"尚未录入"，而不是报错。
    列：date, area, judgement, evidence, falsifier, stance
    """
    p = path or STRUCTURAL_CSV
    if not p.exists():
        return pd.DataFrame(columns=["date", "area", "judgement", "evidence", "falsifier", "stance"])
    df = pd.read_csv(p, encoding="utf-8-sig")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date")
    if asof is not None:
        df = df[df["date"] <= pd.Timestamp(asof)]
    # 每个领域只留最新一条
    return df.groupby("area", sort=False).tail(1).sort_values("date", ascending=False)


KIND_NAME = {
    "cum_yoy": "累计同比",
    "ma3": "同比 MA3",
    "roll3": "3 月滚动和",
    "stock": "存量（同比定分位）",
    "pmi": "扩散指数",
}

KIND_DESC = {
    "cum_yoy": "口径值 = 官方累计同比（%）；同比列同左；分位 = 当前累计同比在过去 N 个月累计同比中的位置",
    "ma3": "口径值 = 官方当月同比的 3 月移动平均；分位 = 当前 MA3 在过去 N 个月 MA3 中的位置",
    "roll3": "口径值 = 当期 3 月滚动和；同比 = 滚动和/去年同期滚动和 − 1；分位 = 当前滚动和在 N 个月滚动和中的位置",
    "stock": "口径值 = 当月值；同比 = 当月值/去年同期 − 1；分位 = 当前同比在 N 个月同比中的位置（水平分位另列参考）",
    "pmi": "口径值 = 当月值；不算同比；分位 = 当前值在过去 N 个月中的位置；另看 MA3 方向",
}

HORIZONS = (60, 120, 180)


def structure_text(diffs: dict[int, float]) -> str:
    """把 60/120/180 三个窗口的条件均值差压成一句人话。

    交替（+−+ / −+−）说明没有清晰的传导结构，直接说"不稳定"，不要硬解释。
    """
    vals = [diffs.get(h, float("nan")) for h in HORIZONS]
    if any(not np.isfinite(v) for v in vals):
        return "—"
    s = [1 if v > 0 else (-1 if v < 0 else 0) for v in vals]
    trio = " / ".join(f"{v:+.1f}%" for v in vals)

    if s[0] == s[1] == s[2]:
        word = "持续有利" if s[0] > 0 else "持续不利"
    elif s[0] == s[1]:
        word = ("短中期有利" if s[0] > 0 else "短中期不利")
        word += "、长期转强" if s[2] > s[1] else "、长期转弱"
    elif s[1] == s[2]:
        word = ("短期有利" if s[0] > 0 else "短期不利")
        word += "、中长期改善" if s[1] > s[0] else "、中长期转弱"
    else:
        return f"{trio} → 不稳定，无清晰传导结构"
    return f"{trio} → {word}"


def _transform(s: pd.Series, kind: str, freq: str) -> pd.Series:
    """按口径把原始月度序列变成"用于分位与展示"的那条序列。"""
    if kind == "cum_yoy":
        return s                      # 已经是累计同比
    if kind == "ma3":
        return s.rolling(3, min_periods=3).mean()
    if kind == "roll3":
        return s.rolling(3, min_periods=1).sum()
    return s                          # stock / pmi：原值


def _yoy12(x: pd.Series) -> pd.Series:
    """同比：当期 / 去年同期 − 1。"""
    return x / x.shift(12) - 1.0


def corr_with_market(df: pd.DataFrame, x: pd.Series, freq: str,
                     fwd_col: str, window: int = 60) -> float:
    """**同期**相关：同一期的指标值 vs 同期收益。

    不用滞后相关，也不用"未来 h 日收益"——高自相关的序列在任何滞后阶都会
    测出相关，那测的是**序列自身的延续性（粘性）**，不是传导。同期相关至少
    是一个干净的定义：指标高的那几期，市场是涨还是跌。
    """
    if fwd_col not in df.columns:
        return float("nan")
    ret = df[fwd_col].resample(freq).sum()      # 同期收益（不是未来）
    j = pd.concat([x.rename("x"), ret.rename("r")], axis=1).dropna().tail(window)
    if len(j) < 24:
        return float("nan")
    return float(j["x"].corr(j["r"]))


def autocorr(x: pd.Series, lag: int = 1, window: int = 60) -> float:
    """AR(1)：这个因素本身有多"粘"。

    高自相关 = 当前高、未来大概率还高（像波动率）。
    这也是为什么"未来收益的条件均值差"没法当传导度量。
    """
    s = x.dropna().tail(window)
    if len(s) < 24:
        return float("nan")
    return float(s.autocorr(lag))


def _pct_rank_series(x: pd.Series, window: int) -> tuple[float, int]:
    """当前值在最近 `window` 期中的分位，返回 (分位, 实际窗口)。"""
    x = x.dropna()
    if len(x) < 13:
        return float("nan"), 0
    w = int(min(window, len(x) - 1))
    tail = x.tail(w + 1)
    return float((tail <= x.iloc[-1]).mean() * 100), w


def _cond_diff(df: pd.DataFrame, x: pd.Series, freq: str,
               fwd_col: str, h: int) -> float:
    """指标高于/低于中位数时，未来 h 个交易日累计收益的均值差。"""
    if fwd_col not in df.columns:
        return float("nan")
    fwd = df[fwd_col].rolling(h).sum().shift(-h)
    fwd_m = fwd.resample(freq).last()
    j = pd.concat([x.rename("x"), fwd_m.rename("f")], axis=1).dropna()
    if len(j) < 24:
        return float("nan")
    med = j["x"].median()
    hi = float(j.loc[j["x"] > med, "f"].mean())
    lo = float(j.loc[j["x"] <= med, "f"].mean())
    return hi - lo


def describe_series(
    df: pd.DataFrame,
    col: str,
    *,
    label: str,
    group: str = "",
    cat: str = "",
    kind: str = "stock",
    freq: str = "ME",
    fwd_col: str = "RET",
    window: int = 60,
) -> dict:
    """一个供需/价值指标的描述统计。

    **不是因果估计**：只回答"高的时候后来涨得多还是少"。

    ``kind`` 决定"口径值"到底是什么（见 `KIND_DESC`），因为四类指标的
    原始形态完全不同：累计同比、同比的 MA3、3 月滚动和、存量值。
    直接拿"当月值"比历史分位，会被单月噪声主导（6 月刺激冲高、7 月回落）。
    """
    if col not in df.columns:
        return {"label": label, "col": col, "kind": kind, "group": group, "cat": cat, "missing": True}

    s = df[col].resample(freq).last().dropna()
    if s.empty:
        return {"label": label, "col": col, "kind": kind, "group": group, "cat": cat, "missing": True}

    x = _transform(s, kind, freq).dropna()
    if x.empty:
        return {"label": label, "col": col, "kind": kind, "group": group, "cat": cat, "missing": True}

    # 分位基准：累计同比/MA3 用自身；滚动和用水平；存量用同比；PMI 用水平
    # 注意：yoy_pct 统一成**百分数**，_yoy12 返回的是比例
    if kind in ("cum_yoy", "ma3"):
        yoy_pct = x                       # 本身已是百分数
        pct, used = _pct_rank_series(x, window)
        pct_level = float("nan")
    elif kind == "roll3":
        yoy_pct = _yoy12(x) * 100.0
        pct, used = _pct_rank_series(x, window)
        pct_level = pct
    elif kind == "stock":
        yoy_pct = _yoy12(x) * 100.0
        pct, used = _pct_rank_series(yoy_pct, window)
        pct_level, _ = _pct_rank_series(x, window)
    elif kind == "ratio":
        # 比值：没有有意义的同比，分位取水平序列
        yoy_pct = pd.Series(dtype=float)
        pct, used = _pct_rank_series(x, window)
        pct_level = pct
    else:  # pmi
        yoy_pct = pd.Series(dtype=float)
        pct, used = _pct_rank_series(x, window)
        pct_level = pct

    # 分位基准：给卡片标注用。用户明确要求"是同比序列的分位还是水平值的分位"
    pct_basis = "同比" if kind in ("cum_yoy", "ma3", "stock") else "水平"

    # 期别：日频面板里这些列是 ffill 的，取"最后一次真正变化"的月份
    changed = s[s.diff().fillna(1) != 0]
    asof = changed.index[-1] if len(changed) else s.index[-1]

    diffs = {h: _cond_diff(df, x, freq, fwd_col, h) for h in HORIZONS}
    corr = corr_with_market(df, x, freq, fwd_col)
    ar1 = autocorr(x)

    out = {
        "label": label, "col": col, "kind": kind, "group": group, "cat": cat, "missing": False,
        "value": float(x.iloc[-1]),
        "yoy": (float(yoy_pct.iloc[-1])
                if len(yoy_pct) and np.isfinite(yoy_pct.iloc[-1]) else float("nan")),
        "pct": pct, "pct_level": pct_level, "pct_basis": pct_basis, "window": used,
        "diffs": diffs, "structure": structure_text(diffs),
        "corr": corr,
        "corr_label": ("—" if not np.isfinite(corr)
                       else ("正相关" if corr > 0.1 else ("负相关" if corr < -0.1 else "弱/无关"))),
        "ar1": ar1,
        "ar1_label": ("—" if not np.isfinite(ar1)
                      else ("高粘性" if ar1 > 0.8 else ("中粘性" if ar1 > 0.5 else "低粘性"))),
        "n": int(len(x)), "asof": str(asof.date()),
    }
    # PMI 的"另看 MA3 方向"：给出具体数值（MA3 较 3 个月前的差）
    if kind == "pmi":
        ma3 = x.rolling(3, min_periods=3).mean().dropna()
        if len(ma3) >= 4:
            d = float(ma3.iloc[-1] - ma3.iloc[-4])
            arrow = "↑" if d > 0.1 else ("↓" if d < -0.1 else "→")
            out["ma3_dir"] = f"{arrow} {d:+.2f}"
    return out


# ---------------------------------------------------------------------------
# 位置 × 边际（v5.28）
# ---------------------------------------------------------------------------
def _basis_series(s: pd.Series, basis: str, roll_n: int = 3) -> pd.Series:
    """按口径把原始月频序列变成"参与算分位"的那条序列。

    level  原始值
    roll3  3 期滚动和（流量类的位置）
    yoy    同比（边际：增速在变好还是变差）
    mom    环比差（扩散指数的边际）
    dma3   同比 MA3 的一阶差分（通胀/外需的趋势加速或减速）
    """
    if basis == "level":
        return s
    if basis == "roll3":
        return s.rolling(roll_n, min_periods=1).sum()
    if basis == "yoy":
        return s / s.shift(12) - 1.0
    if basis == "mom":
        return s.diff()
    if basis == "dma3":
        return (s / s.shift(12) - 1.0).rolling(3, min_periods=3).mean().diff()
    raise ValueError(f"未知口径：{basis}")


BASIS_NAME = {
    "level": "原始值", "roll3": "滚动和", "yoy": "同比",
    "mom": "环比差", "dma3": "同比MA3一阶差",
}


def combo_read(pos_pct: float, mom_pct: float, direction: str) -> str:
    """位置 × 边际 → 一句读法。

    **按方向分化**：同一个「高位 + 改善」，对正向指标是"强势仍在增强"，
    对负向指标是"压力持续加大"。不预设"高就是坏"。
    """
    if not (np.isfinite(pos_pct) and np.isfinite(mom_pct)):
        return "—"
    hi, imp = pos_pct >= 50, mom_pct >= 50
    if direction == "-":
        return {(True, True): "压力持续加大", (True, False): "压力见顶（转好）",
                (False, True): "压力开始累积", (False, False): "压力出清中"}[(hi, imp)]
    if direction == "0":
        return "方向不定，需人工判读"
    return {(True, True): "强势且仍在增强（拥挤度上升）", (True, False): "见顶信号",
            (False, True): "左侧机会", (False, False): "右侧回避"}[(hi, imp)]


def pair_view(
    df: pd.DataFrame,
    *,
    label: str,
    direction: str,
    pos_col: str,
    pos_basis: str,
    mom_col: str,
    mom_basis: str,
    freq: str = "ME",
    window: int = 60,
    roll_n: int = 3,
    note: str = "",
    group: str = "",
) -> dict:
    """位置（在自身历史什么位置）+ 边际（在变好还是变差）。

    两者都只输出**分位**，不给原始值——原始值的绝对值在这里没有可比性。
    窗口统一，否则位置分位和边际分位不可比。
    """
    out = {"label": label, "group": group, "direction": direction,
           "pos_name": BASIS_NAME.get(pos_basis, pos_basis),
           "mom_name": BASIS_NAME.get(mom_basis, mom_basis),
           "note": note, "missing": False}

    def _prep(col: str) -> pd.Series:
        if col not in df.columns:
            return pd.Series(dtype=float)
        return df[col].resample(freq).last().dropna()

    pos_raw = _prep(pos_col)
    pos = _basis_series(pos_raw, pos_basis, roll_n).dropna()
    mom_raw = _prep(mom_col) if mom_col in df.columns else pos_raw
    mom = _basis_series(mom_raw, mom_basis, roll_n).dropna()

    if len(pos) < 13:
        out["missing"] = True
        return out

    pos_pct, pos_win = _pct_rank_series(pos, window)
    mom_pct, mom_win = _pct_rank_series(mom, window) if len(mom) >= 13 else (float("nan"), 0)

    out.update({
        "pos_pct": pos_pct, "pos_window": pos_win, "pos_value": float(pos.iloc[-1]),
        "mom_pct": mom_pct, "mom_window": mom_win,
        "mom_value": float(mom.iloc[-1]) if len(mom) else float("nan"),
        "combo": combo_read(pos_pct, mom_pct, direction),
        "asof": str(pos.index[-1].date()),
    })
    return out
