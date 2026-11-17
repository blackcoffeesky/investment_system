"""Fuyao 接入的两块契约：代码格式转换 + EAV 写入。

这两条都是"错了不会报错、只会静默写坏数据"的地方：

- `thscode` 必须带交易所后缀，库内是 `sh600519`；转换只许有一份（`fuyao.to_thscode`）
- `finances` 是 EAV，主键 4 列、`value` 是 TEXT，不能用 `write_long`（那是 `(code,date)`）

（本仓库的 `tests/run_tests.py` 直接调 `test_*` 函数，**没有 pytest**——所以不用 `pytest.raises`。）
"""

from __future__ import annotations

import sqlite3

import pandas as pd

from datahub.fin.sources import fuyao as F
from datahub.fin.update import write_eav


def test_thscode_roundtrip():
    cases = [("sh600519", "600519.SH", "sh600519"),
             ("sz000001", "000001.SZ", "sz000001"),
             ("bj920000", "920000.BJ", "bj920000")]
    for raw, ths, back in cases:
        assert F.to_thscode(raw) == ths, f"{raw} → {F.to_thscode(raw)}，应为 {ths}"
        assert F.to_raw_code(ths) == back


def test_thscode_rejects_bare_code():
    """纯 6 位码要**报错**，不要猜交易所——猜错就是把别人的数据写到这只票上。"""
    try:
        F.to_thscode("600519")
    except ValueError:
        return
    raise AssertionError("纯 6 位码应该报错")


def test_write_eav_is_idempotent_and_keeps_value_as_text():
    con = sqlite3.connect(":memory:")
    con.execute("""CREATE TABLE finances (
        thscode TEXT NOT NULL, report TEXT NOT NULL, abilities TEXT NOT NULL,
        index_id TEXT NOT NULL, value TEXT,
        PRIMARY KEY (thscode, report, abilities, index_id))""")
    df = pd.DataFrame({
        "thscode": ["600519.SH", "600519.SH"],
        "report": ["2026-3", "2026-3"],
        "abilities": ["profitability", "growth"],
        "index_id": ["sale_gross_margin", "x"],
        "value": ["89.12000000", "1.5"],
    })
    assert write_eav(con, "finances", df) == 2
    assert write_eav(con, "finances", df) == 2          # 重跑不写重
    assert con.execute("SELECT COUNT(*) FROM finances").fetchone()[0] == 2
    # value 保持字符串原样（浮点往返会丢 89.12000000 的尾零）
    v = con.execute("SELECT value FROM finances WHERE index_id='sale_gross_margin'").fetchone()[0]
    assert v == "89.12000000", f"value 被转成了 {v!r}"
    # 同主键写新值 → 覆盖而不是新增
    df2 = df.copy()
    df2["value"] = ["90.00000000", "1.5"]
    write_eav(con, "finances", df2)
    assert con.execute("SELECT COUNT(*) FROM finances").fetchone()[0] == 2
    v2 = con.execute("SELECT value FROM finances WHERE index_id='sale_gross_margin'").fetchone()[0]
    assert v2 == "90.00000000"
    con.close()
