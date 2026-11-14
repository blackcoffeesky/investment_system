"""新鲜度监控清单必须对得上真实表名——否则监控在"假装工作"。

背景（2026-11-14 发现）
----------------------
`causual_core/freshness.py` 的 `WATCH_MACRO` 写着
`cn_cpi / cn_ppi / cn_m2 / cn_exports / cn_social_fin`，
但数据层的表名早就改成 `cpi_yoy / ppi_yoy / m1_yoy / m2_yoy / exports_yoy`：

    # check() 里
    if t not in idx.index:
        continue          ← 8 个名字只命中 3 个，剩下 5 个静默跳过

后果**不是报错，是"没有过期表"这个结论永远成立**。你以为是有人在看，其实没有。
同一份过期清单在 `pipeline._month_of()` 里还有一份副本（所以 `MONTH_TABLES`
现在只有一处定义，两处都读它）。

这批测试盯三件事：
1. 清单里的名字**在 `DataPort.tables()` 里存在**（改名/删表要当场红）
2. 清单里的表 `freq` 是 `_period_end()` 认得的（否则照样静默跳过）
3. `check()` 真的能抓出过期表，且对认不出的名字**会出声**
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr

import pandas as pd

# 固定一个"今天"，避免测试随自然日漂移（也避免周末影响 D 频判定）
TODAY = pd.Timestamp("2026-11-14")


def _meta(rows: list[tuple[str, str, int, str]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["name", "freq", "lag_days", "end_date"])


def test_watchlist_names_exist_in_datahub():
    """`WATCH` / `MONTH_TABLES` / `WATCH_MACRO` 里每个名字都得是真表。

    ⚠️ 这条在数据层删表时会红——**这是有意的**：删表就得把清单一起改，
    不能让它退化成静默跳过。（`dividend_annual` 已于 v5.42 从清单移除：
    causual 不再读那张表，股息率改由 `DataPort.dividend_yield()` 现算。）
    """
    from causual.causual_core import freshness
    from datahub.port import get_port

    known = set(get_port().tables())
    assert known, "DataPort.tables() 是空的——数据库没建好？"

    bad: dict[str, list[str]] = {}
    for label, names in (("WATCH", freshness.WATCH),
                         ("MONTH_TABLES", freshness.MONTH_TABLES),
                         ("WATCH_MACRO", freshness.WATCH_MACRO)):
        missing = [t for t in names if t not in known]
        if missing:
            bad[label] = missing

    assert not bad, f"新鲜度清单里有数据层不存在的表（改名/删表了？）：{bad}"


def test_watchlist_freqs_are_checkable():
    """清单里的表，频率必须是 `_period_end()` 认得的 D/M/Q/Y。

    认不出的 freq（W / SPARSE）会让 `check()` 直接 `continue` —— 和名字写错
    一样的静默失效。要把周频表加进清单，先给 `_period_end()` 补那一支。
    """
    from causual.causual_core.freshness import WATCH, WATCH_MACRO, _period_end
    from datahub.port import get_port

    meta = get_port().metadata().set_index("name")
    skipped: dict[str, str] = {}
    for t in tuple(WATCH) + tuple(WATCH_MACRO):
        if t not in meta.index:
            continue                      # 名字缺失由上面那条测试负责
        freq = str(meta.loc[t, "freq"] or "D")
        if _period_end(freq, TODAY) is None:
            skipped[t] = freq

    assert not skipped, f"这些表的 freq 判不了过期，会被静默跳过：{skipped}"


def test_check_flags_stale_and_passes_fresh():
    """行为验证：过期的报出来，没过期的不报。"""
    from causual.causual_core.freshness import check, stale_only

    meta = _meta([
        ("stale_m", "M", 10, "2026-07-31"),      # 落后 3 个月，lag 10 早该发了
        ("fresh_m", "M", 10, "2026-10-31"),      # 上月值，正常
        ("stale_q", "Q", 45, "2025-12-31"),      # 季频落后一年
        ("fresh_d", "D", 0, "2026-11-13"),       # 昨天
    ])
    tables = ("stale_m", "fresh_m", "stale_q", "fresh_d")

    stale = {f.table for f in stale_only(meta, tables, TODAY)}
    assert stale == {"stale_m", "stale_q"}, stale

    all_items = {f.table: f for f in check(meta, tables, TODAY)}
    assert all_items["fresh_m"].stale is False
    assert all_items["stale_m"].days_behind > 0


def test_check_handles_annual_tables():
    """`freq="Y"` 不能返回 None——否则年频表（如分红）永远不被检查。"""
    from causual.causual_core.freshness import check, stale_only

    meta = _meta([
        ("annual_old", "Y", 90, "2023-12-31"),   # 两年前的年度值
        ("annual_ok", "Y", 90, "2025-12-31"),    # 上年末，去年年报已出
    ])
    tables = ("annual_old", "annual_ok")

    stale = {f.table for f in stale_only(meta, tables, TODAY)}
    assert stale == {"annual_old"}, stale
    assert check(meta, tables, TODAY), "年频表一条都没检查 = 白名单形同虚设"


def test_check_warns_when_watchlist_name_is_unknown():
    """清单里写了 metadata 没有的表，必须**出声**（这条就是当年的 bug）。"""
    from causual.causual_core.freshness import check

    meta = _meta([("cpi_yoy", "M", 9, "2026-10-31")])
    buf = io.StringIO()
    with redirect_stderr(buf):
        check(meta, ("cpi_yoy", "cn_cpi"), TODAY)

    err = buf.getvalue()
    assert "cn_cpi" in err, f"没有对不存在的表告警：{err!r}"
    assert "新鲜度" in err, err
