"""数据服务：模块访问数据的**唯一入口**。

方法签名与 `datahub.db` 对齐，外加两个"受控动作"（触发一次已知流程，不是写接口）。
进程内直接 import 底层实现；HTTP 兜底留给将来（前端由 FastAPI 提供，不是这里）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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
        return self.written >= 0 and not self.message.startswith("失败")


class DataService:
    """进程内 API。**不做缓存、不做变换**——数据层给什么就是什么。"""

    def __init__(self, db_path: Any = None):
        # 延迟 import，避免 service 与 datahub 在模块加载期互相牵连
        from datahub import DB_PATH
        import datahub.fin as _fin

        self._db = _fin
        self.db_path = db_path or DB_PATH

    # ---------------- 读（与 db 对齐）
    def read(self, name: str, *, column: str | None = None,
             start: str | None = None, end: str | None = None):
        return self._db.read(name, column=column, start=start, end=end)

    def read_many(self, names: list[str], *, start: str | None = None,
                  end: str | None = None) -> pd.DataFrame:
        return self._db.read_many(names, start=start, end=end)

    def tables(self) -> list[str]:
        return self._db.tables()

    def metadata(self) -> pd.DataFrame:
        return self._db.metadata()

    def check(self) -> pd.DataFrame:
        return self._db.check()

    def missing(self) -> pd.DataFrame:
        return self._db.missing()

    def covered_tables(self) -> set[str]:
        """被**启用源**覆盖的表名（含 "a|b" 拆表的每一张）。

        状态里需要判断"哪些表该有新数据"，但不该去 import datahub 内部实现。
        """
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
                            written=0, parsed=0,
                            message="成功" if code == 0 else f"失败（code={code}）")

    # ---------------- 进程内单例（模块的入口用它取实例）
    def __repr__(self) -> str:  # pragma: no cover
        return f"<DataService {self.db_path}>"


_INSTANCE: DataService | None = None


def get_data_service() -> DataService:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = DataService()
    return _INSTANCE
