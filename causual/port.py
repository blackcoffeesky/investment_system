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

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "causual" / "output"

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


def _check_fingerprint(path: Path) -> tuple[bool, int]:
    """卡片生成时的各表 end_date 与现在比，有几张变了 → 该重生成。"""
    from datahub.port import get_port

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
        cur = {r["name"]: str(r["end_date"]) for _, r in get_port().metadata().iterrows()
               if r.get("end_date")}
    except Exception:  # noqa: BLE001
        return False, 0
    changed = sum(1 for k, v in old.items() if cur.get(k) and cur[k] != v)
    return changed > 0, changed


class CausalPort:
    """卡片能力。生成是幂等的：目标日期已有卡片就直接返回路径。"""

    # ---------------- 卡片文件
    def list_cards(self, asset: str, start: str | None = None,
                   end: str | None = None) -> list[CardMeta]:
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
            meta = CardMeta(asset, d, kind, p, p.stat().st_size)
            meta.stale, meta.changed = _check_fingerprint(p)
            out.append(meta)
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

    def last_data_date(self, asset: str, series: dict) -> str:
        """面板最后一天（真实交易日，不是外推日）。"""
        table = "csi_all" if asset == "ashare" else "gold_cny"
        s = series.get(table)
        if s is None or not len(s):
            raise RuntimeError(f"{asset} 没有价格数据（{table} 为空）")
        return str(s.index[-1].date())

    def macro_month(self, asset: str) -> str:
        from causual.causual_core.pipeline import _month_of

        return _month_of(asset)

    def exists(self, asset: str, date: str, kind: str = "card") -> bool:
        return card_path(asset, date, kind).exists()

    def generate(self, asset: str, series: dict, meta=None,
                 date: str | None = None) -> Path:
        if date is None:
            date = self.last_data_date(asset, series)
        path = card_path(asset, date)
        if path.exists():
            return path
        from causual.causual_core import pipeline

        return pipeline.run(asset, series, meta, asof=date, quiet=True)

    def generate_macro(self, asset: str, month: str, series: dict,
                       meta=None) -> Path:
        path = card_path(asset, month, "macro")
        if path.exists():
            return path
        from causual.causual_core import pipeline

        pipeline.run(asset, series, meta, quiet=True)
        return card_path(asset, month, "macro")

    def regenerate(self, asset: str, date: str, series: dict, meta=None) -> Path:
        path = card_path(asset, date)
        if path.exists():
            path.unlink()
        return self.generate(asset, series, meta, date=date)


_INSTANCE: CausalPort | None = None


def get_port() -> CausalPort:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = CausalPort()
    return _INSTANCE
