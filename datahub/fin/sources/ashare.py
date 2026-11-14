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
from datahub.fin.sources.base import _num


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


def _codes_with(table: str) -> set[str]:
    """库里某张 long 表**已经有哪些 code**（续跑用：抓过的就不再打接口）。

    只读一列，避免把整表（回填后几万行）读进内存。
    """
    try:
        import datahub.fin as fin
        if table not in fin.tables():
            return set()
        from datahub.fin.read import LONG_SCHEMAS
        first = next(iter(LONG_SCHEMAS[table]))
        df = fin.read_long(table, columns=[first])
        return set(df["code"].astype(str)) if len(df) else set()
    except Exception:  # noqa: BLE001
        return set()


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
                      batch: int = 10, periods: int = 4, max_calls: int = 100,
                      gap: float | None = None,
                      on_chunk: Callable[[pd.DataFrame], None] | None = None,
                      chunk_calls: int = 10, **_) -> pd.DataFrame:
    """季频财报（全市场）。走 `/v1/finance-batch`：**10 只/次、最多 4 期**。

    ⚠️ `net_profit` / `revenue` / `eps` 都是**年内累计**（A股口径：2026-09-30 的
    `NPParentCompanyOwners` 是 1~9 月累计）。`roe` 因此是"累计净利润 / 期末净资产"，
    不是年化 ROE。要 TTM 用接口里现成的 `NPParentCompanyOwnersTTM`（见 DESIGN §11）。

    为什么不用单只 `/v1/finance`（2026-11-14 实测）：
    - 单只 ~1.8s/次，且**速率一高上游就降级**（`reason=temporarily_unavailable`、
      `ok` 变 false 看起来像"无数据"）→ 全市场要 1~3 小时且随时被打断；
    - batch 是 1 个请求 10 只，同样 1 次/只的额度价，但请求数少 10 倍。

    ⚠️ 专业版 batch 有 `只数 × periods ≤ 40` 的乘积限制 → 10 只时最多 4 期。
    所以**新回填的 code 只有最近 4 个季度**（早期 5 只的 8 期保留，不删）。
    ⚠️ batch 有独立日额度 **100 次/日 = 1000 只/日** → `max_calls` 默认就是它。
    抓不完的靠下面这个续跑：**只抓库里还没有的 code**，第二天接着补。

    ⚠️ 降级时会返回 `ok:true` 但**少表**（实测某只只回 balance）——所以逐只校验
    `income`+`balance` 都在，缺的记失败、不写半条，避免静默缺数据。

    **分批落库**：传了 `on_chunk` 就每 `chunk_calls` 次 API 调用把**已抓到的行**
    交给它（随后清空），函数最后返回**剩余不足一批**的行。这样调用方可以先落库再继续，
    中途被杀也只丢最后一批——2026-11-14 一次 100 次调用全白抓就是没做这件事。
    不传 `on_chunk` 时行为不变（全部返回给 `update.run` 一次写）。
    """
    todo = [c for c in _all_codes() if c not in _codes_with("financials")]
    total = len(todo)
    if not todo:
        print("  financials：库里已有全部 code 的财报，跳过")
        return pd.DataFrame()
    calls = min(max_calls, -(-len(todo) // batch))
    todo = todo[:calls * batch]
    print(f"  financials：{calls} 次批量调用、{len(todo)} 只"
          f"（总缺口 {total}，日额度 {max_calls} 次）")

    rows: list[dict] = []
    failed: list[str] = []
    for k, i in enumerate(range(0, len(todo), batch), 1):
        chunk = todo[i:i + batch]
        res: dict = {}
        for attempt in range(3):
            res = api_get("/finance-batch", codes=",".join(chunk),
                          periods=periods, gap=gap)
            if res.get("ok") and res.get("reason") != "temporarily_unavailable":
                break
            wait = min(int(res.get("retry_after_s") or 60), 60)
            print(f"    上游降级（{res.get('reason')}），等 {wait}s 重试 {attempt + 1}/3")
            time.sleep(wait)
        got = {str(e.get("code")): e for e in api_rows(res) if isinstance(e, dict)}
        for code in chunk:
            d = (got.get(code) or {}).get("data") or {}
            inner = d.get("data") or {}
            inc, bal = inner.get("income"), inner.get("balance")
            if not isinstance(inc, list) or not isinstance(bal, list):
                failed.append(code)          # 降级可能只回一部分表：宁缺勿假
                continue
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
        if k % 20 == 0 or k == calls:
            print(f"    ... {min(i + batch, len(todo))}/{len(todo)} 只"
                  f"（失败 {len(failed)}）")
        if on_chunk is not None and k % chunk_calls == 0:
            on_chunk(pd.DataFrame(rows))     # 交给调用方落库，然后清空
            rows = []

    if failed:
        print(f"  financials：{len(failed)} 只本轮失败（降级/无数据），"
              f"下次运行会重试；例：{failed[:5]}")
    print(f"  financials：完成 {len(todo)} 只，本次返回剩余 {len(rows)} 行"
          f"（已落库的由 on_chunk 负责）")
    return pd.DataFrame(rows)


def ashare_shares(start: str = "", end: str = "", table: str = "",
                 batch: int = 200, **_) -> pd.DataFrame:
    """当前股本快照 → shares_history 的 kind=base 行。

    走 `/v1/snapshot-batch`：专业版 **200 只/次**，全市场 28 次（实测约 1 分钟）。
    比逐只 `/v1/snapshot` 快 200 倍，且**额度一样**（1 次/只）。

    历史变动靠以后的增发/回购/送转事件往前推，不从今天倒推
    （倒推会把增发漏掉；`/v1/events` 只给"当日"事件，拿不到历史）。
    """
    codes = _all_codes()
    today = str(pd.Timestamp.today().date())
    rows: list[dict] = []
    failed: list[str] = []
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        res = api_get("/snapshot-batch", codes=",".join(chunk))
        got = {}
        if res.get("ok"):
            for e in api_rows(res):
                if isinstance(e, dict) and e.get("code"):
                    got[str(e["code"])] = e
        for code in chunk:
            e = got.get(code)
            d = e.get("data") if isinstance(e, dict) else None
            total = _num(d.get("total_shares")) if isinstance(d, dict) else None
            if not e or not e.get("ok") or not total:
                failed.append(code)
                continue
            rows.append({"code": code, "date": today, "kind": "base",
                         "delta_total": None, "total_after": total,
                         "float_after": _num(d.get("float_shares"))})
    if failed:
        print(f"  shares_history：{len(failed)} 只失败（无快照/非股票），例：{failed[:5]}")
    print(f"  shares_history：{len(rows)}/{len(codes)} 只写入（{today}）")
    return pd.DataFrame(rows)