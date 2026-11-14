"""ashareapi 的数据源函数（company / financials / dividend / shares）。

每个函数都遵守 `sources.py` 的约定：`f(start, end, table, **_) -> DataFrame`，
只取数、不写库。写库由 `update.py` 按 `Source.shape` 分派。

**池子**：financials / dividend / shares 都是单只一次调用，所以只抓
`datahub.fin.pool.codes()` 里的股票（默认沪深300+中证500）。全市场一次要
5000 次调用，标准版额度撑不了几轮。
"""

from __future__ import annotations

import re
import time

import pandas as pd

from datahub.fin import ashare, pool

# 板块判定：创业板 300/301、科创板 688/689、北交所 8xx/4xx
def board_of(code: str) -> str:
    c = str(code)
    num = c[2:] if c[:2] in ("sh", "sz", "bj") else c
    if num.startswith(("300", "301")):
        return "创业板"
    if num.startswith(("688", "689")):
        return "科创板"
    if c.startswith("bj") or num.startswith(("8", "4", "9")):
        return "北交所"
    return "主板"


def _exchange(code: str) -> str:
    c = str(code)
    if c.startswith("sh"):
        return "SH"
    if c.startswith("sz"):
        return "SZ"
    return "BJ"


# ---------------------------------------------------------------- company
def company(start: str = "", end: str = "", table: str = "", **_) -> pd.DataFrame:
    """全市场清单（一次调用拿全 5578 只）。"""
    res = ashare.get("/stocks", limit=6000)
    if not res.get("ok"):
        raise RuntimeError(f"/v1/stocks 失败：{res.get('error') or res.get('http')}")
    inner = (res.get("data") or {}).get("data") or []
    if not inner:
        raise RuntimeError("/v1/stocks 返回空")

    df = pd.DataFrame(inner)
    out = pd.DataFrame({
        "code": df["code"],
        "name": df.get("name"),
        "exchange": df["code"].map(_exchange),
        "board": df["code"].map(board_of),
        "list_date": df.get("listedDate"),
        "delist_date": None,
        "industry": df.get("industry"),
    })
    # 股本要 /v1/snapshot 才有；这里留空，由 shares 源补
    out["total_shares"] = None
    out["float_shares"] = None
    return out


# ---------------------------------------------------------------- shares
def shares(start: str = "", end: str = "", table: str = "", **_) -> pd.DataFrame:
    """当前股本快照 → shares_history 的一行（kind=base）。

    历史变动靠以后的增发/回购/送转事件往前推，不从今天倒推（倒推会把增发漏掉）。
    """
    today = str(pd.Timestamp.today().date())
    rows = []
    for code in pool.codes():
        res = ashare.get("/snapshot", code=code)
        data = ashare.rows(res)
        if not data:
            continue
        r = data[0]
        total = r.get("total_shares")
        float_s = r.get("float_shares")
        if not total:
            continue
        rows.append({"code": code, "date": today, "kind": "base",
                     "delta_total": None, "total_after": float(total),
                     "float_after": float(float_s) if float_s else None})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- financials
_FIN_MAP = {
    "revenue": "OperatingRevenue",
    "net_profit": "NPParentCompanyOwners",
    "eps": "EPS",
}
_EQUITY_FIELD = "TotalShareholderEquity"


def financials(start: str = "", end: str = "", table: str = "",
               num: int = 8, **_) -> pd.DataFrame:
    """季频财报：利润表 + 资产负债表拼成一张 long。"""
    rows = []
    for code in pool.codes():
        res = ashare.get("/finance", code=code, num=num)
        tables = res.get("data") if res.get("ok") else None
        if not tables or len(tables) < 2:
            continue
        inc, bal = tables[0], tables[1]
        bal_by_end = {r.get("EndDate"): r for r in bal}
        for r in inc:
            end_date = r.get("EndDate")
            if not end_date:
                continue
            pub = r.get("InfoPublDate") or ""
            b = bal_by_end.get(end_date, {})
            equity = b.get(_EQUITY_FIELD)
            np_ = r.get(_FIN_MAP["net_profit"])
            row = {
                "code": code,
                "date": _iso(end_date),
                "publ_date": _iso(pub) if pub else None,
                "revenue": _num(r.get(_FIN_MAP["revenue"])),
                "net_profit": _num(np_),
                "equity": _num(equity),
                "eps": _num(r.get(_FIN_MAP["eps"])),
                "roe": None,
            }
            if row["net_profit"] and row["equity"]:
                row["roe"] = row["net_profit"] / row["equity"]
            rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- dividend
def dividend(start: str = "", end: str = "", table: str = "", **_) -> pd.DataFrame:
    """分红送转：除权日 + 每股现金/送股/配股。

    ⚠️ 口径：接口的 `cashDiviRMB` 是**每 10 股**派息（`dividendPlan` 写的是
    「10派280.242元」），入库前统一除以 10 换成**每股**。
    送股/转增同理：「10送3转2」→ 每股 0.3 + 0.2。
    """
    rows = []
    for code in pool.codes():
        res = ashare.get("/dividend", code=code)
        if not res.get("ok"):
            continue
        for r in ashare.rows(res):
            ex = r.get("exDiviDate")
            if not ex:
                continue
            plan = str(r.get("dividendPlan") or "")
            cash10, bonus10, allot10 = _parse_plan(plan)
            # 没有文案时退回结构化字段（同样是"每 10 股"口径）
            if cash10 is None:
                cash10 = _num(r.get("cashDiviRMB"))
            if bonus10 is None:
                b = (_num(r.get("bonusShareRatio")) or 0) + \
                    (_num(r.get("tranAddShareRatio")) or 0)
                bonus10 = b or None
            rows.append({
                "code": code,
                "date": _iso(ex),
                "report_end": _iso(r.get("reportEndDate") or ""),
                "cash": (cash10 / 10.0) if cash10 is not None else None,
                "bonus": (bonus10 / 10.0) if bonus10 is not None else None,
                "allot_ratio": (allot10 / 10.0) if allot10 is not None else None,
                "allot_price": None,
                "plan": plan or None,
            })
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


# ---------------------------------------------------------------- 小工具
def _iso(s: str) -> str | None:
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


__all__ = ["company", "shares", "financials", "dividend", "board_of"]
