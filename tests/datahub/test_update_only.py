"""`update --only` 的名字校验 + 空报告不炸 + 手工表的"怎么更新"提示。

2026-11-17 的事故链（三个 bug 叠出一个误导性报错）：

```
update --only gold_etf_holdings
  → 它不在 SOURCES 里（是**手工导入表**，靠人上传 WGC xlsx）
  → run() 静默过滤成空集合 → 返回无列空 DataFrame
  → port.run_update 取 df["status"] → KeyError: 'status'
  → 读起来像"WGC 源挂了"，实际是"这张表不归 update 管"
```

**静默无操作比报错更贵**：用户会以为"跑了但没效果"，然后去别处找原因。
"""

from __future__ import annotations

import datahub.fin.update as U
from datahub.fin.intake.registry import TABLE_TO_KEY, hint_for


def test_unknown_table_raises_and_lists_sources():
    try:
        U.run(only=["not_a_table"])
    except ValueError as e:
        msg = str(e)
        assert "not_a_table" in msg
        assert "SOURCES" in msg or "当前" in msg, msg
        return
    raise AssertionError("未知表名必须报错，不能静默返回空表")


def test_intake_table_raises_with_executable_hint():
    """手工表要给出**可直接执行**的命令，而不是一句"更新失败"。"""
    assert TABLE_TO_KEY.get("gold_etf_holdings") == "wgc_etf"
    hint = hint_for("gold_etf_holdings")
    assert hint and "datahub.fin.intake wgc_etf" in hint, hint
    try:
        U.run(only=["gold_etf_holdings"])
    except ValueError as e:
        assert "datahub.fin.intake wgc_etf" in str(e), str(e)
        return
    raise AssertionError("手工表名必须报错并带导入命令")


def test_run_update_survives_empty_report():
    """`picked` 为空（源都存在但 `enabled=False`）时，`run_update` 不能 KeyError。"""
    from datahub.port import get_port

    r = get_port().run_update(only=["adj_factor"])   # 已注册但 enabled=False
    assert r.ok == 0 and r.failed == 0 and r.rows == 0
    assert r.details == []
