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


def stock_daily_delta(**_ignored) -> pd.DataFrame:
    """`stock_daily` 的日更源：读 **10 交易日 dump**，返回 long 行。

    为什么不用 `prices/snapshot` 分页：快照是**盘中实时**的，收盘前跑会把半天的
    成交量当日线写进去；dump 是官方日 K。而且 10 日 dump 只有 **1.1 MB / 2 秒**，
    比逐页抓 56 页快、也不会写进半天数据。

    覆盖 **最多 10 个交易日**的缺口——比原来"落后超过 1 天就拒绝"好：
    原来一旦漏跑就只能手工导 parquet。
    """
    df = dump_frame("daily-k-10d")
    if df.empty:
        return pd.DataFrame()
    out = pd.DataFrame({
        "code": [to_raw_code(t) for t in df["thscode"].astype(str)],
        "date": df["date"].astype(str),
        "open": pd.to_numeric(df["open_price"], errors="coerce"),
        "high": pd.to_numeric(df["high_price"], errors="coerce"),
        "low": pd.to_numeric(df["low_price"], errors="coerce"),
        "close": pd.to_numeric(df["close_price"], errors="coerce"),
        # 单位已与库里逐字段对账：volume=股、turnover=成交额（元）
        "volume": pd.to_numeric(df["volume"], errors="coerce"),
        "turnover": pd.to_numeric(df["turnover"], errors="coerce"),
    })
    out = out.dropna(subset=["close"]).drop_duplicates(subset=["code", "date"], keep="last")
    print(f"  stock_daily 日更（fuyao dump 10d）：{len(out)} 行，"
          f"日期 {out['date'].min()} ~ {out['date'].max()}")
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
