"""ashareapi：HTTP 客户端 + 四个取数函数。

⚠️ 限速器（`_api_lock` / `_api_last` / `_throttle`）必须和 `api_get` 留在**同一个模块**：
两个模块各持一份时钟 = 没有限速，会直接触发上游降级（见 DESIGN §10）。

⚠️ 全库 `code` 统一带 `sh/sz/bj` 前缀；本模块产出的 code 一律带前缀。
"""

from __future__ import annotations

import json as _json
import os as _os
import threading
import time
import urllib.error as _urlerr
import urllib.parse as _urlparse
import urllib.request as _urlreq
from typing import Callable

import pandas as pd

from datahub.fin.read import read
from datahub.fin.sources.base import _max_date, _num, _with_prefix


API_BASE = "https://api.ashareapi.com/v1"


_API_MIN_GAP = 0.55


_api_lock = threading.Lock()


_api_last = [0.0]


def _throttle(gap: float) -> None:
    """全局节流，线程安全：任意两次请求的间隔 ≥ `gap` 秒。

    锁**包住整个"看时间 → 睡够 → 记时间"**，而不是只在最后赋值时加锁——
    否则多线程会同时通过闸门（`workers=6, gap=0.22` ≈ 272 次/分）。
    """
    with _api_lock:
        wait = gap - (time.time() - _api_last[0])
        if wait > 0:
            time.sleep(wait)
        _api_last[0] = time.time()


def api_key() -> str:
    return _os.environ.get("ASHARE_API_KEY", "").strip()


def api_enabled() -> bool:
    return bool(api_key())


def api_get(path: str, *, gap: float | None = None, **params) -> dict:
    """GET 一个端点，返回解析后的 JSON（失败也返回 dict，不抛）。

    `gap` 只给批量回填用（并发时压间隔）；默认走 `_API_MIN_GAP`。
    """
    if not api_enabled():
        return {"ok": False, "error": "没有设置 ASHARE_API_KEY"}

    _throttle(_API_MIN_GAP if gap is None else gap)

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


class QuotaExhausted(RuntimeError):
    """当日 heavy quota 用完。**确定性失败，重试没有意义**，直接停、明天续跑。"""


def quota_left(endpoint: str) -> int | None:
    """该端点的当日剩余额度；端点没有独立额度时返回 None。"""
    u = api_get("/usage")
    hq = (u or {}).get("heavy_quotas") or {}
    entry = hq.get(endpoint)
    if not entry:
        return None
    try:
        return int(entry.get("left"))
    except (TypeError, ValueError):
        return None


def is_quota_error(res: dict) -> bool:
    """响应是不是"额度用完"（区别于"上游临时降级"）。

    额度类：`http=402`，或正文里出现 quota / 额度。
    限流类（`temporarily_unavailable` + `retry_after_s`）**不算**——那个重试一次有意义。
    """
    if res.get("http") == 402:
        return True
    text = " ".join(str(res.get(k) or "") for k in ("reason", "error", "raw")).lower()
    return "quota" in text or "额度" in text


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
        "total_shares": None,
        "float_shares": None,
    })
    # 股本：akshare 全市场快照（总市值/流通市值 ÷ 最新价），季度刷一次。
    # 2026-11-17 与库里已有值逐只对过：中位相对误差 1.7e-11 ⇒ 同口径。
    # 拿不到就保持 None（`write_static` 只更新"返回了的列"，不会把已有股本清空）。
    try:
        from datahub.fin.sources import akshare as AK

        sh = AK.shares_from_spot()
        if not sh.empty:
            out = out.drop(columns=["total_shares", "float_shares"]).merge(
                sh, on="code", how="left")
    except Exception as exc:  # noqa: BLE001
        print(f"  company：股本（akshare）取不到，本次不更新：{type(exc).__name__}")
    return out


def _trade_calendar() -> pd.DatetimeIndex | None:
    """A 股交易日历（akshare，**免费、不占 ashareapi 额度**）。拿不到返回 None。"""
    try:
        from datahub.fin.sources import akshare as AK

        df = AK._ak().tool_trade_date_hist_sina()
        return pd.DatetimeIndex(pd.to_datetime(df["trade_date"]))
    except Exception:  # noqa: BLE001
        return None


def ashare_daily_delta(*, table: str = "stock_daily", force_date: str = "",
                       redownload: bool = False, close_after: str = "15:30",
                       on_chunk: Callable[[pd.DataFrame], None] | None = None,
                       chunk_codes: int = 0, **_) -> pd.DataFrame:
    """**全市场单日** OHLC 增量（`/v1/market-kline`）：只写 1 天，绝不区间补齐。

    为什么只写一天（2026-11-16 实测）：`/v1/market-kline` 一次 **500 credits**，
    回 5571 只。补 2500 个交易日（10 年）= **125 万 credits = 池子的 2.5 倍**，补不起；
    一天只花 500，一年 12.5 万（池子够 3.8 年）。**历史回补继续用同花顺 Fuyao 的
    parquet**——它还自带复权事件，比这个端点值钱。

    三道闸（少一道就会在某个周末白烧 500 credits）：
    1. **该不该跑**：`force_date` 为空时，要求"今天是交易日且已过 `close_after`"，
       否则直接跳过（周末/节假日/盘中 → 0 次调用）
    2. **缺口多大**：与库里最大日期比，落后 **> 1 个交易日** → 警告并跳过，
       **不自动补**（那是人工导 Fuyao 的事）
    3. **有没有新数据**：落后 0 天 → 跳过

    单位换算（wiki 明确）：端点 `volume` 是**手**、`amount` 是**元**；
    而 `stock_daily.volume` 存**股**、`turnover` 存**成交额（元）** → volume ×100。
    """
    now = pd.Timestamp.now()
    cal = _trade_calendar()
    last = _max_date(table)
    target = force_date or now.strftime("%Y-%m-%d")

    if not force_date:
        if cal is None:
            print("  stock_daily 日更：交易日历拿不到，无法判断该不该跑，跳过"
                  "（可显式传 force_date）")
            return pd.DataFrame()
        if now.normalize() not in cal:
            print(f"  stock_daily 日更：{now.date()} 不是交易日，跳过")
            return pd.DataFrame()
        hh, mm = (int(x) for x in close_after.split(":"))
        if (now.hour, now.minute) < (hh, mm):
            print(f"  stock_daily 日更：还没收盘（{now:%H:%M} < {close_after}），跳过")
            return pd.DataFrame()

    if cal is not None and last and not redownload:
        lag = int(((cal > pd.Timestamp(last)) & (cal <= pd.Timestamp(target))).sum())
        if lag == 0:
            print(f"  stock_daily 日更：库里已到 {last}，没有更新的交易日，跳过")
            return pd.DataFrame()
        if lag > 1:
            print(f"  [警告] stock_daily 停在 {last}，距 {target} 落后 {lag} 个交易日——"
                  f"**不自动补**（market-kline 一次 500 credits，补 10 年 = 池子的 2.5 倍）。"
                  f"请用同花顺 Fuyao 的 parquet 补历史，之后本源自会从那天起日更。")
            return pd.DataFrame()

    res = api_get("/market-kline", date=target)
    if is_quota_error(res) or quota_left("market-kline") == 0:
        raise QuotaExhausted(f"market-kline 额度用完（{res.get('reason') or res.get('http')}）")
    rows = [r for r in api_rows(res) if isinstance(r, dict)]
    if not rows:
        print(f"  stock_daily 日更：{target} 没有数据（非交易日或尚未发布）")
        return pd.DataFrame()

    out = pd.DataFrame({
        "code": [_with_prefix(r.get("code")) for r in rows],
        "date": str(target),
        "open": [_num(r.get("open")) for r in rows],
        "high": [_num(r.get("high")) for r in rows],
        "low": [_num(r.get("low")) for r in rows],
        "close": [_num(r.get("close")) for r in rows],
        # ⚠️ 单位：`/v1/market-kline` 的 volume **已经是股**（2026-11-16 实测：
        # 600519 回 4,177,980 股 vs 库里 Fuyao 4,177,984 股，差 4 股是两源取整）；
        # `amount` 是交易额（元）→ 直接进 `turnover`（库里这一列就是成交额）。
        # wiki 说的"volume 一律是手"适用于 `/v1/kline`，**不适用这个端点**。
        "volume": [_num(r.get("volume")) for r in rows],
        "turnover": [_num(r.get("amount")) for r in rows],
    })
    out = out.drop_duplicates(subset=["code", "date"], keep="last")
    out = out.dropna(subset=["close"])
    print(f"  stock_daily 日更：{target} 拉到 {len(out)} 只（{res.get('elapsed_ms')} ms）")
    return out


