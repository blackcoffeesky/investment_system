"""完整性检查：用交易日历判断"该有的日期有没有缺"。

    python -m datahub.fin.check              # 列出问题
    python -m datahub.fin.check --ack vix:2026-10-20 --reason "美股休市数据源未更新"

判定逻辑
--------
1. 按 metadata 的 `freq` + `market` 生成"应该存在的日期"（交易日历 + 频率聚合）
2. 与表里实际存在的日期做差
3. 减掉 `missing` 表里**已人工确认**的缺口
4. 剩下的才报出来
5. 低频表（M/Q/Y）**按周期**比对：同一个月两行（月初 + 月末）报「同月多行」，
   算**重复**不算缺口——按天比会把月初那批误报成「历史缺口」
6. 只报不改：这里从不修改任何表

`missing` 只有一张表，不论频率；没有缺口时它是空的。
"""

from __future__ import annotations

import sys

import argparse
import sqlite3
from pathlib import Path
from typing import cast

import pandas as pd

from datahub import DB_PATH
import pandas as pd

_MARKET_ALIAS = {"CN": "SSE", "US": "NYSE"}

# 低频表按**周期**比对，不按天（天比会把"月初 + 月末"两行误判成缺口）
_PERIOD = {"M": "M", "Q": "Q", "Y": "Y"}
_DUP_LABEL = {"M": "同月", "Q": "同季", "Y": "同年"}


def trading_days(start: str, end: str, market: str = "CN") -> pd.DatetimeIndex:
    """某市场的交易日（不含周末与节假日）。

    ⚠️ **全库只有这一个 `trading_days`**（2026-11-22 合并）：
    之前 `check.py` 里有第二个同名函数返回 `list[str]`，把本函数**覆盖**掉了，
    于是 `expected_dates` 按老签名调用变成 `trading_days("CN", "2026-11-10", ...)` →
    `DateParseError` → `check.run()` 里 **17 张日频表全报「日历失败」**，
    逐日缺口检查**整体失效**（页面上却只像一条普通 issue）。

    拿不到日历（未装 `pandas_market_calendars` / 该市场没日历）→ 退回工作日。
    """
    try:
        import pandas_market_calendars as mcal

        name = _MARKET_ALIAS.get(str(market).upper(), str(market))
        cal = mcal.get_calendar(name)
        sched = cal.schedule(start_date=pd.Timestamp(start), end_date=pd.Timestamp(end))
        return pd.DatetimeIndex(sched.index).normalize()
    except Exception:  # noqa: BLE001
        return pd.bdate_range(start, end)


def expected_dates(freq: str, market: str, start: str, end: str) -> pd.DatetimeIndex:
    """按频率 + 市场推出"应该有哪些日期"。

    D：该市场每个交易日；W/M/Q/Y：每个周期最后一个交易日；
    market=NONE 时不挂交易日历，只用自然周期末。
    """
    if str(market).upper() == "NONE" and freq == "D":
        return pd.DatetimeIndex(pd.date_range(start, end, freq="D")).normalize()
    if freq == "D":
        return trading_days(start, end, market=market)
    if str(market).upper() == "NONE":
        rule = {"W": "W", "M": "ME", "Q": "QE", "Y": "YE"}[freq]
        return pd.DatetimeIndex(pd.date_range(start, end, freq=rule)).normalize()
    days = trading_days(start, end, market=market)
    if len(days) == 0:
        return days
    s = pd.Series(1, index=days)
    if freq == "W":
        return pd.DatetimeIndex(s.resample("W").apply(lambda x: x.index[-1] if len(x) else pd.NaT).dropna()).normalize()
    per = {"M": "M", "Q": "Q", "Y": "Y"}.get(freq, "M")
    return pd.DatetimeIndex(s.groupby(days.to_period(per)).apply(lambda x: x.index[-1]).dropna()).normalize()
from datahub.core.schema import DATA_DDL, EAV_TABLES


def _conn(path: Path | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(str(path or DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def missing_table(path: Path | None = None) -> pd.DataFrame:
    with _conn(path) as con:
        con.executescript(DATA_DDL)
        return pd.read_sql("SELECT * FROM missing ORDER BY name, date", con)


def ack(name: str, dates: list[str], reason: str = "", path: Path | None = None) -> int:
    """登记"这些缺口是已知的、正常的"。"""
    con = _conn(path)
    con.executescript(DATA_DDL)
    now = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")
    rows = [(name, str(pd.Timestamp(d).date()), reason, now) for d in dates]
    con.executemany("INSERT OR REPLACE INTO missing(name, date, reason, noted_at) VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()
    return len(rows)



def _eav_expected_period(today: "pd.Timestamp | None" = None) -> str:
    """最近一期**已披露完成**的报告期：上一季度（`2026-11-17` → `2026-3`）。"""
    t = today or pd.Timestamp.today()
    q = (t.month - 1) // 3 + 1          # 当前季度
    if q == 1:                          # Q1 的上一期是去年 Q4
        return f"{t.year - 1}-4"
    return f"{t.year}-{q - 1}"


def _check_eav(con, name: str, spec: dict) -> tuple[dict, list[str]]:
    """EAV 表的检查：**不 read**，直接按真实结构查。

    2026-11-17 服务器事故：`finances`（EAV）被猜成 `multi`，`check` 去 `read` 它 →
    `DatabaseError` → **validate 整条流水线被 block，卡片出不来**。
    根因是"我能写进去"和"我能读出来"被混为一谈：写入路径给 EAV 用了，
    `check` 却假设所有表都能 `read`。
    """
    pk = [str(c) for c in (spec.get("pk") or ())]
    ent = str(spec.get("entity") or "")
    per = str(spec.get("period") or "")
    info: dict = {}
    info["rows"] = int(con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
    if ent:
        info["entities"] = int(con.execute(
            f'SELECT COUNT(DISTINCT "{ent}") FROM "{name}"').fetchone()[0])
    if per:
        info["periods"] = int(con.execute(
            f'SELECT COUNT(DISTINCT "{per}") FROM "{name}"').fetchone()[0])
        info["latest"] = con.execute(f'SELECT MAX("{per}") FROM "{name}"').fetchone()[0]

    issues: list[str] = []
    if pk:
        dup = con.execute(
            f'SELECT COUNT(*) FROM (SELECT 1 FROM "{name}" GROUP BY '
            + ", ".join(f'"{c}"' for c in pk) + " HAVING COUNT(*) > 1)").fetchone()[0]
        if dup:
            issues.append(f"主键重复 {dup} 组")
    if per and info.get("latest"):
        want = _eav_expected_period()
        if str(info["latest"]) < want:
            issues.append(f"最新报告期 {info['latest']} 落后于预期 {want}")
    return info, issues


def run(path: Path | None = None, recent_days: int = 90) -> pd.DataFrame:
    """返回问题清单。

    `recent_days`：只有最近这么多天内的缺口算"问题"（多半是忘了更新）；
    更早的缺口单独计数——那是历史源数据留下的，不是运维该天天盯的。
    """
    con = _conn(path)
    meta = cast(pd.DataFrame, pd.read_sql("SELECT * FROM metadata", con))
    con.executescript(DATA_DDL)
    acked = cast(pd.DataFrame, pd.read_sql("SELECT name, date FROM missing", con))
    con.close()

    ack_map: dict[str, set[str]] = {}
    for r in acked.to_dict("records"):
        ack_map.setdefault(str(r["name"]), set()).add(str(r["date"]))

    import datahub.fin as fin

    cutoff = str((pd.Timestamp.today() - pd.Timedelta(days=recent_days)).date())
    rows = []
    for m in meta.to_dict("records"):
        name = m["name"]
        # **登记表第一优先**：`metadata.kind` 是可被写坏的缓存（服务器上就还是 multi）
        spec = EAV_TABLES.get(name)
        if spec is not None or str(m["kind"] or "") == "eav":
            if not spec:
                rows.append({"name": name, "freq": "EAV",
                             "issues": "eav 表没在 schema.EAV_TABLES 里登记"})
                continue
            con2 = _conn(path)
            try:
                info, issues = _check_eav(con2, name, spec)
            finally:
                con2.close()
            if issues:
                rows.append({"name": name, "freq": "EAV", "n": info.get("rows", ""),
                             "issues": "；".join(issues)})
            else:
                print(f"  {name}: ok（eav，{info.get('rows'):,} 行 / "
                      f"{info.get('entities', '?')} 只 / {info.get('periods', '?')} 期，"
                      f"最新 {info.get('latest')}）")
            continue
        try:
            obj = fin.read(name, path=path)
        except Exception as e:  # noqa: BLE001
            rows.append({"name": name, "issues": f"读取失败 {type(e).__name__}"})
            continue
        if len(obj) == 0:
            rows.append({"name": name, "issues": "空表"})
            continue

        if str(m["kind"] or "single") == "long":
            # long 表一行一个 (标的, 日期)：只关心"有哪些交易日"
            dates = pd.DatetimeIndex(pd.to_datetime(obj["date"]).unique())
        elif str(m["kind"] or "single") == "static":
            # static 没有日期轴（`obj.index` 只是 RangeIndex），
            # 拿它转日期会得到 1970-01-01 从而报出「同月多行 5578 行」这种假问题。
            # 它本来就不参与缺口检查（行数由 refresh_metadata 保证）。
            continue
        else:
            dates = pd.DatetimeIndex(obj.index)

        if str(m["freq"] or "").upper() == "SPARSE":
            # 稀疏表（事件表、份额快照）：只要求不是很久没更新，不查逐日缺口
            last = str(dates[-1].date()) if len(dates) else ""
            if last < cutoff:
                rows.append({"name": name, "freq": "SPARSE",
                             "n": len(obj), "issues": f"最近一条停在 {last or '无'}"})
            continue

        actual = set(str(d.date()) for d in dates)
        start, end = min(actual), max(actual)
        freq = str(m["freq"] or "D")
        market = str(m["market"] or "CN")
        try:
            exp = expected_dates(freq, market, start, end)
        except Exception as e:  # noqa: BLE001
            # **检查本身没跑成**，不是"数据有问题"：标成 check_failed，
            # 调用方要能分清（2026-11-22：17 张日频表全报「日历失败」，页面上
            # 却只像一条普通 issue，真实缺口一条也检不出来）。
            print(f"[check] {name}：日历失败（检查没跑成）{type(e).__name__}: {e}",
                  file=sys.stderr)
            rows.append({"name": name, "freq": str(m.get("freq") or ""),
                         "issues": f"日历失败 {type(e).__name__}: {e}",
                         "status": "check_failed"})
            continue

        issues: list[str] = []
        dup = pd.Series(dtype="int64")
        per = _PERIOD.get(freq.upper())          # M/Q/Y → "M"/"Q"/"Y"；D/W → None
        if per:
            # 低频表按**周期**比对：同一个月里"月初一行 + 月末一行"是**重复**，不是缺口。
            # 按天比会把月初那批算成缺口（m1/m2/total_* 的「53 天缺口」就是这么误报的）。
            actual_per = pd.DatetimeIndex(sorted(dates)).to_period(per)
            exp_per = pd.DatetimeIndex(exp).to_period(per)
            acked = ack_map.get(name, set())
            ack_per = (pd.DatetimeIndex(sorted(acked)).to_period(per)
                       if acked else pd.PeriodIndex([], freq=per))
            missing_per = sorted(set(exp_per) - set(actual_per) - set(ack_per))
            missing = [str(p.end_time.date()) for p in missing_per]
            counts = pd.Series(actual_per).value_counts()
            dup = counts[counts > 1].sort_index()
        else:
            expected = set(str(d.date()) for d in exp)
            missing = sorted(expected - actual - ack_map.get(name, set()))

        recent = [d for d in missing if d >= cutoff]
        old = len(missing) - len(recent)

        if per and len(dup):
            issues.append(f"{_DUP_LABEL[per]}多行 {len(dup)} 期"
                          f"（最多 {int(dup.max())} 行，首见 {dup.index[0]}）")
        if recent:
            sample = ", ".join(recent[:5])
            more = f" … 共 {len(recent)} 天" if len(recent) > 5 else ""
            issues.append(f"近期缺 {len(recent)} 天（{sample}{more}）")
        if old:
            issues.append(f"历史缺口 {old} 天")
        if isinstance(obj, pd.Series) and obj.isna().any():
            issues.append(f"空值 {int(obj.isna().sum())}")
        if issues:
            # 手工导入表（WGC 月报/季报）的提示要**可执行**：
            # "已 48 天未更新" 只说明现象，用户还得去翻代码才知道怎么补。
            from datahub.fin.intake.registry import hint_for

            hint = hint_for(name)
            if hint:
                issues.append(hint)
            rows.append({"name": name, "freq": freq, "market": market,
                         "n": len(obj), "issues": "；".join(issues)})
    df = pd.DataFrame(rows)
    if len(df):
        # `status`：`data` = 数据真有问题；`check_failed` = 检查本身没跑成。
        # 两者都不该被静默，但排查方向完全不同——别混成一种。
        if "status" not in df.columns:
            df["status"] = "data"
        df["status"] = df["status"].fillna("data")
    return df


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ack", help="登记已知缺口：表名:日期[,日期…]")
    ap.add_argument("--reason", default="")
    a = ap.parse_args()
    if a.ack:
        name, _, dates = a.ack.partition(":")
        n = ack(name.strip(), [d.strip() for d in dates.split(",") if d.strip()], a.reason)
        print(f"已登记 {n} 条")
    df = run()
    print(f"\n问题表数 {len(df)}")
    print(df.to_string(index=False) if len(df) else "全部通过")


# ======================================================================
# 新鲜度：两个布尔量，别把它们合成一个
# ======================================================================
# 2026-11-18 从 `causual/causual_core/freshness.py` 搬回数据层：
# 那个模块自己读 `metadata` 判过期，是 causual 与数据层的耦合，又拿不到适配层。
# 语义照搬（`_period_end` 的四个分支 + 只判 D/M/Q/Y），但**多给一个布尔量**。


#: 本进程里"跳过冻结表"的通知只打一次（每次调用都打会淹掉真正的告警）
_FROZEN_ANNOUNCED = False


def _frozen_tables() -> set[str]:
    """**有意冻结**的表：`SOURCES` 里登记但 `enabled=False` 的。

    2026-11-18：`freshness` 一上线就把 `geopolitic_risk` / `industrial_yoy` /
    `exports_yoy` 报成 overdue——**技术正确、语义错误**：这三张是**有意停更**的
    （FRED 无此序列 / 财经日历接口停更），不是"忘了跑"。

    判定依据只能是 **源注册表**（`metadata` 里没有 `enabled` 列）：
    在 `SOURCES` 里且 `enabled=False` ⇒ 冻结；**不在 `SOURCES` 里的表（手工导入表、
    intake 表）一律不跳**——它们更该被盯着（`gold_etf_holdings` 就是靠这个报缺月的）。
    """
    from datahub.fin.sources import SOURCES

    all_t = {t for s in SOURCES for t in s.table.split("|")}
    active = {t for s in SOURCES if s.enabled for t in s.table.split("|")}
    return all_t - active


def _announce_frozen(frozen: set[str], skipped: list[str]) -> None:
    """**跳过要"响"一次**：否则以后有人问"为什么这三张不报了"，又得翻代码。"""
    global _FROZEN_ANNOUNCED
    if _FROZEN_ANNOUNCED or not skipped:
        return
    _FROZEN_ANNOUNCED = True
    print(f"[新鲜度] 跳过 {len(skipped)} 张已冻结的表（源 enabled=False，有意停更）："
          f"{' / '.join(sorted(skipped))}", file=sys.stderr)


def _period_end(freq: str, today: pd.Timestamp) -> pd.Timestamp | None:
    """最近一个"已结束的周期"的期末；不判过期的频率返回 None。

    语义与原 causual 实现逐条一致（搬过来时没改口径）：

    - `D` → 上一工作日（周末不判过期）
    - `M` → 上月月末
    - `Q` → 上季末
    - `Y` → 上年末
    - `W` / `SPARSE` / 其它 → `None`（"该有了"没有定义，**有意不判**）
    """
    if freq == "D":
        d = today
        while d.weekday() >= 5:
            d -= pd.Timedelta(days=1)
        return d
    if freq == "M":
        return (today.replace(day=1) - pd.Timedelta(days=1)).normalize()
    if freq == "Q":
        q_end = today.to_period("Q").start_time - pd.Timedelta(days=1)
        return pd.Timestamp(q_end).normalize()
    if freq == "Y":
        return pd.Timestamp(year=today.year - 1, month=12, day=31)
    return None


def freshness(tables=None, *, today: str | pd.Timestamp | None = None,
              path: Path | None = None) -> list[dict]:
    """每张表一行：**是否过期（stale）** 与 **是否超出宽限（overdue）**。

    两个布尔量是**不同的事**，不要合成一个：

    | stale | overdue | 含义 | 该做什么 |
    |---|---|---|---|
    | False | False | 已是最新 | 无 |
    | True  | False | **不可抗力**：源还没发布，仍在 `lag_days` 宽限内 | 等，**不用报警** |
    | True  | True  | **忘记更新**：宽限**已过**（严格大于截止日）还没到 | 报警 / 查管线 |

    原 causual 实现的写法是 `if today < expect + lag: continue` ——
    **"宽限内"那一支被直接吞掉了**，于是上层只看到"过期表"，
    分不清"源还没发"和"我们忘了跑"。这里把它显式化成 `overdue`。

    返回值每行：`table / freq / expect / actual / lag_days / stale / overdue /
    days_behind / text`。`freq` 必须带——下游（如月频卡的归档月）靠它筛
    `freq in ("M","Q")`，不要再去维护一份手写清单。

    - `tables=None` → `metadata` 里全部表
    - 表名不在 `metadata` 里 → **打印告警**（清单漂移 = 监控失效，2026-11 踩过）
    - `static` / `eav`（没有日期轴）→ 跳过
    - `today` 可指定（测试用固定日期，不依赖真实今天）
    """
    from datahub.fin.read import kind_of

    con = _conn(path)
    try:
        meta = pd.read_sql("SELECT name, freq, lag_days, end_date, kind FROM metadata", con)
    finally:
        con.close()
    if meta is None or not len(meta):
        return []
    now = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.today().normalize()
    idx = meta.set_index("name")
    names = list(tables) if tables is not None else [str(n) for n in idx.index]

    frozen = _frozen_tables()
    skipped_frozen: list[str] = []
    out: list[dict] = []
    for t in names:
        if t not in idx.index:
            print(f"[新鲜度] 监控清单里的 {t!r} 不在 metadata 里（改名/删表了？"
                  f"这张表的新鲜度没人看）", file=sys.stderr)
            continue
        # 冻结表（有意停更）不参与新鲜度：报了只会误导
        if t in frozen:
            skipped_frozen.append(t)
            continue
        # static / eav 没有日期轴：不是"过期"，是"没有期"这个维度
        if str(idx.loc[t].get("kind") or "") in ("static", "eav"):
            continue
        row = idx.loc[t]
        freq = str(row.get("freq") or "D")
        lag = int(row.get("lag_days") or 0)
        expect = _period_end(freq, now)
        if expect is None:                      # W / SPARSE：有意不判
            continue
        actual_s = row.get("end_date")
        if not actual_s:
            continue
        actual = pd.Timestamp(str(actual_s))
        stale = bool(actual < expect)
        # 宽限：**严格大于**才算"该到了"。
        # `expect + lag` 那天是**发布截止日**——数据可能当天收盘后才进库，
        # 把截止日当天算 overdue，会在"数据其实已经到了、只是还没抓"时误报；
        # 多宽限一天更稳（2026-11-18 用户定的）。
        overdue = bool(stale and now > expect + pd.Timedelta(days=lag))
        behind = max(0, (expect - actual).days)
        if not stale:
            text = ""
        elif overdue:
            text = (f"{t} 停在 {actual.date()}，期望至少 {expect.date()}"
                    f"（已超宽限 {lag} 天）")
        else:
            text = (f"{t} 停在 {actual.date()}，期望至少 {expect.date()}"
                    f"（源可能还没发布，在 {lag} 天宽限内）")
        out.append({
            "table": t, "freq": freq, "expect": str(expect.date()),
            "actual": str(actual.date()), "lag_days": lag,
            "stale": stale, "overdue": overdue,
            "days_behind": behind, "text": text,
        })
    _announce_frozen(frozen, skipped_frozen)
    return out

# ======================================================================
# 日更守卫 + 缺口检查（**源无关**，2026-11-22 从 sources/fuyao.py 搬来）
# ======================================================================
# 为什么放这里而不是某个源里：这两件事不是 Fuyao 特有的，是**任何数据源都该有的**。
# 长在源里，第二个源要用就变成第二份副本，必然漂移（AGENTS §4「数据只在一处定义」）。


def is_trading_day(day: str, market: str = "CN") -> bool:
    """这天是不是交易日。拿不到日历就退回"工作日"（周六周日一定不是）。"""
    d = trading_days(str(day)[:10], str(day)[:10], market)
    return len(d) == 1


def last_closed_trading_day(now: pd.Timestamp | None = None,
                            market: str = "CN") -> str:
    """**最后一个已经收盘的交易日**。

    收盘（15:30）之前，"今天"的数据还不完整 → 上一个交易日；
    今天不是交易日（周末/节假日）→ 上一个交易日。
    """
    now = now or pd.Timestamp.now(tz="Asia/Shanghai")
    today = now.strftime("%Y-%m-%d")
    if now.hour * 60 + now.minute >= 15 * 60 + 30 and is_trading_day(today, market):
        return today
    days = trading_days(str((now - pd.Timedelta(days=30)).date()), today, market)
    if len(days) >= 2:
        return str(days[-2].date())
    return str(days[-1].date()) if len(days) else today


def find_gaps(table: str, lookback: int = 20, *, path: Path | None = None) -> list[str]:
    """最近 `lookback` 个交易日里，`table` **缺哪几天**（`YYYY-MM-DD` 列表）。

    为什么需要它：`MAX(date)` 只能看"尾巴"，**看不出中间的洞**。
    2026-11-21 的 `stock_daily` 就是"11-18 有、11-19 无、11-20 有"，
    `MAX=11-20` 完全正常——靠人眼看 `/data` 才发现。

    终点取 `last_closed_trading_day()`（收盘前不算今天），起点 = 终点往前
    `lookback` 个交易日。表不存在 / 空表 → 返回整个窗口（全缺）。
    """
    now = pd.Timestamp.now(tz="Asia/Shanghai")
    end = last_closed_trading_day(now)
    days = trading_days(str((pd.Timestamp(end) - pd.Timedelta(days=lookback * 3)).date()), end)
    window = [str(d.date()) for d in (days[-lookback:] if len(days) > lookback else days)]
    if not window:
        return []
    con = _conn(path)
    try:
        have = {str(r[0])[:10] for r in con.execute(
            f'SELECT DISTINCT date FROM "{table}" WHERE date >= ?', (window[0],))}
    except Exception:  # noqa: BLE001
        have = set()
    finally:
        con.close()
    return [d for d in window if d not in have]


def write_guard(table: str, trade_date: str, *, source: str | None = None,
                path: Path | None = None,
                now: pd.Timestamp | None = None) -> tuple[bool, str]:
    """**能不能把 `trade_date` 这天的数据写进 `table`** → `(能写?, 原因)`。

    四条判断（任何"把当前快照当日线写"的源都必须走这里）：

    1. `trade_date` 必须就是**今天** —— 周末/节假日接口会回上一个交易日的
       `timestamp`，照写就是把旧数据当新数据灌进去；
    2. 今天必须**已收盘**（≥15:30）—— 盘中写进去是半天的量；
    3. `trade_date` 必须是**交易日** —— 2026-11-21（周六）实测：快照仍返回
       `timestamp = 今天`，但值是**周五收盘**；只判"日期==今天"会把周五数据
       整份标成周六写进库（5,572 行与 11-20 逐字段相同）；
    4. 库里**已经有这一天或更新的日期** → 跳过（幂等，重复跑不写重）。

    ⚠️ 第 4 条边界是 `>=`：等价于"只补更新的日期"——同一天重复跑不写重，
    也不会把更旧的快照盖到更新的行上。

    返回 `(True, "")` 或 `(False, 跳过原因)`；原因里带 `source` 便于定位。
    """
    src = f"{source}：" if source else ""
    now = now or pd.Timestamp.now(tz="Asia/Shanghai")
    td = str(trade_date)[:10]
    if td != now.strftime("%Y-%m-%d"):
        return False, (f"{src}接口的交易日是 {td}，今天是 {now:%Y-%m-%d}"
                       f"（周末/节假日）→ 不写")
    if now.hour * 60 + now.minute < 15 * 60 + 30:
        return False, f"{src}现在 {now:%H:%M} 未收盘 → 不写（盘中数据是半天的量）"
    if not is_trading_day(td):
        return False, (f"{src}{td} 不是交易日（快照仍回 timestamp=今天，"
                       f"但值是上一交易日的收盘）→ 不写")
    con = _conn(path)
    try:
        latest = con.execute(f'SELECT MAX(date) FROM "{table}"').fetchone()[0]
    except Exception:  # noqa: BLE001
        latest = None
    finally:
        con.close()
    if latest and str(latest)[:10] >= td:
        return False, f"{src}{table}：{td} 已存在（最新 {latest}）→ 跳过"
    return True, ""
