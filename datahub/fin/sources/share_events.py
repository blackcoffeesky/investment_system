"""股本变动事件：**源** + 规范化规则（唯一一份）。

两条用法：

1. **源**（增量）：`shares_history_events()` —— 按证券逐只抓巨潮的变动历史，
   从库里已有的最新一天接着抓。5,658 只 × ~0.6s，8 线程约 5~7 分钟，
   所以注册成 `enabled=False`，按季度或手动跑：
   `python -m datahub.fin.update --only shares_history`
2. **首次回填**：`normalize_events()` —— 把用户提供的
   `data/股本数据/股本变动历史明细.csv`（257,357 行）转成入库行，
   由 `datahub/fin/intake/shares.py` 调用。

三条口径（**两处共用这一份**，别再各写一遍）：

- **`定期报告` 不是变动**（约 58%），必须剔掉；否则股本曲线变成一堆平线；
- **单位 ×1e4**（csv/接口是**万股**，库是**股**）；
- **代码按交易所代码段加前缀**：`920xxx`/`43|83|87|88` → bj、`6xx`/`900` → sh、
  `0|3`/`200` → sz。不能按首字符猜（`920xxx` 会被猜成 sh）。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from datahub.fin.sources.base import _existing_keys  # noqa: F401  (保留给增量调用方)

#: 不是真实变动的"原因"（季报/半年报/年报的例行快照）
NOT_EVENT: frozenset[str] = frozenset({"定期报告"})
#: 并发度：巨潮接口 8 并发没问题（抓取脚本用的也是 8）
WORKERS = 8
#: 万股 → 股
WAN = 1e4


def to_prefixed(code: str) -> str | None:
    """6 位裸码 → `sh/sz/bj` 前缀；无法判定返回 `None`。按**代码段**判，不按首字符。"""
    c = str(code).strip().zfill(6)
    if not c.isdigit() or len(c) != 6:
        return None
    if c.startswith("920") or c[0] in "48":
        return "bj" + c
    if c.startswith("6") or c.startswith("900"):
        return "sh" + c
    if c.startswith("0") or c.startswith("3") or c.startswith("200"):
        return "sz" + c
    return None


def normalize_events(raw: pd.DataFrame, *, code_col: str = "证券代码",
                     date_col: str = "变动日期", reason_col: str = "变动原因",
                     rcode_col: str = "变动原因编码", total_col: str = "总股本",
                     float_col: str = "已流通股份") -> tuple[pd.DataFrame, dict]:
    """原始明细（csv 或接口返回）→ 待入库的 event 行 + 统计。

    列名可覆盖（csv 与 akshare 返回的列名一致，但留出参数以防上游改名）。
    """
    n_all = len(raw)
    df = pd.DataFrame({
        "code": raw[code_col].map(to_prefixed),
        "date": pd.to_datetime(raw[date_col], errors="coerce").dt.strftime("%Y-%m-%d"),
        "event_type": raw[reason_col].astype(str).str.strip(),
        "event_code": raw[rcode_col].astype(str).str.strip(),
        "total_after": pd.to_numeric(raw[total_col], errors="coerce") * WAN,
        "float_after": pd.to_numeric(raw[float_col], errors="coerce") * WAN,
    })
    stats: dict = {"原始行数": n_all}
    n_bad = int(df["code"].isna().sum())
    stats["代码无法映射"] = n_bad
    if n_bad:
        # **不许静默跳过**：静默跳过和静默写错一样危险（只是方向相反）。
        # 这些是深 B 老代码段（`201xxx`），不在 A 股池里；猜个前缀写进库，
        # 将来做全市场统计会把 B 股算进去，且没人会想到要剔除。
        bad = sorted(set(raw.loc[df["code"].isna(), code_col].astype(str)))
        stats["跳过原因"] = (f"深 B 老代码段（{bad[0]} 一类，{len(bad)} 个代码），"
                          f"不在 A 股池 → 不猜前缀、不写库")
    stats["日期无法解析"] = int(df["date"].isna().sum())
    df = cast_ok(df.dropna(subset=["code", "date"]))

    is_report = df["event_type"].isin(NOT_EVENT)
    stats["定期报告（剔除）"] = int(is_report.sum())
    df = cast_ok(df[~is_report].copy())

    # 同一 (code, date) 多条（送股+转增同日分两条）→ 合并成一行，原因用 `、` 连
    df = df.sort_values(["code", "date"], kind="stable")
    dup = df.duplicated(subset=["code", "date"], keep=False)
    stats["同码同日多条"] = int(dup.sum())
    if dup.any():
        agg = (df[dup].groupby(["code", "date"], as_index=False)
                 .agg(event_type=("event_type", lambda s: "、".join(dict.fromkeys(s))),
                      event_code=("event_code", lambda s: ",".join(dict.fromkeys(s))),
                      total_after=("total_after", "last"),
                      float_after=("float_after", "last")))
        df = pd.concat([df[~dup], agg], ignore_index=True)
        df = df.sort_values(["code", "date"], kind="stable")

    # `delta_total` = 与**上一笔变动**的总股本差；第一笔没有上一笔 → NaN
    # （不用 0 硬填：那会把"上市"记成 +0，看不出是从 0 到 N）。
    prev = df.groupby("code", sort=False)["total_after"].shift(1)
    df["delta_total"] = df["total_after"] - prev
    df["kind"] = "event"

    stats["事件行"] = len(df)
    stats["涉及证券"] = int(df["code"].nunique())
    stats["日期范围"] = f"{df['date'].min()} ~ {df['date'].max()}" if len(df) else "—"
    return cast_ok(df), stats


def cast_ok(df: pd.DataFrame) -> pd.DataFrame:
    """`dropna` 之后让类型检查器闭嘴（内容不变）。"""
    return df


def _fetch_one(raw_code: str, start: str, end: str) -> pd.DataFrame:
    """抓一只的变动历史 → 规范化 event 行（失败抛给调用方计失败）。"""
    import akshare as ak

    df = ak.stock_share_change_cninfo(symbol=raw_code, start_date=start.replace("-", ""),
                                      end_date=end.replace("-", ""))
    if df is None or len(df) == 0:
        return pd.DataFrame()
    out, _ = normalize_events(df)
    return out


def shares_history_events(start: str = "1990-01-01", end: str = "",
                          *, max_codes: int = 0) -> pd.DataFrame:
    """全市场股本变动**事件**（增量：每只从库内最新一条 event 的日期接着抓）。"""
    from datahub.core.conn import connect

    if not end:
        end = pd.Timestamp.today().strftime("%Y-%m-%d")
    con = connect()
    try:
        codes = [str(r[0]) for r in con.execute("SELECT code FROM company ORDER BY code")]
        latest = dict(con.execute(
            "SELECT code, MAX(date) FROM shares_history WHERE kind='event' GROUP BY code"))
    finally:
        con.close()
    if not codes:
        raise RuntimeError("company 表为空：先跑一次 update --only company")
    if max_codes:
        codes = codes[:max_codes]

    frames: list[pd.DataFrame] = []
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {}
        for full in codes:
            raw = full[2:] if full[:2] in ("sh", "sz", "bj") else full
            futs[pool.submit(_fetch_one, raw, latest.get(full) or start, end)] = full
        for fut in as_completed(futs):
            try:
                d = fut.result()
            except Exception:  # noqa: BLE001
                failures.append(futs[fut])
                continue
            if len(d):
                frames.append(d)
    if failures:
        print(f"  shares_history：{len(failures)} 只抓取失败（前 5：{failures[:5]}）")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates(
        subset=["code", "date"], keep="last")
