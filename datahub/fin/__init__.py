"""金融数据层（原 `datahub` 根目录下的那套）。

对外只有读接口：

    from datahub.fin import read, metadata, tables, run_check, missing
    from datahub.fin import read_many

写库只在 `update` / `intake` 内部发生。
"""

from __future__ import annotations

from datahub.fin.read import metadata, read, read_many, tables

# 注意：不要再叫 `check`。`datahub.fin.check` 是子模块，一旦有人 `import datahub.fin.check`，包属性 `check` 会被替换成模块对象，
# 于是 `fin.check()` 报 "'module' object is not callable"。
from datahub.fin.check import missing_table as missing
from datahub.fin.read import long_stats, read_long, read_static, static_stats
from datahub.fin.check import run as run_check

__all__ = ["read", "read_many", "metadata", "tables", "run_check",
           "missing", "read_long", "long_stats"]
