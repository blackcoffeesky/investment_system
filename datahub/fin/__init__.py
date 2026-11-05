"""金融数据层（原 `datahub` 根目录下的那套）。

对外只有读接口：

    from datahub.fin import read, metadata, tables, check, missing
    from datahub.fin import read_many

写库只在 `update` / `intake` 内部发生。
"""

from __future__ import annotations

from datahub.fin.read import check, metadata, missing, read, read_many, tables

__all__ = ["read", "read_many", "metadata", "tables", "check", "missing"]
