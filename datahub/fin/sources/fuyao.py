"""Fuyao（同花顺）数据源客户端。

**一个文件一件事**：所有 fuyao HTTP 调用都走这里（`FUYAO_API_KEY` 只在这里读）。

鉴权：请求头 `X-API-Key`（2026-11-17 实测；`Authorization: Bearer` 也返回 200，
但 `code` 不是 0 —— 别用）。
响应信封：**HTTP 200 + `code` 字段**；`code=0` 才是成功，`code=4001` / HTTP 429 是限流。

三条实测踩过的坑
----------------
1. **`.env` 的 BOM**：用 `encoding="utf-8-sig"` 读，否则第一个键名前面带 `\\ufeff`。
2. **`date_ms` 是 UTC 毫秒戳**：直接 `pd.to_datetime(ms, unit="ms")` 会得到**前一天**
   （上海 00:00 = UTC 前一天 16:00）。必须 `tz_convert("Asia/Shanghai")`。
3. **单位**（2026-11-17 与 `stock_daily` 逐字段对账过，6 个字段全等）：
   `volume` = **股**、`turnover` = **成交额（元）**。
   ⚠️ `turnover` 这个名字在别处常指"换手率"，**fuyao 这里是成交额**。

**不限累计调用次数，只限流**（HTTP 429 / `code=4001`）——所以"全市场逐只抓"是
**可行**的（`adj_factor` 5,578 次、`finances` 一季 5,578 次），别被 ashareapi 那套
"额度/降级"的思路带偏。限流按 1/2/4/8 秒退避即可。

**`thscode` 必须带交易所后缀**（`600519.SH` / `000001.SZ` / `920000.BJ`），
不接受纯 6 位码；而库里统一是 `sh600519` 格式。**转换只许有一份**：
`to_thscode()` / `to_raw_code()` 就是它，别在调用处各写一遍 `code[2:]`。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterator

import pandas as pd
import requests

BASE = "https://fuyao.aicubes.cn"
ENV_FILE = Path(__file__).resolve().parents[3] / ".env"

#: dump 三种：全市场 10 年日 K / 最近 10 交易日日 K / 全历史复权因子事件
DUMP_KINDS = ("daily-k", "daily-k-10d", "adjustment-factors")

#: 交易所前缀 → fuyao 后缀（`sh` → `SH`）
_SUFFIX = {"sh": "SH", "sz": "SZ", "bj": "BJ"}


def to_thscode(code: str) -> str:
    """库里的 `sh600519` → fuyao 的 `600519.SH`。**全库唯一的转换入口**。"""
    c = str(code).strip().lower()
    if c[:2] in _SUFFIX:
        return f"{c[2:]}.{_SUFFIX[c[:2]]}"
    raise ValueError(f"不是带前缀的库内代码：{code!r}（应为 sh600519 这种）")


def to_raw_code(thscode: str) -> str:
    """fuyao 的 `600519.SH` → 库里的 `sh600519`。"""
    t = str(thscode).strip().upper()
    ticker, _, suffix = t.partition(".")
    if suffix.lower() not in _SUFFIX:
        raise ValueError(f"不认识的交易所后缀：{thscode!r}")
    return suffix.lower() + ticker


def api_key() -> str:
    """`FUYAO_API_KEY`（环境变量优先，其次 `.env`；读 `.env` 必须 `utf-8-sig`）。"""
    import os

    key = os.environ.get("FUYAO_API_KEY") or os.environ.get("Fuyao_API_KEY")
    if key:
        return key.strip()
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip().upper() == "FUYAO_API_KEY":
                return v.strip()
    raise RuntimeError("没有 FUYAO_API_KEY（环境变量或 .env）")


def api_get(path: str, *, retries: int = 4, **params: Any) -> dict[str, Any]:
    """GET 一个端点，返回**完整信封**（含 `code` / `data`）。

    限流（HTTP 429 或 `code=4001`）按 1/2/4/8 秒退避重试；其余错误直接返回信封，
    由调用方判断（**不要静默吞**）。
    """
    url = BASE + path
    last: dict[str, Any] = {}
    for i in range(retries):
        r = requests.get(url, params=params, headers={"X-API-Key": api_key()}, timeout=60)
        try:
            body = r.json()
        except ValueError:
            raise RuntimeError(f"fuyao {path} 返回的不是 JSON：{r.text[:200]}")
        if r.status_code != 429 and body.get("code") != 4001:
            return body
        last = body
        time.sleep(2 ** i)
    return last


def _rows(body: dict[str, Any]) -> list[dict[str, Any]]:
    data = body.get("data") or {}
    for key in ("item", "items", "rows"):
        if isinstance(data.get(key), list):
            return data[key]
    return []


def _ms_to_date(series: pd.Series) -> pd.Series:
    """UTC 毫秒戳 → 上海日期（`YYYY-MM-DD`）。**不这么转会整体少一天。**"""
    return (pd.to_datetime(series, unit="ms", utc=True)
            .dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d"))


def snapshot(thscodes: str | list[str] | None = None,
             limit: int = 100, offset: int = 0) -> pd.DataFrame:
    """行情快照（**实时**）。

    - 不传 `thscodes`：全市场分页（`limit`/`offset`）
    - 传 `thscodes`：按批取、不分页

    ⚠️ 这是**盘中实时**数据：在收盘前跑会把"半天"的成交量当日线写进去。
    日更用 `dump_frame("daily-k-10d")`，不要用这个。
    """
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if thscodes:
        params["thscodes"] = (thscodes if isinstance(thscodes, str)
                              else ",".join(thscodes))
    body = api_get("/api/a-share/prices/snapshot", **params)
    rows = _rows(body)
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    out["date"] = _ms_to_date(pd.Series([body["data"]["timestamp"]] * len(out)))
    return out


def iter_snapshot(page_size: int = 100) -> Iterator[pd.DataFrame]:
    """遍历全市场快照（约 56 页）。"""
    total = None
    offset = 0
    while total is None or offset < total:
        body = api_get("/api/a-share/prices/snapshot", limit=page_size, offset=offset)
        data = body.get("data") or {}
        total = int(data.get("total") or 0)
        rows = data.get("item") or []
        if not rows:
            break
        df = pd.DataFrame(rows)
        df["date"] = _ms_to_date(pd.Series([data["timestamp"]] * len(df)))
        yield df
        offset += len(rows)


def dump_url(kind: str) -> str:
    """取某个 dump 的预签名下载链接（有效期 ~1 小时，过期重新取）。"""
    if kind not in DUMP_KINDS:
        raise ValueError(f"未知 dump：{kind}；可用 {DUMP_KINDS}")
    body = api_get(f"/api/dump/market-dumps/{kind}/download-url")
    if body.get("code") != 0:
        raise RuntimeError(f"取 {kind} 下载链接失败：{body.get('message')}")
    return str(body["data"]["presigned_url"])


def dump_frame(kind: str) -> pd.DataFrame:
    """下载并读取一个 dump（parquet），`date` 已按上海时区转好。

    实测体量（2026-11-17）：`daily-k` **182.6 MB / 366 秒**（10 年全市场 1040 万行，
    一次就够，别放日更）、`daily-k-10d` **1.1 MB / 1.2 秒**（55,718 行，覆盖最近 10 个交易日）。
    """
    url = dump_url(kind)
    r = requests.get(url, timeout=300)
    if r.status_code != 200:
        raise RuntimeError(f"下载 {kind} 失败：HTTP {r.status_code}")
    import io

    df = pd.read_parquet(io.BytesIO(r.content))
    if "date_ms" in df.columns:
        df["date"] = _ms_to_date(df["date_ms"])
    return df


def indicators(thscode: str, report: str) -> dict[str, Any]:
    """单只 × 单报告期的财务指标（`abilities → indicators[index_id/value]`）。"""
    body = api_get("/api/a-share/financials/indicators", thscode=thscode, report=report)
    if body.get("code") != 0:
        raise RuntimeError(f"{thscode} {report} 指标取数失败：{body.get('message')}")
    return body.get("data") or {}


# ======================================================================
# 历史补数（historical-batch）+ 日更守卫（**只有这一份**）
# ======================================================================
def _ms(date: str | pd.Timestamp) -> int:
    """`2026-11-19` → Asia/Shanghai 的毫秒时间戳（接口只认毫秒）。"""
    return int(pd.Timestamp(str(date)[:10], tz="Asia/Shanghai").timestamp() * 1000)


def historical_batch(thscodes: list[str], start: str, end: str, *,
                     interval: str = "1d", adjust: str = "none",
                     is_index: bool = False) -> pd.DataFrame:
    """批量历史 K 线（**单次 ≤10 个代码**）→ long 行。

    个股走 `/api/a-share/prices/historical-batch`，指数走
    `/api/a-share-index/prices/historical-batch`（两个端点的返回结构一样）。

    返回列：`code / date / open / high / low / close / volume / turnover`
    （`code` 是库内的 `sh600519` 形式；`date` 已按 Asia/Shanghai 转好——
    `date_ms` 是 UTC 毫秒，不转会整体少一天）。
    """
    if len(thscodes) > 10:
        raise ValueError(f"historical-batch 单次最多 10 个代码，收到 {len(thscodes)}")
    path = ("/api/a-share-index/prices/historical-batch" if is_index
            else "/api/a-share/prices/historical-batch")
    body = api_get(path, thscodes=",".join(thscodes), interval=interval,
                   adjust=adjust, start=_ms(start), end=_ms(end))
    if body.get("code") != 0:
        raise RuntimeError(f"historical-batch 失败：{body.get('code')} {body.get('message')}")
    blocks = (body.get("data") or {}).get("item") or []
    rows: list[pd.DataFrame] = []
    for blk in blocks:
        items = blk.get("item") or []
        if not items:
            continue
        df = pd.DataFrame(items)
        df["code"] = to_raw_code(str(blk.get("thscode")))
        rows.append(df)
    if not rows:
        return pd.DataFrame(columns=["code", "date", "open", "high", "low",
                                     "close", "volume", "turnover"])
    df = pd.concat(rows, ignore_index=True)
    out = pd.DataFrame({
        "code": df["code"],
        "date": _ms_to_date(df["date_ms"]),
        "open": pd.to_numeric(df["open_price"], errors="coerce"),
        "high": pd.to_numeric(df["high_price"], errors="coerce"),
        "low": pd.to_numeric(df["low_price"], errors="coerce"),
        "close": pd.to_numeric(df["close_price"], errors="coerce"),
        "volume": pd.to_numeric(df["volume"], errors="coerce"),
        "turnover": pd.to_numeric(df["turnover"], errors="coerce"),
    })
    return out.dropna(subset=["close"]).drop_duplicates(subset=["code", "date"], keep="last")



def _is_trading_day(day: str) -> bool:
    """这天是不是交易日。拿不到日历就退回"工作日"（周六周日一定不是）。"""
    d = pd.Timestamp(str(day)[:10])
    try:
        from datahub.fin.sources.ashare import _trade_calendar

        cal = _trade_calendar()
        if cal is not None and len(cal):
            return d in pd.DatetimeIndex(cal)
    except Exception:  # noqa: BLE001
        pass
    return d.weekday() < 5


def daily_write_guard(table: str, trade_date: str, *,
                      now: pd.Timestamp | None = None) -> str | None:
    """日更守卫：**能不能把 `trade_date` 这天的数据写进 `table`**。

    返回 `None` = 可以写；返回字符串 = **跳过原因**（调用方打印后 return 空表）。

    三条判断（`stock_daily` 原来写死在函数里，现在抽成公共的一份）：

    1. `trade_date` 必须就是今天 —— 非交易日/节假日接口会回**上一个交易日**的
       `timestamp`，照写就是把旧数据当新数据灌进去；
    2. 今天必须**已收盘**（≥15:30）—— 盘中写进去是半天的量；
    3. 库里**已经有这一天或更新的日期** → 跳过（幂等，重复跑不写重）。

    ⚠️ 边界用 `>=`：库里最新 >= trade_date 就跳过。等价于"只补更新的日期"，
    所以**同一天重复跑不会写重**，也不会把更旧的 snapshot 盖到更新的行上。
    """
    from datahub.core.conn import connect

    now = now or pd.Timestamp.now(tz="Asia/Shanghai")
    if str(trade_date)[:10] != now.strftime("%Y-%m-%d"):
        return (f"{table}：接口的交易日是 {trade_date}，今天是 {now:%Y-%m-%d}"
                f"（周末/节假日）→ 不写")
    if now.hour * 60 + now.minute < 15 * 60 + 30:
        return f"{table}：现在 {now:%H:%M} 未收盘 → 不写（盘中数据是半天的量）"
    # ⚠️ **非交易日**必须拦：2026-11-21（周六）实测 —— 快照仍返回
    # `timestamp = 今天`，但 values 是**周五收盘**。只判"日期==今天"会把周五数据
    # 整份标成周六写进库（5,572 行与 11-20 逐字段相同，已删除并补此守卫）。
    if not _is_trading_day(str(trade_date)[:10]):
        return (f"{table}：{trade_date} 不是交易日（快照仍回 timestamp=今天，"
                f"但值是上一交易日的收盘）→ 不写")
    con = connect()
    try:
        latest = con.execute(f'SELECT MAX(date) FROM "{table}"').fetchone()[0]
    except Exception:  # noqa: BLE001
        latest = None
    finally:
        con.close()
    if latest and str(latest)[:10] >= str(trade_date)[:10]:
        return f"{table}：{trade_date} 已存在（最新 {latest}）→ 跳过"
    return None


def trading_days_between(start: str, end: str) -> list[str]:
    """[start, end] 之间的交易日（`YYYY-MM-DD`）。拿不到日历就退回工作日。"""
    try:
        from datahub.fin.sources.ashare import _trade_calendar

        cal = _trade_calendar()
        if cal is not None and len(cal):
            d = pd.DatetimeIndex(cal)
            sel = d[(d >= pd.Timestamp(start)) & (d <= pd.Timestamp(end))]
            return [str(x.date()) for x in sel]
    except Exception:  # noqa: BLE001
        pass
    return [str(x.date()) for x in pd.bdate_range(start, end)]


def stock_daily_backfill(*, start: str, end: str,
                         max_codes: int = 0) -> pd.DataFrame:
    """用 `historical-batch` 补 `stock_daily` 的缺口（每批 10 个代码）。

    为什么放在这里：**日更缺口不能靠 snapshot 补**——快照只有"当前"，
    错过一天就永远补不回来（2026-11-21 发现 `stock_daily` 缺 11-19 就是这么来的）。
    """
    from datahub.fin.sources.ashare import _all_codes

    codes = _all_codes()
    if max_codes:
        codes = codes[:max_codes]
    out: list[pd.DataFrame] = []
    for i in range(0, len(codes), 10):
        batch = [to_thscode(c) for c in codes[i:i + 10]]
        try:
            df = historical_batch(batch, start, end)
        except Exception as exc:  # noqa: BLE001
            print(f"  stock_daily 补数：批次 {i} 失败（{type(exc).__name__}），继续")
            continue
        if not df.empty:
            out.append(df)
        if i and i % 500 == 0:
            print(f"  stock_daily 补数：{i}/{len(codes)}")
    if not out:
        return pd.DataFrame()
    res = pd.concat(out, ignore_index=True)
    print(f"  stock_daily 补数（{start} ~ {end}）：{len(res)} 行 / "
          f"{res['code'].nunique()} 只")
    return res


def stock_daily_sync(**_ignored) -> pd.DataFrame:
    """`stock_daily` 的日更入口：**先补历史，再取最新**。

    1. 量缺口：`今天 - MAX(date)` ≥ 1 个交易日 → 用 `historical-batch` 补齐；
    2. 再取当天：`prices/snapshot` + 公共守卫（收盘后 / 交易日==今天 / 已存在则跳过）；
    3. **绝不把旧数据当新数据写**：守卫不通过就打印原因、返回空表。
    """
    from datahub.core.conn import connect

    now = pd.Timestamp.now(tz="Asia/Shanghai")
    today = now.strftime("%Y-%m-%d")
    # 收盘前，"该有的最新一天"是昨天
    target = today if now.hour * 60 + now.minute >= 15 * 60 + 30 else str(
        (pd.Timestamp(today) - pd.Timedelta(days=1)).date())

    con = connect()
    try:
        latest = con.execute("SELECT MAX(date) FROM stock_daily").fetchone()[0]
    finally:
        con.close()
    latest = str(latest)[:10] if latest else ""

    frames: list[pd.DataFrame] = []
    # ---- 缺哪些交易日：**尾巴缺口 + 中间的洞**都要找 ----
    # 只看 MAX(date) 会漏掉"中间缺一天"的情况：2026-11-21 服务器上
    # `stock_daily` 有 11-18 和 11-20、**独缺 11-19**，MAX 完全看不出来。
    window_start = str((pd.Timestamp(target) - pd.Timedelta(days=20)).date())
    recent = trading_days_between(window_start, target)
    con = connect()
    try:
        have = {str(r[0])[:10] for r in con.execute(
            "SELECT DISTINCT date FROM stock_daily WHERE date >= ?", (window_start,))}
    finally:
        con.close()
    missing = [d for d in recent if d not in have]
    if missing:
        print(f"  stock_daily 缺 {len(missing)} 个交易日（{', '.join(missing)}）"
              f"→ 用 historical-batch 补")
        gaps = stock_daily_backfill(start=missing[0], end=missing[-1])
        if not gaps.empty:
            frames.append(gaps[gaps["date"].isin(missing)])

    live = stock_daily_delta()
    if not live.empty:
        frames.append(live)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates(
        subset=["code", "date"], keep="last")

def stock_daily_delta(**_ignored) -> pd.DataFrame:
    """`stock_daily` 日更源：**全市场行情快照分页**（`prices/snapshot`）。

    为什么不用 dump：`daily-k-10d` 是 1.1 MB / 2 秒，而 snapshot 分页
    **5,583 只 / 3 页（limit=2000）/ ~2 秒**，日更只需要当天，不用搬 10 天。

    五个必须做对的地方（都是踩过的）：

    1. **交易日归属用 `data.timestamp`**，不用本机日期：它是毫秒戳（Asia/Shanghai），
       决定这行属于哪一天。**只有"今天已收盘（≥15:30）且 timestamp 的日期 == 今天"
       才写**——否则打印原因跳过（盘中写进去就是半天的量；周末跑 timestamp 是上个交易日，
       那就是重复写）。
    2. **单位**：`volume` = 股、`turnover` = **成交额（元）**（不是换手率）。
       已与 `stock_daily` 全市场逐只对过（见 DESIGN §18.5），不是抽查一只。
    3. **不复权**：snapshot 的价格是原始价，`stock_daily` 也是原始价（复权靠 `adj_factor`
       现算）。`prev_price` 同样是未复权前收，本函数**不使用**它。
    4. **分页边界**：本页条数 < limit 即到末尾；**空页要报错**（异常/全停牌不能被当成
       "到末尾"静默退出）；另有 60 页上限防死循环。
    5. dump 只作为**历史回补**手段保留（`daily-k` 10 年），**不在日更路径里**。
    """
    from zoneinfo import ZoneInfo

    now = pd.Timestamp.now(tz="Asia/Shanghai")

    pages: list[pd.DataFrame] = []
    offset, page_size, page = 0, 2000, 0
    trade_date = None
    while True:
        body = api_get("/api/a-share/prices/snapshot", limit=page_size, offset=offset)
        if body.get("code") != 0:
            raise RuntimeError(f"snapshot 取数失败：{body.get('code')} {body.get('message')}")
        data = body.get("data") or {}
        items = data.get("item") or []
        if not items:
            raise RuntimeError(
                f"snapshot 第 {page + 1} 页返回 0 条（offset={offset}）——"
                f"是上游异常还是全市场停牌？不能当成'已到末尾'")
        page += 1
        # ① 交易日：全部以第一页的 timestamp 为准（同一次请求必然同一天）
        stamp = pd.to_datetime(data["timestamp"], unit="ms", utc=True)
        day = stamp.tz_convert(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        if trade_date is None:
            trade_date = day
        elif day != trade_date:
            raise RuntimeError(f"分页途中交易日变了：{trade_date} → {day}（跨零点？）")
        pages.append(pd.DataFrame(items))
        if len(items) < page_size:
            break
        offset += page_size
        if page >= 60:
            raise RuntimeError("snapshot 超过 60 页仍未结束，疑似分页参数失效")

    df = pd.concat(pages, ignore_index=True)
    assert trade_date is not None

    reason = daily_write_guard("stock_daily", trade_date, now=now)
    if reason:
        print(f"  {reason}")
        return pd.DataFrame()

    out = pd.DataFrame({
        "code": [to_raw_code(t) for t in df["thscode"].astype(str)],
        "date": trade_date,
        "open": pd.to_numeric(df["open_price"], errors="coerce"),
        "high": pd.to_numeric(df["high_price"], errors="coerce"),
        "low": pd.to_numeric(df["low_price"], errors="coerce"),
        "close": pd.to_numeric(df["last_price"], errors="coerce"),
        # 单位：volume=股、turnover=成交额（元）
        "volume": pd.to_numeric(df["volume"], errors="coerce"),
        "turnover": pd.to_numeric(df["turnover"], errors="coerce"),
    })
    out = out.dropna(subset=["close"]).drop_duplicates(subset=["code"], keep="last")
    print(f"  stock_daily 日更（fuyao snapshot · {page} 页）：{len(out)} 只 / {trade_date}")
    return out


def finances_delta(*, table: str = "finances", reports: list[str] | None = None,
                   codes: list[str] | None = None, max_codes: int = 0,
                   on_chunk=None, **_ignored) -> pd.DataFrame:
    """`finances` 源：逐只 × 逐报告期取财务指标（EAV 行）。

    **默认 `enabled=False`，财报季手动开**：一季 5,578 只 ≈ 5,578 次调用
    （≈ 31 分钟 @3 req/s）。库里已经有 **2017-1 ~ 2026-3**（4,096,812 行，
    最新两期 5,560 / 5,572 只），所以**正常增量只需要当期**：

        python -m datahub.fin.update --only finances -r 2027-1      # 下一期

    `reports` 不传时只抓 `2026-4` 以来（下一期是年报）；已有行由
    `ON CONFLICT … DO UPDATE` 覆盖，重复跑不会写重。
    """
    from datahub.fin.sources.ashare import _all_codes

    want = reports or ["2026-4"]
    todo = list(codes) if codes else _all_codes()
    if max_codes:
        todo = todo[:max_codes]
    out: list[pd.DataFrame] = []
    for i, code in enumerate(todo, 1):
        ths = to_thscode(code)
        for report in want:
            try:
                data = indicators(ths, report)
            except Exception:  # noqa: BLE001
                continue
            ab = data.get("abilities") or []
            if not ab:
                continue
            rows = [{"thscode": data.get("thscode") or ths,
                     "report": data.get("report") or report,
                     "abilities": a.get("ability"),
                     "index_id": ind.get("index_id"),
                     "value": ind.get("value")}
                    for a in ab for ind in (a.get("indicators") or [])]
            if rows:
                out.append(pd.DataFrame(rows))
        if on_chunk is not None and i % 200 == 0 and out:
            on_chunk(pd.concat(out, ignore_index=True))
            out = []
        if i % 500 == 0:
            print(f"  finances：已取 {i}/{len(todo)} 只")
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def adj_factor_delta(*, table: str = "adj_factor", max_codes: int = 0,
                     codes: list[str] | None = None,
                     on_chunk=None, **_ignored) -> pd.DataFrame:
    """`adj_factor` 源：逐只取**除权事件流**（5,580 次调用 ≈ 30 分钟 @3 req/s）。

    **口径已逐位对过**（2026-11-17，5 只票 141 个事件）：fuyao 的
    `dividend_per_share` / `per_share_bonus` 与库里 `adj_factor.dividend` / `bonus`
    **完全相等**、日期集合也相同 ⇒ 直接落，不需要换算、不会让历史收益率偏掉。

    ⚠️ 全市场跑一次是 5,580 次调用，**不放进日更**：在 `SOURCES` 里
    `enabled=False`，用 `--backfill adj_factor --max-calls N` 分批跑。
    事件是稀疏的（一只票一年 1~2 次），一周刷一次足够。
    """
    from datahub.fin.sources.ashare import _all_codes

    todo = list(codes) if codes else _all_codes()
    if max_codes:
        todo = todo[:max_codes]
    out: list[pd.DataFrame] = []
    for i, code in enumerate(todo, 1):
        ths = to_thscode(code)
        try:
            df = adjustment_factors(ths, code)
        except Exception as exc:  # noqa: BLE001
            print(f"  adj_factor：{code} 取数失败（{type(exc).__name__}），跳过")
            continue
        if not df.empty:
            out.append(df)
        if on_chunk is not None and i % 200 == 0:
            on_chunk(pd.concat(out, ignore_index=True))
            out = []
        if i % 500 == 0:
            print(f"  adj_factor：已取 {i}/{len(todo)} 只")
    if not out:
        return pd.DataFrame()
    return pd.concat(out, ignore_index=True)


def adjustment_factors(thscode: str, raw_code: str) -> pd.DataFrame:
    """除权事件流 → `adj_factor` 的四列（`code/date/dividend/bonus`）。

    接口返回的是**原始事件**（`dividend_per_share` 每股分红、`per_share_bonus` 每股送股），
    不是现成的复权因子。2026-11-17 实测 600519.SH：2026-06-26 的
    `dividend_per_share=28.02423` 与库里 `adj_factor.dividend` **逐位相同** ⇒ 单位一致。
    """
    body = api_get("/api/a-share/corporate-actions/adjustment-factors", thscode=thscode)
    empty = pd.DataFrame(columns=["code", "date", "dividend", "bonus",
                                  "allot_ratio", "allot_price"])
    # `code=3002` = **这只票从来没有分红/送股**（实测 920011.BJ），是正常结果不是错误。
    # 早先把它当异常抛，全市场回填时几百只"失败"其实就是这个（2026-11-17）。
    if body.get("code") == 3002 or "No adjustment events" in str(body.get("message")):
        return empty
    if body.get("code") != 0:
        raise RuntimeError(f"{thscode} 除权事件取数失败：{body.get('code')} {body.get('message')}")
    rows = _rows(body)
    if not rows:
        return empty
    df = pd.DataFrame(rows)
    out = pd.DataFrame({
        "code": raw_code,
        "date": _ms_to_date(df["ex_date_ms"]),
        "dividend": pd.to_numeric(df.get("dividend_per_share"), errors="coerce"),
        "bonus": pd.to_numeric(df.get("per_share_bonus"), errors="coerce").fillna(0.0),
    })
    # ⚠️ **不要**输出 `allot_ratio` / `allot_price`：本接口只给分红与送股，
    # 不含配股。2026-11-17 第一次回填时我把这两列写死 0.0，**覆盖掉了 985 行真实配股**
    # （如 sz000001 1993-05-24 配股 0.1 股 @16 元 → 变成 0）。写路径只更新
    # "返回了的列"，所以不返回 = 原值不动。**这里一次都不能多写**。
    return out.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
