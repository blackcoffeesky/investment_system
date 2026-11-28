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

from datahub.core.schema import DATA_DDL, EAV_TABLES
# 交易日历**只有一份**，在 `shared/utils/trading_calendar.py`（2026-11-22 搬走）。
# 这里只 import，不再定义（上次同名覆盖把 17 张日频表的缺口检查整体搞挂过）。
# 低频表按**周期**比对，不按天（天比会把"月初 + 月末"两行误判成缺口）
_PERIOD = {"M": "M", "Q": "Q", "Y": "Y"}
_DUP_LABEL = {"M": "同月", "Q": "同季", "Y": "同年"}

from shared.utils.trading_calendar import (
    expected_periods,
    is_trading_day,
    last_closed_trading_day,
    trading_days,
)


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


def run(path: Path | None = None, recent_days: int = 90,
        today: str | pd.Timestamp | None = None) -> pd.DataFrame:
    """返回问题清单。

    `recent_days`：只有最近这么多天内的缺口算"问题"（多半是忘了更新）；
    更早的缺口单独计数——那是历史源数据留下的，不是运维该天天盯的。

    `today`：**可指定"今天是哪天"**（默认现在）。不传的话整个结果随真实日期变，
    测试与回归都不复现（2026-11-23 的日期依赖红就是这么来的）。
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

    _today = _as_now(today).normalize()
    cutoff = str((_today - pd.Timedelta(days=recent_days)).date())
    # **有意冻结的表**（`SOURCES` 里 `enabled=False`）不参与 check：
    # 它们不是"忘了跑"，报出来只会和 `freshness` 的结论互相打脸
    # （2026-11-22 回归：切分 check 时把这段过滤丢了，冻结表又回到问题清单）。
    frozen = _frozen_tables()
    skipped_frozen: list[str] = []
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
        if name in frozen:
            skipped_frozen.append(name)
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

        freq = str(m["freq"] or "D")
        market = str(m["market"] or "CN")
        try:
            # `max_age_days` 不传：SPARSE 表按 `SPARSE_MAX_AGE` 的表级配置判过时，
            # 其余频率本来就不用它（洞/尾部各有各的算法）。
            gaps = find_gaps(name, today=today, path=path)
        except Exception as e:  # noqa: BLE001
            # **检查本身没跑成**，不是"数据有问题"：标成 check_failed，
            # 调用方要能分清（2026-11-22：17 张日频表全报「日历失败」，页面上
            # 却只像一条普通 issue，真实缺口一条也检不出来）。
            print(f"[check] {name}：日历失败（检查没跑成）{type(e).__name__}: {e}",
                  file=sys.stderr)
            rows.append({"name": name, "freq": freq, "market": market,
                         "issues": f"日历失败 {type(e).__name__}: {e}",
                         "status": "check_failed"})
            continue

        issues: list[str] = []
        # ⓪ 脏数据：窗口内出现了非交易日（守卫只管"写入前"，已入库的它不管）
        if gaps.get("non_trading"):
            nt = gaps["non_trading"]
            sample = "、".join(nt[:5])
            more = f" 等 {len(nt)} 天" if len(nt) > 5 else ""
            issues.append(f"非交易日数据 {len(nt)} 天（{sample}{more}）")
        # ① 尾部：最新 < 应到（**不套 lag_days**——宽限是 freshness 的事）
        if gaps["stale"]:
            tail = (f"最新停在 {gaps['stale']}" +
                    (f"，应到 {gaps['expected_latest']}" if gaps["expected_latest"] else ""))
            if freq == "SPARSE":
                tail += f"，超过 {SPARSE_MAX_AGE.get(name, SPARSE_MAX_AGE_DEFAULT)} 天未更新"
            issues.append(tail)
        # ② 区间内的洞：去掉已登记的（ack）
        missing = unacked(gaps["missing"], ack_map.get(name, set()), freq)
        recent = [d for d in missing if d >= cutoff]
        old = len(missing) - len(recent)
        # ③ 低频表的"同月多行"（重复，不是缺口）
        per = _PERIOD.get(freq.upper())
        if per and len(dates):
            counts = pd.Series(pd.DatetimeIndex(sorted(dates)).to_period(per)).value_counts()
            dup = counts[counts > 1].sort_index()
            if len(dup):
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
            # 只有"非交易日脏数据"时给 `warning`（脏数据该显眼，但不该让整个检查 fail）；
            # 还缺数/停更的，仍然算 `data`。
            only_dirty = (gaps.get("non_trading") and not gaps["missing"]
                          and not gaps["stale"] and len(issues) == 1)
            rows.append({"name": name, "freq": freq, "market": market,
                         "n": len(obj), "issues": "；".join(issues),
                         "status": "warning" if only_dirty else "data"})
    _announce_frozen(frozen, skipped_frozen)
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

#: 本进程里"跳过冻结表"的通知只打一次（每次调用都打会淹掉真正的告警）
_FROZEN_ANNOUNCED = False


def _frozen_tables() -> set[str]:
    """**有意停更**的表：所有源都标了 `frozen=True`。

    ⚠️ 判断依据是 `Source.frozen`，**不是**"有没有 enabled 的源"：
    `enabled=False` 有两种含义——「有意停更」（上游没了）和「手动节奏」
    （`shares_history` 季度跑、`adj_factor` 按需回填）。后者**必须继续被检查**，
    否则源悄悄停了我们不会知道（2026-11-28：`shares_history` 一度因此被跳过，
    180 天过时规则永远不触发）。
    """
    from datahub.fin.sources import SOURCES

    by_table: dict[str, list] = {}
    for src in SOURCES:
        for t in str(src.table).split("|"):
            by_table.setdefault(t, []).append(src)
    return {t for t, srcs in by_table.items() if srcs and all(x.frozen for x in srcs)}


def _announce_frozen(frozen: set[str], skipped: list[str]) -> None:
    """**跳过要"响"一次**：否则以后有人问"为什么这三张不报了"，又得翻代码。"""
    global _FROZEN_ANNOUNCED
    if _FROZEN_ANNOUNCED or not skipped:
        return
    _FROZEN_ANNOUNCED = True
    # 前缀用 `[datahub]` 而不是 `[新鲜度]`：`freshness` 和 `check` 共用这一条通知
    print(f"[datahub] 跳过 {len(skipped)} 张已冻结的表（源 enabled=False，有意停更）："
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


#: **SPARSE 表的"最久多久算过时"**（天）。SPARSE 没有"周期缺口"概念，
#: 只剩这一个判断；不写在这里就等于"没人管它会不会停"（`shibor3m` 的教训）。
#: `shares_history`（股本变动）：源是巨潮，正常季度内有新增；180 天没动静一定是源坏了。
SPARSE_MAX_AGE: dict[str, int] = {
    "shares_history": 180,
}
#: 没登记的 SPARSE 表用的兜底值
SPARSE_MAX_AGE_DEFAULT = 90

#: 各频率默认扫多长（**不是一刀切**）：D 扫 60 个交易日（约 3 个月，够抓到
#: "漏更一周"这类问题）；W 26 周、M 24 月、Q 8 季、Y 5 年——低频表一年才几个点，
#: 扫短了等于没扫。
_LOOKBACK: dict[str, int] = {
    "D": 60, "W": 26, "M": 24, "Q": 8, "Y": 5, "SPARSE": 24,
}


def _as_now(today: "str | pd.Timestamp | None" = None) -> pd.Timestamp:
    """`today` 参数 → 带时区的 `Timestamp`（默认现在）。

    ⚠️ **保留时间部分**：`last_closed_trading_day` 用"是否过 15:30"判断当天算不算
    "已收盘"。只保留日期会把它变成 00:00 → 永远算"未收盘" → 退回**前一个**交易日
    （`today="2026-11-20"` 会得到 `expected_latest=2026-11-19`，差一天）。
    """
    if today is None:
        return pd.Timestamp.now(tz="Asia/Shanghai")
    ts = pd.Timestamp(today)
    return ts.tz_localize("Asia/Shanghai") if ts.tzinfo is None else ts


def _non_trading_dates(have: set[str], start: str, end: str, market: str) -> list[str]:
    """窗口内**存在但不是交易日**的日期（脏数据）。

    2026-11-24 发现服务器上躺着 5,572 行 `2026-11-22`（**周日**）的 `stock_daily`
    ——守卫补上之前写进去的，一直没人发现。**守卫是"写入前拦"，已入库的脏数据它不清、
    也报不出来**，所以需要这条检查。

    **只对 A 股（`market=CN`）的 `D` 表调用**，两条实测理由：

    - 美股 / 全球序列**本来就有非交易日数据**：`fed_rate`（FRED DFF）每个日历日都有值、
      FRED 在美股假日（如 Labor Day）照发、`world_tension` 是新闻指数（周末也有分数）。
      对它们查非交易日 → 全是误报（2026-11-24 一开就报了 8 张表、几十天）。
    - `W/M/Q/Y` 的数据日期是**周期末日**，落在周末是正常的。

    **只对 `D` 表调用**：`W/M/Q/Y` 的数据日期是**周期末日**，本来就可能落在周末
    （`cpi_yoy` 有月末是周六的；`gold_cot_net_long` 是周线、日期全在周日——
    实测对 W 开启会一次报 33 天，全是误报）。

    ⚠️ 判定必须用 `is_trading_day()`（走交易所日历）：A 股有**调休开市**（周末上班）、
    也有**节假日休市**（工作日关门），`weekday() < 5` 是错的。
    """
    mkt = market if market != "NONE" else "CN"
    return sorted(d for d in have
                  if start <= d <= end and not is_trading_day(d, mkt))


def find_gaps(table: str, *, lookback: int | None = None,
              max_age_days: int | None = None,
              today: str | pd.Timestamp | None = None,
              path: Path | None = None) -> dict:
    """`table` 的**原始缺口事实**：区间内的洞 + 尾部是否过期。

    返回 `{"table", "freq", "missing": [日期…], "stale": 实际最新|None,
    "expected_latest": 应到的最新}`。

    三条语义（都刻意如此）：

    1. **不吃 `lag_days`**。宽限（"源还没发布"）是 `freshness` 的语义，不是"数据缺"。
       这里只报事实，调用方自己套宽限——否则就是第二个 `freshness`，两套定义必然打架。
    2. **按频率判"该有哪些期"**：`D` = 每个交易日（`shared` 的日历，含节假日休市）；
       `W/M/Q/Y` = 每个周期末（用 `expected_periods`），所以月表在月中不会被误报。
    3. **SPARSE / eav 没有"洞"的概念**，`missing` 恒为 `[]`，只报 `stale`。

    为什么返回 dict 而不是 list：`run()`（`/data` 页）和 `stock_daily_sync`（补数）
    要的是同一份结果的不同字段，返回 list 会让每个调用方各自再算一遍 stale。
    """
    from datahub.fin.read import kind_of

    con = _conn(path)
    try:
        row = con.execute("SELECT freq, market, end_date FROM metadata WHERE name=?",
                          (table,)).fetchone()
    finally:
        con.close()
    freq = str((row["freq"] if row else "") or "D").upper()
    market = str((row["market"] if row else "") or "CN")
    kind = kind_of(table, path)

    # `today` 可指定：**否则这个函数的结果随真实日期变**，测试只能靠"今天恰好是什么"，
    # 一天红一次（2026-11-23：`expected_latest` 从 11-20 变成 11-23 导致测试红）。
    now = _as_now(today)
    end = last_closed_trading_day(now, market if market != "NONE" else "CN")
    n = int(lookback if lookback is not None else _LOOKBACK.get(freq, 20))

    con = _conn(path)
    try:
        have = {str(r[0])[:10] for r in con.execute(
            f'SELECT date FROM "{table}"')} if kind not in ("eav",) else set()
    except Exception:  # noqa: BLE001
        have = set()
    finally:
        con.close()

    out: dict = {"table": table, "freq": freq, "missing": [],
                 "stale": None, "expected_latest": "", "non_trading": []}
    if kind == "eav" or freq == "SPARSE":
        # 稀疏表：没有"每周该有几行"的定义 → `missing` 恒空，**只报 `stale`**：
        # 超过 `max_age_days`（默认 90）没更新就算 stale。这就是原来
        # `run()` 里"最近一条停在 X"那条判断，现在统一成 `stale` 一个概念。
        latest = max(have) if have else None
        age_limit = int(max_age_days if max_age_days is not None
                       else SPARSE_MAX_AGE.get(table, SPARSE_MAX_AGE_DEFAULT))
        out["expected_latest"] = ""
        if latest is not None and (now.date() - pd.Timestamp(latest).date()).days > age_limit:
            out["stale"] = latest
        return out

    # 窗口：按 freq 往前推够长的日历区间，取最后 n 个"期"
    if freq == "D":
        span_days = n * 2
    else:
        span_days = {"W": n * 9, "M": n * 32, "Q": n * 95, "Y": n * 370}.get(freq, 60)
    start = str((pd.Timestamp(end) - pd.Timedelta(days=span_days)).date())
    # **表还没建的时候不算缺口**：新表（或刚换源的表）首日之前的日期不存在"该有"，
    # 拿它们报缺口只会制造噪音（临时表 11-09 才有数据，却报 8 月缺 51 天）。
    if have:
        first = min(have)
        if first > start:
            start = first

    if freq == "D":
        expected = [str(d.date()) for d in
                    expected_periods("D", market if market != "NONE" else "CN", start, end)]
        expected = expected[-n:] if len(expected) > n else expected
        out["expected_latest"] = expected[-1] if expected else end
        out["missing"] = [d for d in expected if d not in have]
        if str(market).upper() in ("CN", "SSE", "SZSE"):
            out["non_trading"] = _non_trading_dates(
                have, start, expected[-1] if expected else end, market)
        latest = max(have) if have else None
        if latest is not None and latest < out["expected_latest"]:
            out["stale"] = latest
        return out

    # ⚠️ W/M/Q/Y **按周期**比，不按日期相等：低频表的数据日期是"披露日/月末"
    # 之类的任意一天（如 `cpi_yoy` 停在 2026-10-31、上一期在 2026-10-15），
    # 逐日比会把整月算成缺口（`m1/m2/total_*` 的「53 天缺口」就是这么误报的）。
    per = {"W": "W", "M": "M", "Q": "Q", "Y": "Y"}[freq]
    end_p = pd.Timestamp(end).to_period(per)
    # "该有的最新一期" = **上一个已结束的周期**（当月/当季还没结束，不该要求它）
    last_ok_p = end_p - 1 if freq != "W" else end_p
    have_p = {pd.Timestamp(d).to_period(per) for d in have}
    window_p = pd.period_range(pd.Timestamp(start).to_period(per), last_ok_p, freq=per)
    window_p = window_p[-n:] if len(window_p) > n else window_p
    expected = [str(p.end_time.date()) for p in window_p]
    out["expected_latest"] = expected[-1] if expected else ""
    out["missing"] = [str(p.end_time.date()) for p in window_p if p not in have_p]
    # `W` **不查**非交易日：周线的日期本来就是"周期末日"（`gold_cot_net_long` 是周日），
    # 实测一开就 33 天全误报——和 M/Q/Y 同一个道理。
    if have:
        latest_p = pd.Timestamp(max(have)).to_period(per)
        if expected and latest_p < last_ok_p:
            out["stale"] = max(have)
    return out


def find_gaps_list(table: str, *, lookback: int | None = None,
                   today: str | pd.Timestamp | None = None,
                   path: Path | None = None) -> list[str]:
    """兼容薄包装：只要 `missing` 的调用方用它（别把调用点改散）。"""
    return find_gaps(table, lookback=lookback, today=today, path=path)["missing"]


def _period_of(day: str, freq: str) -> object:
    """一个日期属于哪个"期"（`M`/`Q`/`Y`），`D` 原样返回日期字符串。"""
    if freq in ("M", "Q", "Y"):
        return pd.Timestamp(day).to_period({"M": "M", "Q": "Q", "Y": "Y"}[freq])
    return day


def unacked(missing: list[str], acked: set[str], freq: str) -> list[str]:
    """从缺口里**去掉已登记的**（`missing` 表 ack）。

    `D` 按日比；`M`/`Q`/`Y` **按周期**比——低频表"月初一行 + 月末一行"是重复，
    按天比会把整月算成缺口（`m1/m2/total_*` 的「53 天缺口」就是这么误报的）。
    """
    if not missing:
        return []
    f = freq.upper()
    if f in ("M", "Q", "Y"):
        acked_p = {_period_of(a, f) for a in acked}
        return [d for d in missing if _period_of(d, f) not in acked_p]
    return [d for d in missing if d not in acked]


