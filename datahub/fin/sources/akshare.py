"""akshare 源：指数 / 期货 / 利率 / 宏观 / ETF 份额 / 年度分红。

akshare 很重、列名常变，所以只在这个文件里出现；`_ak()` 懒加载，import 本模块不会
把 akshare 拉起来（`update.py` 只 import `SOURCES`）。
"""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from datahub.fin.sources.base import (_clip, _existing_keys, _read_cached,
                                      _with_prefix)


INDEX_ETFS: dict[str, list[str]] = {
    "SSE50": ["510050", "510710", "510100", "510190"],
    "CSI300": ["510300", "510310", "510330", "159919", "159925"],
    "CSI1000": ["512100", "560010", "159845", "510580", "159968", "510510"],
    "CSI2000": ["159531", "563300", "563200", "562660"],
    "DIVIDEND": ["515080", "515180", "515890", "159589"],
    "STAR": ["589000", "589080", "589300", "589630", "589660"],
}


_lock = threading.Lock()


def _ak():
    import akshare as ak
    return ak


def _ohlcv(d: pd.DataFrame, date_col: str, start: str, end: str,
           volume_cols: tuple[str, ...] = ("volume", "成交量", "amount")) -> pd.DataFrame:
    """源表 → 标准 OHLCV（列名固定 `open/high/low/close/volume`）。

    两个源（腾讯指数、新浪期货）原来各写一遍"设索引 + 改列名 + 转数值 + 截区间"，
    列名一个英文一个中文。`volume_cols` 是按优先级试的成交量列：
    腾讯的 `amount` 才是成交额（`volume` 是手），所以调用方把 `amount` 排前面。
    """
    frame = d.copy()
    frame.index = pd.to_datetime(frame[date_col])
    frame = frame.sort_index()
    out: dict[str, object] = {}
    for name, cands in (("open", ("open", "开盘价")), ("high", ("high", "最高价")),
                        ("low", ("low", "最低价")), ("close", ("close", "收盘价")),
                        ("volume", volume_cols)):
        col = next((c for c in cands if c in frame.columns), None)
        out[name] = pd.to_numeric(frame[col], errors="coerce") if col else np.nan
    return pd.DataFrame(out).loc[start:end].dropna(how="all")


def index_tx(symbol: str, start: str, end: str, **_) -> pd.DataFrame:
    """腾讯指数日线 → OHLCV。"""
    return _ohlcv(_ak().stock_zh_index_daily_tx(symbol=symbol), "date", start, end,
                  volume_cols=("amount", "volume"))


def index_sina(symbol: str, start: str, end: str, **_) -> pd.Series:
    d = _ak().stock_zh_index_daily(symbol=symbol)
    d["date"] = pd.to_datetime(d["date"])
    return _clip(d.set_index("date")["close"].astype(float), start, end)


def fut_daily(symbol: str, start: str, end: str, **_) -> pd.DataFrame:
    """上期所主力连续（沪金 OHLCV）。"""
    return _ohlcv(_ak().futures_main_sina(symbol=symbol), "日期", start, end,
                  volume_cols=("成交量", "volume"))


def fut_hold(symbol: str, start: str, end: str, **_) -> pd.Series:
    d = _ak().futures_main_sina(symbol=symbol)
    d["日期"] = pd.to_datetime(d["日期"])
    return _clip(pd.to_numeric(d.set_index("日期")["持仓量"], errors="coerce"), start, end)


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


def _month_index(labels: pd.Series) -> pd.DatetimeIndex:
    """「2026年09月份」/「2026-09」→ DatetimeIndex（**不动日号**）。

    统计局/央行的月份标签是月初；归到月末是调用方的事（见 `_cn_month`），
    因为 `m1_*`/`m2_*` 历史行是按月初入库的，擅自改成月末会造出"同月两行"。
    """
    s = (labels.astype(str).str.replace("年", "-", regex=False)
         .str.replace("月份", "", regex=False).str.replace("月", "", regex=False))
    return pd.DatetimeIndex(pd.to_datetime(s))


def _cn_month(d: pd.DataFrame, col: str) -> pd.Series:
    # 月份标签是月初（2026-09-01），统一归到月末，与库里的日期约定一致
    idx = _month_index(d["月份"]) + pd.offsets.MonthEnd(0)
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
    idx = _month_index(d["月份"])          # 保持月初（库里 m1/m2 的历史约定）

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
    """ETF 份额（**long**：一行一个 `code / date / shares`，code 带市场前缀）。

    **以库里已有的日期为缓存**：只补缺的交易日，不依赖本地 CSV。
    默认只看最近 `weeks` 周（接口每次约 8 秒，全历史要几小时）。

    注意：akshare 的 `基金代码` 是裸 6 位（`510050`），`ALL_ETFS` 也按裸码比对，
    只在**入库前**统一加前缀（`_with_prefix`）。
    """
    import datahub.fin as fin

    begin = max(pd.Timestamp(start),
                pd.Timestamp(end) - pd.Timedelta(weeks=weeks) if weeks else pd.Timestamp(start))
    days = pd.bdate_range(start=begin, end=end)
    have = _existing_keys(table, lambda d: str(pd.Timestamp(d).date()))
    todo = [d for d in days if str(d.date()) not in have]

    rows: list[dict] = []

    def work(d):
        got = _fetch_etf_day(d.strftime("%Y%m%d"))
        with _lock:
            rows.extend(got)

    if todo:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(work, todo))

    frames: list[pd.DataFrame] = []
    old = _read_cached(table)               # 带上库里已有的，保证 upsert 后仍是完整表
    if len(old):
        frames.append(old)
    if rows:
        frames.append(pd.DataFrame(rows))
    if not frames:
        raise RuntimeError("ETF 份额没有取到任何数据")

    out = pd.concat(frames, ignore_index=True)
    out["code"] = out["code"].map(_with_prefix)
    out["date"] = pd.to_datetime(out["date"])
    out = (out.drop_duplicates(subset=["code", "date"], keep="last")
              .sort_values(["code", "date"]))
    out = out[(out["date"] >= pd.Timestamp(start)) & (out["date"] <= pd.Timestamp(end))]
    return out[["code", "date", "shares"]].reset_index(drop=True)


ALL_ETFS = sorted({c for cs in INDEX_ETFS.values() for c in cs})
