"""datahub 的异常类型。

分得细一点是为了让调用方能区分"表不存在"（可忽略）和"表存在但结构不对"（要修）。
"""

from __future__ import annotations


class DatahubError(Exception):
    """所有 datahub 异常的基类。"""


class TableNotFound(DatahubError):
    """请求的表不在 metadata 里。"""


class OpsTableAccess(DatahubError):
    """用金融接口访问运维表（或反过来）——这两个命名空间不允许串。"""


class SchemaError(DatahubError):
    """表结构与预期不符（列数对不上、缺主键等）。"""
