"""EAV 表（`finances`）的登记与检查契约。

事故（2026-11-17 服务器）：`finances` 是 EAV（`thscode, report, abilities, index_id, value`），
但 `_ensure_meta` 靠"列是否等于 `["date","value"]`"猜 kind → 猜成 `multi` →
`check` 去 `fin.read` 它 → `DatabaseError` → **validate 卡住，卡片出不来**。

根因是**"我能写进去"和"我能读出来"被混为一谈**：写入路径给 EAV 用了，
`check` 却假设所有表都能 `read`。所以：

1. 形态登记在 `core/schema.EAV_TABLES`（**不靠列名猜**）
2. `check` 遇到 eav 表按真实结构验（覆盖度 / 主键唯一 / 新鲜度），**不调 `read`**
"""

from __future__ import annotations

import sqlite3

import pandas as pd

from datahub.core.schema import EAV_TABLES
from datahub.fin.check import _check_eav, _eav_expected_period


def _fixture(dups: bool = False) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute("""CREATE TABLE finances (
        thscode TEXT NOT NULL, report TEXT NOT NULL, abilities TEXT NOT NULL,
        index_id TEXT NOT NULL, value TEXT,
        PRIMARY KEY (thscode, report, abilities, index_id))""")
    con.execute("INSERT INTO finances VALUES ('600519.SH','2026-3','growth','x','1')")
    con.execute("INSERT INTO finances VALUES ('000001.SZ','2026-3','growth','x','2')")
    return con


def test_finances_is_registered_as_eav():
    assert "finances" in EAV_TABLES, "finances 必须在登记处，不能靠列名猜"
    spec = EAV_TABLES["finances"]
    assert spec["pk"] == ("thscode", "report", "abilities", "index_id")
    assert spec["entity"] == "thscode" and spec["period"] == "report"


def test_expected_period_is_a_valid_report_string():
    """预期报告期必须是 `YYYY-Q` 形式（季度末已披露的那一期）。"""
    import re

    got = _eav_expected_period(pd.Timestamp("2026-11-17"))
    assert re.fullmatch(r"\d{4}-[1-4]", got), got
    assert got == "2026-3", got
    # Q1 的上一期要跨年
    assert _eav_expected_period(pd.Timestamp("2027-02-01")) == "2026-4"


def test_check_eav_reports_coverage_and_no_issues():
    con = _fixture()
    info, issues = _check_eav(con, "finances", EAV_TABLES["finances"])
    assert info["rows"] == 2
    assert info["entities"] == 2
    assert info["periods"] == 1
    assert issues == [], issues     # 2026-3 就是当前预期期
    con.close()
