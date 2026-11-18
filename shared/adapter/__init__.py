"""耦合层：datahub → 消费方。

**唯一允许 import datahub 的 shared 子包**。外面只用这几个名字：

    from shared.adapter import Panel, get_data
    df = get_data(Panel.STATE_ASHARE)

`column_of` / `COLUMNS` / `tables_of` 是给 panel 与测试用的约定辅助。

⚠️ `shared/__init__.py` **不** import 本包（会把 datahub + sqlite 拖进纯工具层）。
"""

from shared.adapter.datahub_adapter import (COLUMNS, Panel, clear_cache,
                                            column_of, freshness, get_data,
                                            tables_of)

__all__ = ["Panel", "get_data", "freshness", "clear_cache", "COLUMNS",
           "column_of", "tables_of"]
