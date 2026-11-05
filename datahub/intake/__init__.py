"""手工数据入库的兼容入口。

示例：
    python -m datahub.intake wgc datahub/fin/intake/raw/xxx.xlsx --load
    python -m datahub.intake wgc_etf ... --load

实现都在 `datahub.fin.intake`。
"""

from datahub.fin.intake import registry  # noqa: F401
from datahub.fin.intake.cli import main  # noqa: F401

__all__ = ["registry", "main"]
