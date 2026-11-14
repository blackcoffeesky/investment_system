"""用得少的外部源：FRED / CFTC / 黄金与地缘 JSON / 手工 CSV。

都在这里是因为它们**各自只有一个函数**、依赖也不重叠，拆开只会多几个空模块。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from datahub.fin.sources.base import _clip


# ⚠️ 本文件在 `datahub/fin/sources/` 下：`parents[1]` 才是 `datahub/fin`
CLEAN_DIR = Path(__file__).resolve().parents[1] / "intake" / "clean"


def fred(series_id: str, start: str, end: str, **_) -> pd.Series:
    """FRED 单序列。走代理（国内直连不稳）。"""
    import os

    import pandas_datareader.data as web

    os.environ.setdefault("HTTP_PROXY", "http://127.0.0.1:7890")
    os.environ.setdefault("HTTPS_PROXY", "http://127.0.0.1:7890")
    df = web.DataReader(series_id, "fred", start=start, end=end)
    return _clip(df[series_id].astype(float), start, end)


def us_cpi_surprise(start: str, end: str, **_) -> pd.Series:
    """美国 CPI 意外指数 = 同比 − AR(1) 预期（pp）。

    做法（标准、可复现）：
    1. FRED `CPIAUCSL` 取水平值
    2. 算同比（%）
    3. 对同比做**扩展窗口 AR(1)**：用 t 期之前的所有样本拟合 y_t = a·y_{t-1} + b，
       预测 t 期；不未来数据
    4. 意外 = 实际同比 − 预测

    正值 = 通胀高于自身惯性所隐含的水平。
    """
    import numpy as np

    hist_start = str((pd.Timestamp(start) - pd.Timedelta(days=800)).date())
    cpi = fred("CPIAUCSL", start=hist_start, end=end)
    yoy = (cpi / cpi.shift(12) - 1.0) * 100.0
    yoy.index = yoy.index + pd.offsets.MonthEnd(0)
    yoy = yoy.dropna().sort_index()

    pred = {}
    for i in range(24, len(yoy)):
        hist = yoy.iloc[:i]
        x, y = hist.shift(1).dropna(), hist.iloc[1:]
        n = min(len(x), len(y))
        a, b = np.polyfit(x.to_numpy()[:n], y.to_numpy()[:n], 1)
        pred[yoy.index[i]] = a * hist.iloc[-1] + b
    sur = (yoy.loc[list(pred)] - pd.Series(pred)).dropna()
    sur.name = "gold_cpi_surprise"
    return _clip(sur, start, end)


def world_tension(start: str, end: str, index: str = "world_tension", **_) -> pd.Series:
    """World Tension Meter：地缘紧张指数（0–100）。CC BY 4.0，需注明出处。

    该站 2026-03-23 才建账本；单次最多 366 天，所以按年切段拉取。
    """
    import requests

    frames = []
    cur = pd.Timestamp(start)
    stop = pd.Timestamp(end)
    while cur <= stop:
        seg_end = min(cur + pd.Timedelta(days=365), stop)
        r = requests.get("https://worldtensionmeter.com/api/v1/history",
                         params={"index": index, "from": str(cur.date()),
                                 "to": str(seg_end.date()), "format": "json"},
                         timeout=30)
        if r.status_code == 200:
            pts = r.json().get("series") or []
            if pts:
                frames.append(pd.Series({pd.Timestamp(p["date"]): float(p["score"])
                                         for p in pts}))
        cur = seg_end + pd.Timedelta(days=1)
    if not frames:
        raise RuntimeError("wtm 没有返回任何数据")
    return _clip(pd.concat(frames).sort_index(), start, end)


def xaus_gold(start: str, end: str, **_) -> pd.Series:
    """XAUS Gold Data API：伦敦金日频收盘（USD/oz），约 5 年历史，无需 key。"""
    import requests

    r = requests.get("https://xaus.com/api/v1/history", timeout=30)
    r.raise_for_status()
    pts = r.json().get("points") or []
    if not pts:
        raise RuntimeError("xaus 返回空 points")
    s = pd.Series({pd.Timestamp(p["d"]): float(p["c"]) for p in pts}).sort_index()
    return _clip(s, start, end)


def cot_gold_net(start: str, end: str, timeout: int = 60, **_) -> pd.Series:
    """CFTC Legacy 持仓报告：黄金非商业**净多头占未平仓量（%）**，周频。

    数据来自 CFTC 的 Socrata 公开接口（无需 token，但无 token 时限流较严，
    失败会抛异常交给 update 记录）。源日期是**周二**（报告截止），
    这里对齐到当周周日，与旧口径一致。
    """
    import os

    import requests

    os.environ.setdefault("HTTP_PROXY", "http://127.0.0.1:7890")
    os.environ.setdefault("HTTPS_PROXY", "http://127.0.0.1:7890")

    # 只取需要的区间 + 只要必要列，减少被限流的概率
    since = (pd.Timestamp(start) - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
    params = {"$limit": 50000,
              "$select": "report_date_as_yyyy_mm_dd,contract_market_name,"
                         "pct_of_oi_noncomm_long_all,pct_of_oi_noncomm_short_all",
              "$where": f"contract_market_name like '%GOLD%' "
                        f"AND report_date_as_yyyy_mm_dd >= '{since}'",
              "$order": "report_date_as_yyyy_mm_dd"}
    token = os.environ.get("CFTC_APP_TOKEN")
    if token:
        params["$$app_token"] = token
    # 无 token 时 Socrata 会 403 限流，退避重试几次
    last = None
    for i in range(3):
        r = requests.get("https://publicreporting.cftc.gov/resource/6dca-aqww.json",
                         params=params, timeout=timeout)
        if r.status_code == 200:
            break
        last = r
        time.sleep(2 + 4 * i)
    if r.status_code != 200:
        raise RuntimeError(f"CFTC 接口 {r.status_code}（多半是限流，已重试 3 次）")
    r = r if last is None or r.status_code == 200 else r
    d = pd.DataFrame(r.json())
    if d.empty:
        raise RuntimeError("CFTC 返回空；接口可能改版或限流")

    names = sorted(d["contract_market_name"].dropna().unique())
    gold = d[d["contract_market_name"].str.fullmatch("GOLD", case=False, na=False)]
    if gold.empty:
        raise RuntimeError(f"CFTC 里找不到 'GOLD' 合约，现有：{names[:6]}")

    gold = (gold.drop_duplicates(subset=["report_date_as_yyyy_mm_dd"], keep="last")
                .assign(date=lambda x: pd.to_datetime(x["report_date_as_yyyy_mm_dd"]))
                .set_index("date").sort_index())
    net = (pd.to_numeric(gold["pct_of_oi_noncomm_long_all"], errors="coerce")
           - pd.to_numeric(gold["pct_of_oi_noncomm_short_all"], errors="coerce"))
    # 周二 → 当周周日
    net.index = net.index + pd.to_timedelta(6 - net.index.dayofweek, unit="D")
    net.name = "gold_cot_net_long"
    return _clip(net.dropna(), start, end)


def manual_table(start: str, end: str, table: str = "", **_) -> pd.Series:
    """手工维护的表：`datahub/fin/intake/clean/<table>.csv`，列固定 `date,value`。

    文件名 = 表名；行缺失 = 该期未更新（交给 check 判断）；`#` 开头是注释。
    """
    p = CLEAN_DIR / f"{table}.csv"
    if not p.exists():
        raise FileNotFoundError(f"没有手工文件：{p}")
    d = pd.read_csv(p, encoding="utf-8-sig", comment="#")
    if d.empty:
        return pd.Series(dtype=float)
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    d = d.dropna(subset=["date"]).set_index("date").sort_index()
    col = "value" if "value" in d.columns else d.columns[0]
    return _clip(pd.to_numeric(d[col], errors="coerce"), start, end)