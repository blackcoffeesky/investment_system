"""multi 表的写路径：**只更新本次返回的列**，不许把没返回的列清空。

2026-11-16 的事故：`_write` 的 multi 分支用 `INSERT OR REPLACE`——REPLACE 是
"删旧行 + 插新行"，**本次没返回的列会变成 NULL**。`csi_all` 的源 `index_tx` 只回
`open/high/low/close/volume` 五列，于是任何一次刷新都把 `amount` / `turnover`
从 3503 清成 0，而**没有任何测试或 check 响过**（`state-layer-agent` 偶然看
`/state` 显示「缺」才发现）。

改成 `INSERT … ON CONFLICT(date) DO UPDATE SET <只更新返回的列>` 之后，四条要钉的行为：

1. **等价**：同一份数据写两次 == 写一次（列齐全时 `DO UPDATE` 与 `REPLACE` 等价）
2. **缺列不清空**：先写全列，再只写其中两列 → 其余列原值不动
3. **主键/冲突语义**：同一 date 写两次只有一行；值被更新；不会重复插入
   （`DATA_DDL` 里没有 TRIGGER / AUTOINCREMENT / WITHOUT ROWID，所以"就地更新"与
   "删了重插"没有可观察差异——这条是给后来人看的契约）
4. **列熔断**：某列从"大量有值"变"全空"时必须**大声报警**，不能静默写
"""

from __future__ import annotations

import contextlib
import io
import sqlite3

import pandas as pd

from datahub.fin.update import _write


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE IF NOT EXISTS t (date TEXT PRIMARY KEY, "
                "close REAL, amount REAL)")
    return con


def _frame(rows: list[tuple]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["date", "close", "amount"])
    return df.set_index("date")


def _dump(con: sqlite3.Connection) -> list[tuple]:
    return con.execute("SELECT date, close, amount FROM t ORDER BY date").fetchall()


def test_writing_twice_is_idempotent():
    df = _frame([("2026-11-10", 1.0, 10.0), ("2026-11-11", 2.0, 20.0)])
    con = _con()
    n1 = _write(con, "t", df, "2099-01-01")
    first = _dump(con)
    n2 = _write(con, "t", df, "2099-01-01")
    assert n1 == n2 == 2
    assert _dump(con) == first, "同一份数据写两次结果必须逐位相同"
    con.close()


def test_missing_columns_do_not_get_wiped():
    """本次修的 bug：只返回两列的一次写入，不能把没返回的列清空。"""
    con = _con()
    _write(con, "t", _frame([("2026-11-10", 1.0, 10.0)]), "2099-01-01")
    # 模拟 `index_tx`：只回 date + close
    partial = pd.DataFrame({"close": [1.5]}, index=pd.DatetimeIndex(["2026-11-10"]))
    _write(con, "t", partial, "2099-01-01")
    row = _dump(con)
    assert len(row) == 1, "同一 date 不该新增一行"
    assert row[0][1] == 1.5, "返回了的列要被更新"
    assert row[0][2] == 10.0, f"没返回的列必须保留原值，实际 {row[0][2]!r}（复现了 2026-11-16 的事故）"
    con.close()


def test_primary_key_semantics_unchanged():
    """同一 date 写两次：一行、值更新、不重复（`DO UPDATE` 与 `REPLACE` 在无触发器时等价）。"""
    con = _con()
    _write(con, "t", _frame([("2026-11-10", 1.0, 10.0)]), "2099-01-01")
    _write(con, "t", _frame([("2026-11-10", 9.0, 90.0)]), "2099-01-01")
    rows = con.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    assert rows == 1
    assert _dump(con)[0] == ("2026-11-10", 9.0, 90.0)
    con.close()


def test_column_loss_is_reported_loudly():
    """整列被清空时必须报警（2026-11-16 那次是静默的）。"""
    con = _con()
    _write(con, "t", _frame([("2026-11-10", 1.0, 10.0), ("2026-11-11", 2.0, 20.0)]),
           "2099-01-01")
    # 只写 close，amount 不出现 → 不会被清空（这是 ② 修的）
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        _write(con, "t", pd.DataFrame({"close": [1.1, 2.2]},
                                      index=pd.DatetimeIndex(["2026-11-10", "2026-11-11"])),
               "2099-01-01")
    assert "列熔断" not in buf.getvalue(), "没返回的列不该被清空，也就不该报警"

    # 显式传 amount 但整列是 NaN → 真的清空了，必须报警
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        _write(con, "t", _frame([("2026-11-10", 1.2, float("nan")),
                                 ("2026-11-11", 2.3, float("nan"))]), "2099-01-01")
    assert "列熔断" in buf.getvalue(), f"整列清空必须报警，实际 stderr：{buf.getvalue()!r}"
    assert "amount" in buf.getvalue()
    con.close()
