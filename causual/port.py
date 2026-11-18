"""causual 对外的能力：卡片检索 + 生成。

合并了原来的 `causual_cards.py`（文件检索/渲染）与 `service/card_service.py`
（生成编排）。两者都是"causual 能对外做什么"，没理由分居两个包。

其他模块只能通过 `Context.services["causual"]` 拿这个端口，不直接 import。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import markdown as _md
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "causual" / "data" / "output"

ASSET_NAME = {"ashare": "A 股中证全指", "gold": "国内黄金"}
PREFIX = {"card": "card", "detail": "detail",
          "macro": "macro", "macro_detail": "macro_detail"}
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
MONTH_RE = re.compile(r"(\d{4}-\d{2})(?!-\d)")


@dataclass
class CardMeta:
    asset: str
    date: str
    kind: str          # card / detail / macro / macro_detail
    path: Path
    size: int
    stale: bool = False
    changed: int = 0

    @property
    def is_macro(self) -> bool:
        return self.kind.startswith("macro")

    @property
    def href(self) -> str:
        return f"/card/{self.asset}/{self.date}?kind={self.kind}"


def cards_root(asset: str) -> Path:
    return OUTPUT / asset / "cards"


def card_path(asset: str, date: str, kind: str = "card") -> Path:
    """`kind` = card | detail | macro | macro_detail（macro 用 `YYYY-MM`）。"""
    if kind in ("macro", "macro_detail"):
        y, m = date.split("-")[:2]
        return cards_root(asset) / y / m / f"{PREFIX[kind]}_{y}-{m}.md"
    y, m, _ = date.split("-")
    return cards_root(asset) / y / m / f"{PREFIX[kind]}_{date}.md"


def _check_fingerprint(path: Path, meta=None) -> tuple[bool, int]:
    """卡片生成时的各表 end_date 与现在比，有几张变了 → 该重生成。

    `meta` 由调用方传入（service 从 datahub 端口取），这里不读库。
    """
    meta_path = path.with_suffix(".meta.json")
    if not meta_path.exists():
        return False, 0
    try:
        old = json.loads(meta_path.read_text(encoding="utf-8")).get("tables") or {}
    except Exception:  # noqa: BLE001
        return False, 0
    if not old:
        return False, 0
    try:
        if meta is None or not len(meta):
            return False, 0
        cur = {r["name"]: str(r["end_date"]) for _, r in meta.iterrows()
               if r.get("end_date")}
    except Exception:  # noqa: BLE001
        return False, 0
    changed = sum(1 for k, v in old.items() if cur.get(k) and cur[k] != v)
    return changed > 0, changed


class CausalPort:
    """卡片能力。生成是幂等的：目标日期已有卡片就直接返回路径。"""

    # ---------------- 卡片文件
    def list_cards(self, asset: str, start: str | None = None,
                   end: str | None = None,
                   table_meta=None) -> list[CardMeta]:
        """`table_meta` 是 datahub 的 metadata 表（判断卡片是否该重生成）。"""
        root = cards_root(asset)
        if not root.exists():
            return []
        out: list[CardMeta] = []
        for p in root.rglob("*.md"):
            is_macro = p.stem.startswith("macro")
            m = (MONTH_RE if is_macro else DATE_RE).search(p.stem)
            if not m:
                continue
            d = m.group(1)
            if start and d < start:
                continue
            if end and d > end:
                continue
            if p.stem.startswith("macro_detail"):
                kind = "macro_detail"
            elif is_macro:
                kind = "macro"
            elif p.stem.startswith("detail"):
                kind = "detail"
            else:
                kind = "card"
            cm = CardMeta(asset, d, kind, p, p.stat().st_size)
            cm.stale, cm.changed = _check_fingerprint(p, table_meta)
            out.append(cm)
        return sorted(out, key=lambda c: (c.date, c.kind), reverse=True)

    def get_card(self, asset: str, date: str, kind: str = "card") -> str:
        p = card_path(asset, date, kind)
        if not p.exists():
            raise FileNotFoundError(f"没有 {asset} {date} 的{kind}卡：{p}")
        return p.read_text(encoding="utf-8")

    def to_html(self, text: str) -> str:
        return _md.markdown(text, extensions=["tables", "fenced_code", "toc", "sane_lists"])

    def latest(self, asset: str) -> CardMeta | None:
        cs = [c for c in self.list_cards(asset) if c.kind == "card"]
        return cs[0] if cs else None

    # ---------------- 生成
    def required_tables(self, asset: str) -> tuple[str, ...]:
        """这个资产需要哪些数据表（纯声明，调用方负责读）。"""
        from causual.causual_assets import load as load_asset

        return tuple(load_asset(asset).REQUIRED_TABLES)

    def last_data_date(self, asset: str, panel: pd.DataFrame) -> str:
        """面板最后一天（真实交易日，不是外推日）。

        `panel` = `get_data(Panel.CAUSUAL_*)` 的输出，价格列叫 `close`。
        """
        if panel is None or "close" not in panel.columns or not len(panel):
            raise RuntimeError(f"{asset} 的面板没有 close 列（装配点传错了？）")
        return str(pd.DatetimeIndex(panel.index)[-1].date())

    def panel_kind(self, asset: str):
        """这个资产要哪个耦合层面板（`shared.adapter.Panel` 的枚举值）。

        **纯查表，不取数**：装配点（cli / service / web）拿它去 `get_data(...)`。
        """
        from causual.causual_assets import load as load_asset

        return load_asset(asset).PANEL_KIND

    def macro_month(self, freshness: list[dict] | None) -> str:
        """月频卡该覆盖的月份（月/季频表 `actual` 的最大值所在月）。

        只吃装配点算好的新鲜度行（`shared.adapter.freshness(panel)`）——它本来就是
        按**本资产的面板**算的，所以不需要再传 `asset` 收窄（旧的 `asset=` 关键字
        参数已删；不删的话黄金仍可能被别的表拖走）。
        """
        from causual.causual_core.pipeline import _month_of

        return _month_of(freshness)

    def exists(self, asset: str, date: str, kind: str = "card") -> bool:
        return card_path(asset, date, kind).exists()

    def generate(self, asset: str, panel: pd.DataFrame,
                 date: str | None = None,
                 state: dict | None = None,
                 freshness: list[dict] | None = None) -> Path:
        if date is None:
            date = self.last_data_date(asset, panel)
        path = card_path(asset, date)
        if path.exists():
            return path
        from causual.causual_core import pipeline

        return pipeline.run(asset, panel, freshness=freshness, state=state,
                            asof=date, quiet=True)

    def generate_macro(self, asset: str, month: str, panel: pd.DataFrame,
                       state: dict | None = None,
                       freshness: list[dict] | None = None) -> Path:
        path = card_path(asset, month, "macro")
        if path.exists():
            return path
        from causual.causual_core import pipeline

        pipeline.run(asset, panel, freshness=freshness, state=state, quiet=True)
        return card_path(asset, month, "macro")

    def regenerate(self, asset: str, date: str, panel: pd.DataFrame,
                   state: dict | None = None,
                   freshness: list[dict] | None = None) -> Path:
        path = card_path(asset, date)
        if path.exists():
            path.unlink()
        return self.generate(asset, panel, date=date, state=state, freshness=freshness)


_INSTANCE: CausalPort | None = None


def get_port() -> CausalPort:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = CausalPort()
    return _INSTANCE
