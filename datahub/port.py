"""datahub 对外的能力（其他模块只能通过这里访问数据层）。

`service` 在启动时拿一次单例，放进 `Context.services["datahub"]`。
**不在这里暴露任何写接口**——写库是数据层自己的事（`/data` 页面走内部 route）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd


@dataclass
class UpdateReport:
    ok: int = 0
    failed: int = 0
    rows: int = 0
    details: list[dict] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.failed == 0

    def summary(self) -> str:
        return f"更新 {self.ok} 张表、失败 {self.failed} 张、共 {self.rows} 行"


@dataclass
class IntakeReport:
    table: str = ""
    written: int = 0
    parsed: int = 0
    message: str = ""

    @property
    def success(self) -> bool:
        return not self.message.startswith("失败")


class DataPort:
    """进程内 API。不做缓存、不做变换——数据层给什么就是什么。"""

    def __init__(self, db_path: Path | str | None = None):
        from datahub import DB_PATH

        self.db_path = db_path or DB_PATH

    # ---------------- 读
    def read(self, name: str, *, column: str | None = None,
             start: str | None = None, end: str | None = None):
        import datahub.fin as fin

        return fin.read(name, column=column, start=start, end=end)

    def read_many(self, names: list[str], *, start: str | None = None,
                  end: str | None = None) -> pd.DataFrame:
        import datahub.fin as fin

        return fin.read_many(names, start=start, end=end)

    def series_map(self, names: list[str]) -> dict[str, pd.Series]:
        """把若干张表读成 {key: Series}，供资产面板使用。

        - single → `{表名: Series}`
        - multi  → `{表名: 列名}`
        - long   → pivot 成宽表后 `{表名: 代码}`（代码多时请自己先过滤）

        装配规则只写在这一个地方，四个调用方（两个 CLI、service、web）
        不用各写一遍。
        """
        import datahub.fin as fin
        # 注意：`datahub.fin.read` 这个**属性**被同名函数占用了，
        # 所以要按函数名 import，不能 `from datahub.fin import read as rd`。
        from datahub.fin.read import _long_columns, kind_of
        from datahub.fin.read import pivot as pivot_long

        from typing import cast

        out: dict[str, pd.Series] = {}
        for t in names:
            kind = kind_of(t)
            if kind == "long":
                df = fin.read_long(t)
                if df.empty:
                    continue
                value = "close" if "close" in df.columns else _long_columns(t)[0]
                wide = cast(pd.DataFrame, pivot_long(df, value))
                for code in wide.columns:
                    out[f"{t}:{code}"] = cast(pd.Series, wide[code]).astype(float)
            else:
                s = fin.read(t)
                if s is None:
                    continue
                if isinstance(s, pd.DataFrame):
                    for c in s.columns:
                        out[f"{t}:{c}"] = cast(pd.Series, s[c]).astype(float)
                else:
                    out[t] = cast(pd.Series, s)
        return out

    def tables(self) -> list[str]:
        import datahub.fin as fin

        return fin.tables()

    def metadata(self) -> pd.DataFrame:
        import datahub.fin as fin

        return fin.metadata()

    def check(self) -> pd.DataFrame:
        import datahub.fin as fin

        return fin.run_check()

    def missing(self) -> pd.DataFrame:
        import datahub.fin as fin

        return fin.missing()

    def covered_tables(self) -> set[str]:
        """被**启用源**覆盖的表名（含 "a|b" 拆表的每一张）。"""
        from datahub.fin.sources import SOURCES

        out: set[str] = set()
        for s in SOURCES:
            if s.enabled:
                out.update(x.strip() for x in s.table.split("|"))
        return out

    # ---------------- 受控动作
    def run_update(self, start: str = "2000-01-01", end: str | None = None,
                   only: list[str] | None = None) -> UpdateReport:
        from datahub.fin.update import run as _run

        df = _run(start=start, end=end, only=only)
        ok = int((df["status"] == "ok").sum()) if len(df) else 0
        failed = int((df["status"] == "失败").sum()) if len(df) else 0
        rows = int(df.loc[df["status"] == "ok", "rows"].sum()) if len(df) else 0
        return UpdateReport(ok=ok, failed=failed, rows=rows,
                            details=df.to_dict("records") if len(df) else [])

    def run_intake(self, source: str, path: str, load: bool = True) -> IntakeReport:
        from datahub.fin.intake.cli import run as _run
        from datahub.fin.intake.registry import SOURCES

        code = _run(source, path, load=load)
        src = SOURCES.get(source)
        return IntakeReport(table=(src.table if src else ""),
                            message="成功" if code == 0 else f"失败（code={code}）")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DataPort {self.db_path}>"


_INSTANCE: DataPort | None = None


def get_port() -> DataPort:
    """模块级单例：注册表里的 factory 指向它。"""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = DataPort()
    return _INSTANCE
