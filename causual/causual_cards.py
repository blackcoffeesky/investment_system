"""卡片的读写接口：按日期检索、渲染 HTML。

目录约定：`causual/output/{asset}/cards/YYYY/MM/card_YYYY-MM-DD.md`
按年月分目录，三年后也不会变成几千个文件平铺。

这里只做"找文件 + 转 HTML"，不碰模型——服务层的路由直接调它。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import markdown as _md

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "causual" / "output"

ASSET_NAME = {"ashare": "A 股中证全指", "gold": "国内黄金"}

# kind → 文件名前缀
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
    stale: bool = False        # 生成后相关表又更新过？
    changed: int = 0           # 有几张表变了

    @property
    def is_macro(self) -> bool:
        return self.kind.startswith("macro")

    @property
    def href(self) -> str:
        return f"/card/{self.asset}/{self.date}?kind={self.kind}"


def cards_root(asset: str) -> Path:
    return OUTPUT / asset / "cards"


def card_path(asset: str, date: str, kind: str = "card") -> Path:
    """`kind` = card | detail | macro | macro_detail

    macro 的"日期"是 `YYYY-MM`（月份），不是 `YYYY-MM-DD`。
    """
    if kind in ("macro", "macro_detail"):
        y, m = date.split("-")[:2]
        return cards_root(asset) / y / m / f"{PREFIX[kind]}_{y}-{m}.md"
    y, m, _ = date.split("-")
    return cards_root(asset) / y / m / f"{PREFIX[kind]}_{date}.md"


def list_cards(asset: str, start: str | None = None,
               end: str | None = None) -> list[CardMeta]:
    """列出某资产的卡片（新→旧）。目录不存在时返回空列表。"""
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
        elif p.stem.startswith("macro"):
            kind = "macro"
        elif p.stem.startswith("detail"):
            kind = "detail"
        else:
            kind = "card"
        meta = CardMeta(asset, d, kind, p, p.stat().st_size)
        meta.stale, meta.changed = _check_fingerprint(p)
        out.append(meta)
    return sorted(out, key=lambda c: (c.date, c.kind), reverse=True)


def _check_fingerprint(path: Path) -> tuple[bool, int]:
    """卡片生成时的各表 end_date 与现在比，有几张变了。

    有变化 = 数据后来又更新过，这张卡的内容可能已经过时，建议重新生成。
    """
    import json

    meta_path = path.with_suffix(".meta.json")
    if not meta_path.exists():
        return False, 0
    try:
        old = json.loads(meta_path.read_text(encoding="utf-8")).get("tables") or {}
    except Exception:  # noqa: BLE001
        return False, 0
    if not old:
        return False, 0

    from datahub.fin import metadata

    try:
        cur = {r["name"]: str(r["end_date"]) for _, r in metadata().iterrows()
               if r.get("end_date")}
    except Exception:  # noqa: BLE001
        return False, 0
    changed = sum(1 for k, v in old.items() if cur.get(k) and cur[k] != v)
    return changed > 0, changed


def get_card(asset: str, date: str, kind: str = "card") -> str:
    p = card_path(asset, date, kind)
    if not p.exists():
        raise FileNotFoundError(f"没有 {asset} {date} 的{'详情' if kind == 'detail' else ''}卡：{p}")
    return p.read_text(encoding="utf-8")


def to_html(text: str) -> str:
    return _md.markdown(text, extensions=["tables", "fenced_code", "toc", "sane_lists"])


def latest(asset: str) -> CardMeta | None:
    cs = [c for c in list_cards(asset) if c.kind == "card"]
    return cs[0] if cs else None
