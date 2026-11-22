"""写库守卫（**写数据**的附加功能，不属于"查数据"）。

2026-11-22 从 `fin/check.py` 搬来：`check` 负责"查"（缺口/新鲜度/主键），
`guard` 负责"写之前挡一下"。两者语义不同，混在一起会让 check 长出写库概念。

唯一职责：判断**能不能把某个交易日的数据写进某张表**（四条），返回 `(能不能写, 原因)`。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from datahub.core.conn import connect
from shared.utils.trading_calendar import is_trading_day


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


def _conn(path: Path | None = None):
    """只读连接（守卫要查"库里最新到哪天"）。"""
    return connect(path) if path is None else connect(path)
