"""数据源注册表：**一张表在哪取数、怎么取**。

约定
----
所有取数函数签名统一为:

    def f(start: str, end: str, table: str = "", **_) -> pd.Series | pd.DataFrame

- `start` / `end`：ISO 日期字符串，闭区间；API 不支持区间就在函数内过滤。
- `table`：目标表名（手工表、按表名读文件的函数用得上）。
- 返回值只含数据本身，不做对齐、不做 ffill、不写库——那是 `update.py` 的事。

`SOURCES` 是唯一事实来源：改数据源 = 改这里一行，不动更新逻辑。
`table` 写成 `"a|b|c"` 表示一次取数拆成多张单列表（拆表是 update 的职责）。
"""

from __future__ import annotations

import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CLEAN_DIR = Path(__file__).resolve().parent / "intake" / "clean"

# 一个指数对应多只 ETF（上交所 5xxxxx/56xxxx/51xxxx 代码）
INDEX_ETFS: dict[str, list[str]] = {
    "SSE50": ["510050", "510710", "510100", "510190"],
    "CSI300": ["510300", "510310", "510330", "159919", "159925"],
    "CSI1000": ["512100", "560010", "159845", "510580", "159968", "510510"],
    "CSI2000": ["159531", "563300", "563200", "562660"],
    "DIVIDEND": ["515080", "515180", "515890", "159589"],
    "STAR": ["589000", "589080", "589300", "589630", "589660"],
}
ALL_ETFS = sorted({c for cs in INDEX_ETFS.values() for c in cs})


@dataclass(frozen=True)
class Source:
    table: str
    func: Callable
    params: dict = field(default_factory=dict)
    kind: str = "auto"        # auto / manual
    shape: str = "auto"       # auto / long / static（决定写库方式）
    enabled: bool = True
    note: str = ""


def _ak():
    import akshare as ak
    return ak


def _clip(s: pd.Series, start: str, end: str) -> pd.Series:
    s = s.dropna()
    return s.loc[(s.index >= pd.Timestamp(start)) & (s.index <= pd.Timestamp(end))]


# ---------------------------------------------------------------- 宏观（FRED）
def fred(series_id: str, start: str, end: str, **_) -> pd.Series:
    """FRED 单序列。走代理（国内直连不稳）。"""
    import os

    import pandas_datareader.data as web

    os.environ.setdefault("HTTP_PROXY", "http://127.0.0.1:7890")
    os.environ.setdefault("HTTPS_PROXY", "http://127.0.0.1:7890")
    df = web.DataReader(series_id, "fred", start=start, end=end)
    return _clip(df[series_id].astype(float), start, end)


# ---------------------------------------------------------------- 指数 / 商品
def index_tx(symbol: str, start: str, end: str, **_) -> pd.DataFrame:
    """腾讯指数日线 → OHLCV。"""
    d = _ak().stock_zh_index_daily_tx(symbol=symbol)
    d["date"] = pd.to_datetime(d["date"])
    d = d.set_index("date").sort_index()
    out = pd.DataFrame({
        "open": d.get("open"), "high": d.get("high"),
        "low": d.get("low"), "close": d.get("close"),
        "volume": d.get("amount", d.get("volume")),
    })
    return out.loc[start:end].dropna(how="all")


def index_sina(symbol: str, start: str, end: str, **_) -> pd.Series:
    d = _ak().stock_zh_index_daily(symbol=symbol)
    d["date"] = pd.to_datetime(d["date"])
    return _clip(d.set_index("date")["close"].astype(float), start, end)


def fut_daily(symbol: str, start: str, end: str, **_) -> pd.DataFrame:
    """上期所主力连续（沪金 OHLCV）。"""
    d = _ak().futures_main_sina(symbol=symbol)
    d["日期"] = pd.to_datetime(d["日期"])
    d = d.set_index("日期").sort_index()
    ren = {"开盘价": "open", "最高价": "high", "最低价": "low",
           "收盘价": "close", "成交量": "volume"}
    out = pd.DataFrame({k: pd.to_numeric(d.get(v), errors="coerce") for v, k in ren.items()})
    return out.loc[start:end].dropna(how="all")


def fut_hold(symbol: str, start: str, end: str, **_) -> pd.Series:
    d = _ak().futures_main_sina(symbol=symbol)
    d["日期"] = pd.to_datetime(d["日期"])
    return _clip(pd.to_numeric(d.set_index("日期")["持仓量"], errors="coerce"), start, end)


# ---------------------------------------------------------------- 利率 / 杠杆
def shibor3m(start: str, end: str, **_) -> pd.Series:
    d = _ak().macro_china_shibor_all()
    col = next((c for c in d.columns if "3M" in str(c) and "定价" in str(c)), None) or \
          next((c for c in d.columns if "3M" in str(c)), None)
    d["日期"] = pd.to_datetime(d["日期"])
    return _clip(pd.to_numeric(d.set_index("日期")[col], errors="coerce"), start, end)


def margin_total(start: str, end: str, **_) -> pd.Series:
    """沪深两融余额合计（亿元）。接口按年取，所以逐年循环。"""
    ak = _ak()
    out = []
    for y in range(pd.Timestamp(start).year, pd.Timestamp(end).year + 1):
        try:
            out.append(ak.stock_margin_sse(start_date=f"{y}0101", end_date=f"{y}1231"))
        except Exception:  # noqa: BLE001
            continue
    if not out:
        raise RuntimeError("两融接口全部失败")
    d = pd.concat(out)
    d["信用交易日期"] = pd.to_datetime(d["信用交易日期"], format="%Y%m%d", errors="coerce")
    s = pd.to_numeric(d["融资余额"], errors="coerce") / 1e8
    return _clip(s.groupby(d["信用交易日期"]).sum().sort_index(), start, end)


# ---------------------------------------------------------------- 宏观（统计局）
def _cn_month(d: pd.DataFrame, col: str) -> pd.Series:
    idx = pd.to_datetime(d["月份"].astype(str).str.replace("年", "-", regex=False)
                         .str.replace("月份", "", regex=False).str.replace("月", "", regex=False))
    # 月份标签是月初（2026-09-01），统一归到月末，与库里的日期约定一致
    idx = pd.DatetimeIndex(idx) + pd.offsets.MonthEnd(0)
    s = pd.Series(pd.to_numeric(d[col], errors="coerce").to_numpy(), index=idx)
    return s.dropna().sort_index()


def cn_cpi(start: str, end: str, **_) -> pd.Series:
    return _clip(_cn_month(_ak().macro_china_cpi(), "全国-同比增长"), start, end)


def cn_ppi(start: str, end: str, **_) -> pd.Series:
    return _clip(_cn_month(_ak().macro_china_ppi(), "当月同比增长"), start, end)


def cn_pmi(start: str, end: str, **_) -> pd.Series:
    return _clip(_cn_month(_ak().macro_china_pmi(), "制造业-指数"), start, end)


def cn_retail(start: str, end: str, **_) -> pd.Series:
    """社零累计同比（旧口径，保留对照）。"""
    return _clip(_cn_month(_ak().macro_china_consumer_goods_retail(), "累计-同比增长"), start, end)


def cn_retail_mom(start: str, end: str, **_) -> pd.Series:
    """社零**当月同比**。位置/边际都用它——累计同比带季节性。"""
    return _clip(_cn_month(_ak().macro_china_consumer_goods_retail(), "同比增长"), start, end)


def cn_money(start: str, end: str, **_) -> pd.DataFrame:
    d = _ak().macro_china_money_supply()
    idx = pd.to_datetime(d["月份"].astype(str).str.replace("年", "-", regex=False)
                         .str.replace("月份", "", regex=False).str.replace("月", "", regex=False))

    def pick(key, kw):
        c = next((x for x in d.columns if key in str(x) and kw in str(x)), None)
        return pd.to_numeric(d[c], errors="coerce").to_numpy() if c else np.nan

    out = pd.DataFrame({"m1_yoy": pick("M1", "同比"), "m2_yoy": pick("M2", "同比"),
                        "m1_level": pick("M1", "数量"), "m2_level": pick("M2", "数量")},
                       index=idx).dropna(how="all").sort_index()
    return out.loc[start:end]


def _econ_calendar(d: pd.DataFrame, start: str, end: str) -> pd.Series:
    idx = pd.to_datetime(d["日期"], errors="coerce")
    s = pd.Series(pd.to_numeric(d["今值"], errors="coerce").to_numpy(), index=idx)
    s = s.dropna().sort_index()
    return _clip(s[~s.index.duplicated(keep="last")], start, end)


def cn_industrial(start: str, end: str, **_) -> pd.Series:
    return _econ_calendar(_ak().macro_china_industrial_production_yoy(), start, end)


def cn_exports(start: str, end: str, **_) -> pd.Series:
    return _econ_calendar(_ak().macro_china_exports_yoy(), start, end)


def stock_market_cap(start: str, end: str, **_) -> pd.DataFrame:
    d = _ak().macro_china_stock_market_cap()
    raw = (d[d.columns[0]].astype(str).str.replace("年", "-", regex=False)
           .str.replace("月份", "", regex=False).str.replace("月", "", regex=False))
    idx = pd.to_datetime(raw, format="%Y-%m", errors="coerce")
    d = d.loc[idx.notna()].copy()
    d.index = idx[idx.notna()]
    d = d.sort_index()

    def total(prefix):
        cols = [c for c in d.columns if str(c).startswith(prefix)]
        return d[cols].apply(pd.to_numeric, errors="coerce").sum(axis=1, min_count=1)
    out = pd.DataFrame({"total_shares": total("发行总股本"),
                        "total_mktcap": total("市价总值")})
    return out.loc[start:end].dropna(how="all")


# ---------------------------------------------------------------- 外部 JSON API
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


# ---------------------------------------------------------------- ETF / 分红
_lock = threading.Lock()


def _existing_dates(table: str) -> set[str]:
    try:
        import datahub.fin as db
        if table not in db.tables():
            return set()
        return {str(d.date()) for d in db.read(table).index}
    except Exception:  # noqa: BLE001
        return set()


def _fetch_etf_day(date: str, tries: int = 4) -> list[dict]:
    import akshare as ak

    for i in range(tries):
        try:
            df = ak.fund_etf_scale_sse(date=date)
            sub = df[df["基金代码"].isin(ALL_ETFS)]
            ts = pd.Timestamp(f"{date[:4]}-{date[4:6]}-{date[6:]}")
            return [{"date": ts, "code": str(r["基金代码"]).zfill(6),
                     "shares": float(r["基金份额"])} for _, r in sub.iterrows()]
        except Exception:  # noqa: BLE001
            time.sleep(1.5 * (i + 1) + random.random())
    return []


def etf_shares(start: str, end: str, weeks: int | None = 8,
               workers: int = 6, table: str = "etf_shares", **_) -> pd.DataFrame:
    """ETF 份额宽表（列名 c+代码）。

    **以库里已有的日期为缓存**：只补缺的交易日，不依赖本地 CSV。
    默认只看最近 `weeks` 周（接口每次约 8 秒，全历史要几小时）。
    """
    import datahub.fin as db

    begin = max(pd.Timestamp(start),
                pd.Timestamp(end) - pd.Timedelta(weeks=weeks) if weeks else pd.Timestamp(start))
    days = pd.bdate_range(start=begin, end=end)
    have = _existing_dates(table)
    todo = [d for d in days if str(d.date()) not in have]

    rows: list[dict] = []

    def work(d):
        got = _fetch_etf_day(d.strftime("%Y%m%d"))
        with _lock:
            rows.extend(got)

    if todo:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(work, todo))

    wide = (pd.DataFrame(rows).pivot_table(index="date", columns="code",
                                           values="shares", aggfunc="last").sort_index()
            if rows else pd.DataFrame())
    try:                                    # 带上库里已有的，保证写库时是完整宽表
        old = db.read(table)
        if isinstance(old, pd.DataFrame) and len(old):
            old = old.rename(columns=lambda c: str(c)[1:] if str(c).startswith("c") else str(c))
            wide = pd.concat([old, wide]).sort_index()
            wide = wide[~wide.index.duplicated(keep="last")]
    except Exception:  # noqa: BLE001
        pass
    if wide.empty:
        raise RuntimeError("ETF 份额没有取到任何数据")
    wide.columns = [f"c{c}" for c in wide.columns]
    return wide.loc[start:end]


def dividend_annual(start: str, end: str, years: int = 6,
                    table: str = "dividend_annual", **_) -> pd.DataFrame:
    """全市场年度分红：分红总额 / 净利润 / 分红率 / 家数。**以库中已有年份为缓存。**"""
    import datahub.fin as db

    got: dict[int, dict] = {}
    try:
        old = db.read(table)
        if isinstance(old, pd.DataFrame) and len(old):
            for d, r in old.iterrows():
                got[int(d.year)] = r.to_dict()
    except Exception:  # noqa: BLE001
        pass

    stop = pd.Timestamp(end)
    for y in range(max(2000, stop.year - years + 1), stop.year + 1):
        if y in got:
            continue
        r = _fetch_dividend_year(y)
        if r:
            got[y] = r

    if not got:
        raise RuntimeError("年度分红没有取到任何数据")
    out = pd.DataFrame.from_dict(got, orient="index").sort_index()
    out.index = pd.to_datetime([f"{y}-12-31" for y in out.index])
    return out.loc[start:end]


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype(str).str.replace(",", "", regex=False)
                         .str.replace("%", "", regex=False), errors="coerce")


def _fetch_dividend_year(year: int) -> dict | None:
    import akshare as ak

    try:
        d = ak.stock_fhps_em(date=f"{year}1231")
    except Exception:  # noqa: BLE001
        return None
    if d is None or not len(d):
        return None
    col_div = next((c for c in d.columns if "现金分红" in str(c) and "总额" in str(c)), None)
    col_np = next((c for c in d.columns if "净利润" in str(c)), None)
    if not col_div:
        return None
    div = _num(d[col_div])
    np_ = _num(d[col_np]).sum() if col_np else float("nan")
    return {"dividend_total": float(div.sum()),
            "net_profit_total": float(np_),
            "payout_ratio": float(div.sum() / np_ * 100) if np_ and np_ > 0 else float("nan"),
            "dividend_count": int(div.gt(0).sum())}


# ---------------------------------------------------------------- 手工
def manual_table(start: str, end: str, table: str = "", **_) -> pd.Series:
    """手工维护的表：`datahub/intake/clean/<table>.csv`，列固定 `date,value`。

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


def csi_all_flow(start: str, end: str, **_) -> pd.DataFrame:
    """中证全指 000985 的成交额与换手率（akshare 东财）。

    腾讯那套只给 OHLCV，没有成交额；换手率也从这里一起取。
    返回两列，由 SOURCES 拆成 csi_all_amount / csi_all_turnover 两张表。
    """
    ak = _ak()
    d = ak.index_zh_a_hist(symbol="000985", period="daily",
                           start_date=pd.Timestamp(start).strftime("%Y%m%d"),
                           end_date=pd.Timestamp(end).strftime("%Y%m%d"))
    if d is None or not len(d):
        raise RuntimeError("中证全指成交额接口无数据")
    d = d.copy()
    d["日期"] = pd.to_datetime(d["日期"])
    d = d.set_index("日期").sort_index()
    return pd.DataFrame({
        "csi_all_amount": pd.to_numeric(d["成交额"], errors="coerce") / 1e8,
        "csi_all_turnover": pd.to_numeric(d["换手率"], errors="coerce"),
    }).dropna(how="all")


def market_breadth(start: str, end: str, **_) -> pd.DataFrame:
    """全市场涨跌家数（**只能取当天快照**，历史无法回溯）。

    指数告诉你涨了多少，宽度告诉你多少股票在涨。
    返回三列，拆成 breadth_adv / breadth_dec / breadth_limitup。
    """
    ak = _ak()
    d = ak.stock_zh_a_spot_em()
    if d is None or not len(d):
        raise RuntimeError("全市场快照为空")
    pct = pd.to_numeric(d["涨跌幅"], errors="coerce").dropna()
    today = pd.Timestamp.today().normalize()
    return pd.DataFrame({
        "breadth_adv": [int((pct > 0).sum())],
        "breadth_dec": [int((pct < 0).sum())],
        "breadth_limitup": [int((pct > 9.8).sum())],
    }, index=[today])


# ---------------------------------------------------------------- 注册表
SOURCES: list[Source] = [
    # 共享宏观
    Source("vix", fred, {"series_id": "VIXCLS"}, note="FRED"),
    Source("usdcny", fred, {"series_id": "DEXCHUS"}, note="FRED"),
    Source("usd_index", fred, {"series_id": "DTWEXBGS"}, note="FRED 广义美元指数"),
    Source("fed_rate", fred, {"series_id": "DFF"}, note="FRED 联邦基金有效利率"),
    Source("real_yield_10y", fred, {"series_id": "DFII10"}, note="FRED 10 年 TIPS"),
    Source("geopolitic_risk", fred, {"series_id": "GPRC"}, enabled=False,
           note="FRED 无此序列；旧数据冻结，新数据改用 world_tension"),
    Source("world_tension", world_tension, note="World Tension Meter（2026-03 起）"),
    Source("xau_usd", xaus_gold, note="XAUS Gold API；2021-11 之前保留旧值"),

    # 价格与成交
    Source("csi_all", index_tx, {"symbol": "sh000985"}, note="腾讯指数 OHLCV"),
    Source("csi_all_amount|csi_all_turnover", csi_all_flow,
           note="中证全指成交额(亿元)/换手率(%)，akshare 东财"),
    Source("breadth_adv|breadth_dec|breadth_limitup", market_breadth,
           note="全市场涨跌家数/涨停数（仅当天快照，不回溯）"),
    Source("csi300", index_sina, {"symbol": "sh000300"}, note="新浪指数收盘"),
    Source("csi1000", index_sina, {"symbol": "sh000852"}, note="新浪指数收盘"),
    Source("gold_cny", fut_daily, {"symbol": "AU0"}, note="上期所主力连续"),
    Source("gold_open_interest", fut_hold, {"symbol": "AU0"}, note="上期所持仓量"),
    Source("gold_cot_net_long", cot_gold_net, note="CFTC Legacy：非商业净多头/未平仓量（%）"),

    # 利率 / 杠杆
    Source("shibor3m", shibor3m, note="Shibor 3M 定价"),
    Source("margin_balance", margin_total, note="沪深两融合计"),

    # 宏观
    Source("cpi_yoy", cn_cpi, note="统计局月度"),
    Source("ppi_yoy", cn_ppi, note="统计局月度"),
    Source("pmi", cn_pmi, note="统计局月度"),
    Source("m1_yoy|m2_yoy|m1_level|m2_level", cn_money, note="货币供应，一并取 4 张表"),
    Source("retail_sales", cn_retail, note="社零累计同比（旧口径）"),
    Source("retail_sales_mom", cn_retail_mom, note="社零当月同比"),
    Source("industrial_yoy", cn_industrial, enabled=False,
           note="仅财经日历接口，已停更（2025-08）"),
    Source("exports_yoy", cn_exports, enabled=False, note="同上"),
    Source("total_shares|total_mktcap", stock_market_cap, note="交易所月度统计"),
    Source("gold_cpi_surprise", us_cpi_surprise, note="FRED CPIAUCSL → 同比 − AR(1) 预期"),

    # 手工
    Source("equity_fin_monthly", manual_table, kind="manual", note="人行社融表解析后导出"),
    Source("insider_net", manual_table, kind="manual", note="内部人净增持"),

    # 需要多步采集
    Source("etf_shares", etf_shares, {"weeks": 8}, note="上交所份额；只补最近 8 周"),
    Source("dividend_annual", dividend_annual, {"years": 6}, note="年度分红，逐年补齐"),
]


# ======================================================================
# ashareapi：HTTP 客户端 + 四个取数函数
# ======================================================================
# key 只从环境变量 / 仓库根的 .env 读，绝不写进代码。
import json as _json
import os as _os
import urllib.error as _urlerr
import urllib.parse as _urlparse
import urllib.request as _urlreq

API_BASE = "https://api.ashareapi.com/v1"
_API_MIN_GAP = 0.55          # 标准版 120 次/分，留点余量
_api_last = [0.0]


def api_key() -> str:
    return _os.environ.get("ASHARE_API_KEY", "").strip()


def api_enabled() -> bool:
    return bool(api_key())


def api_get(path: str, **params) -> dict:
    """GET 一个端点，返回解析后的 JSON（失败也返回 dict，不抛）。"""
    if not api_enabled():
        return {"ok": False, "error": "没有设置 ASHARE_API_KEY"}

    gap = time.time() - _api_last[0]
    if gap < _API_MIN_GAP:
        time.sleep(_API_MIN_GAP - gap)
    _api_last[0] = time.time()

    url = f"{API_BASE}{path}" + (f"?{_urlparse.urlencode(params)}" if params else "")
    req = _urlreq.Request(url, headers={"Authorization": f"Bearer {api_key()}",
                                        "User-Agent": "datahub"})
    try:
        with _urlreq.urlopen(req, timeout=60) as r:
            return _json.loads(r.read().decode("utf-8"))
    except _urlerr.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            info = _json.loads(body)
        except Exception:  # noqa: BLE001
            info = {"raw": body[:300]}
        info.setdefault("ok", False)
        info["http"] = e.code
        return info
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def api_rows(res: dict) -> list[dict]:
    """取行数组形态的 data（行数组 / 表列表 / 嵌套 data 都兼容）。"""
    data = res.get("data")
    if isinstance(data, dict):          # /stocks：{total, returned, data: [...]}
        inner = data.get("data")
        return [x for x in inner if isinstance(x, dict)] if isinstance(inner, list) else []
    if isinstance(data, list):
        if data and isinstance(data[0], list):     # 表列表
            return data[0]
        return [x for x in data if isinstance(x, dict)]
    st = res.get("structured")
    return st if isinstance(st, list) else []


def _iso_date(s) -> str | None:
    """20251231 / 2026-12-31 → 2026-12-31；空 → None。"""
    if not s:
        return None
    s = str(s).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s[:10]


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def board_of(code: str) -> str:
    """创业板 300/301、科创板 688/689、北交所 8xx/4xx，其余主板。"""
    c = str(code)
    num = c[2:] if c[:2] in ("sh", "sz", "bj") else c
    if num.startswith(("300", "301")):
        return "创业板"
    if num.startswith(("688", "689")):
        return "科创板"
    if c.startswith("bj") or num.startswith(("8", "4")):
        return "北交所"
    return "主板"


def _exchange_of(code: str) -> str:
    c = str(code)
    return "SH" if c.startswith("sh") else "SZ" if c.startswith("sz") else "BJ"


# ---------------------------------------------------------------- 取数
def ashare_company(start: str = "", end: str = "", table: str = "", **_) -> pd.DataFrame:
    """全市场清单（一次调用拿全 ~5578 只）。"""
    res = api_get("/stocks", limit=6000)
    if not res.get("ok"):
        raise RuntimeError(f"/v1/stocks 失败：{res.get('error') or res.get('http')}")
    inner = (res.get("data") or {}).get("data") or []
    if not inner:
        raise RuntimeError("/v1/stocks 返回空")

    df = pd.DataFrame(inner)
    out = pd.DataFrame({
        "code": df["code"],
        "name": df.get("name"),
        "exchange": df["code"].map(_exchange_of),
        "board": df["code"].map(board_of),
        "list_date": df.get("listedDate"),
        "delist_date": None,
        "industry": df.get("industry"),
        "total_shares": None,      # 股本要 /v1/snapshot，由 shares 源补
        "float_shares": None,
    })
    return out


def ashare_financials(start: str = "", end: str = "", table: str = "",
                      num: int = 8, **_) -> pd.DataFrame:
    """季频财报：利润表 + 资产负债表拼成一张 long（全市场，季度跑一次）。"""
    rows = []
    for code in _all_codes():
        res = api_get("/finance", code=code, num=num)
        tables = res.get("data") if res.get("ok") else None
        if not tables or len(tables) < 2:
            continue
        inc, bal = tables[0], tables[1]
        bal_by_end = {r.get("EndDate"): r for r in bal}
        for r in inc:
            end_date = r.get("EndDate")
            if not end_date:
                continue
            b = bal_by_end.get(end_date, {})
            net = _num(r.get("NPParentCompanyOwners"))
            eq = _num(b.get("TotalShareholderEquity"))
            rows.append({
                "code": code,
                "date": _iso_date(end_date),
                "publ_date": _iso_date(r.get("InfoPublDate")),
                "revenue": _num(r.get("OperatingRevenue")),
                "net_profit": net,
                "equity": eq,
                "eps": _num(r.get("EPS")),
                "roe": (net / eq) if (net and eq) else None,
            })
    return pd.DataFrame(rows)


def ashare_dividend(start: str = "", end: str = "", table: str = "", **_) -> pd.DataFrame:
    """分红送转。⚠️ 接口给的是**每 10 股**（`dividendPlan` 写「10派280.242元」），
    入库统一除以 10 换成每股。"""
    rows = []
    for code in _all_codes():
        res = api_get("/dividend", code=code)
        if not res.get("ok"):
            continue
        for r in api_rows(res):
            ex = r.get("exDiviDate")
            if not ex:
                continue
            plan = str(r.get("dividendPlan") or "")
            cash10, bonus10, allot10 = _parse_plan(plan)
            if cash10 is None:
                cash10 = _num(r.get("cashDiviRMB"))
            if bonus10 is None:
                b10 = (_num(r.get("bonusShareRatio")) or 0) + \
                      (_num(r.get("tranAddShareRatio")) or 0)
                bonus10 = b10 or None
            rows.append({
                "code": code,
                "date": _iso_date(ex),
                "report_end": _iso_date(r.get("reportEndDate")),
                "cash": (cash10 / 10.0) if cash10 is not None else None,
                "bonus": (bonus10 / 10.0) if bonus10 is not None else None,
                "allot_ratio": (allot10 / 10.0) if allot10 is not None else None,
                "allot_price": None,
                "plan": plan or None,
            })
    return pd.DataFrame(rows)


def ashare_shares(start: str = "", end: str = "", table: str = "", **_) -> pd.DataFrame:
    """当前股本快照 → shares_history 的一行（kind=base）。

    历史变动靠以后的增发/回购/送转事件往前推，不从今天倒推（倒推会把增发漏掉）。
    """
    today = str(pd.Timestamp.today().date())
    rows = []
    for code in _all_codes():
        res = api_get("/snapshot", code=code)
        data = api_rows(res)
        if not data:
            continue
        r = data[0]
        total = _num(r.get("total_shares"))
        if not total:
            continue
        rows.append({"code": code, "date": today, "kind": "base",
                     "delta_total": None, "total_after": total,
                     "float_after": _num(r.get("float_shares"))})
    return pd.DataFrame(rows)


def _parse_plan(plan: str) -> tuple[float | None, float | None, float | None]:
    """「10派2.5元送3股转2股」→ (2.5, 5.0, None)，单位都是每 10 股。"""
    if not plan:
        return None, None, None
    cash = re.search(r"派\s*([\d.]+)", plan)
    song = re.search(r"送\s*([\d.]+)", plan)
    zhuan = re.search(r"转\s*([\d.]+)", plan)
    pei = re.search(r"配\s*([\d.]+)", plan)
    bonus = None
    if song or zhuan:
        bonus = float(song.group(1) if song else 0) + float(zhuan.group(1) if zhuan else 0)
    return (float(cash.group(1)) if cash else None,
            bonus,
            float(pei.group(1)) if pei else None)


def _all_codes() -> list[str]:
    """全市场代码（来自 company 表；表还没建就现场拉一次清单）。"""
    try:
        df = read("company")
        if len(df):
            return [str(c) for c in df["code"]]
    except Exception:  # noqa: BLE001
        pass
    res = api_get("/stocks", limit=6000)
    inner = (res.get("data") or {}).get("data") or []
    return [str(r["code"]) for r in inner if r.get("code")]

SOURCES += [
    # 全市场清单：一次调用拿全，每月刷一次足够
    Source("company", ashare_company, shape="static", note="ashareapi /v1/stocks"),
    # 当前股本快照：季度刷一次（历史变动靠事件推）
    Source("shares_history", ashare_shares, shape="long", note="ashareapi /v1/snapshot"),
    # 季频财报：报告期 + 披露日，用于算 PE/PB/ROE
    Source("financials", ashare_financials, shape="long", note="ashareapi /v1/finance"),
    # 分红送转：除权日 + 每股现金/送股/配股
    Source("dividend", ashare_dividend, shape="long", note="ashareapi /v1/dividend"),
]
