"""模块对外能力的类型契约。

状态机 / service 只依赖这里的 Protocol，不依赖任何模块的内部实现。
模块那边是**结构化实现**（不继承也没关系），只要方法签名对得上。

加新模块时，在这里补一个 Protocol；端口实现放 `<module>/port.py`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import pandas as pd


class DataPort(Protocol):
    """数据层能力（`datahub/port.py` 实现）。"""

    def read(self, name: str, *, column: str | None = None,
             start: str | None = None, end: str | None = None) -> pd.Series: ...

    def read_many(self, names: list[str], *, start: str | None = None,
                  end: str | None = None) -> pd.DataFrame: ...

    def tables(self) -> list[str]: ...

    def metadata(self) -> pd.DataFrame: ...

    def check(self) -> pd.DataFrame: ...

    def missing(self) -> pd.DataFrame: ...

    def covered_tables(self) -> set[str]: ...

    def run_update(self, start: str = "2000-01-01", end: str | None = None,
                   only: list[str] | None = None) -> Any: ...

    def run_intake(self, source: str, path: str, load: bool = True) -> Any: ...


class CausalPort(Protocol):
    """因果层能力（`causual/port.py` 实现）：卡片检索 + 生成。"""

    def last_data_date(self, asset: str, ds: DataPort | None = None) -> str: ...

    def macro_month(self, asset: str) -> str: ...

    def exists(self, asset: str, date: str, kind: str = "card") -> bool: ...

    def generate(self, asset: str, date: str | None = None,
                 ds: DataPort | None = None) -> Path: ...

    def generate_macro(self, asset: str, month: str) -> Path: ...

    def regenerate(self, asset: str, date: str, ds: DataPort | None = None) -> Path: ...

    def list_cards(self, asset: str, start: str | None = None,
                   end: str | None = None) -> list[Any]: ...

    def get_card(self, asset: str, date: str, kind: str = "card") -> str: ...

    def to_html(self, text: str) -> str: ...
