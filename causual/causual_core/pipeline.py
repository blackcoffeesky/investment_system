"""出卡片：面板 → 信号 → 叙事 → 落盘。不拟合任何模型。

    python -m causual.causual_core.cli daily --asset ashare
    python -m causual.causual_core.cli daily --asset gold --asof 2026-11-03

数据从**外面传进来**（`panel: DataFrame`），这里不碰数据层：
- service / CLI 在装配点 `get_data(Panel.CAUSUAL_*)` 取好再传
- 本模块不认识 datahub，也不接受 `{表名: Series}` 的 dict

拒绝条件（fail-closed）：
1. 该资产没有状态模型 / 没有边模型快照；
2. 快照的标签签名与该资产当前状态模型不一致；
3. 快照超过 max_age 天未重估（除非 force）。
"""

from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd

from causual.causual_assets import load as load_asset
from causual.causual_core import snapshot
from causual.web.render import build_console
from causual.causual_core.signal_layer import build_bundle

OUT = Path("causual/data/output")


class CardRejected(RuntimeError):
    """出卡前置条件不满足（模型缺失、签名不一致、快照过旧）。"""


def run(
    asset: str,
    panel: pd.DataFrame,
    *,
    freshness: list[dict] | None = None,
    state: dict | None = None,
    asof: str | None = None,
    out_dir: str | Path = OUT,
    max_age: int = 250,
    force: bool = False,
    quiet: bool = False,
) -> Path:
    """出卡。`panel` 是数据，`freshness` 是**装配点算好的新鲜度**（`shared.adapter.freshness`）。

    因果层**不自己判断哪张表过期了**：判定口径（期望期、lag 宽限、`overdue`）在数据层，
    这里只挑行、拼句子。`freshness` 缺省时退化成"不提示数据滞后、归档月用自然月"。
    """
    pkg = load_asset(asset)

    df, spec = pkg.build_panel(asof, panel=panel, state=state)
    try:
        models, snap_meta = snapshot.load(asset, pkg.EDGES)
    except FileNotFoundError as e:
        raise CardRejected(f"没有边快照：{e}") from e

    if snap_meta["label_signature"] != spec["data"]["label_signature"]:
        raise CardRejected(
            "标签体系不一致："
            f"状态模型 {spec['data']['label_signature']} vs 边快照 {snap_meta['label_signature']}；"
            f"先跑 `python -m causual.causual_core.cli edges --asset {asset}`")

    fit_date = pd.Timestamp(snap_meta["fit_date"])
    age = (df.index[-1] - fit_date).days
    if age > max_age and not force:
        raise CardRejected(
            f"边模型快照已 {age} 天未重估（阈值 {max_age}）；"
            f"先跑 `python -m causual.causual_core.cli edges --asset {asset}`")

    # 卡片日期 = **数据截止日**（面板最后一天），不是外推后的标签日
    when = pd.Timestamp(asof) if asof else df.index[-1]

    if str(df.loc[when, pkg.LABEL_COL]) == "":
        raise CardRejected(
            f"{asset} 在 {when.date()} 没有状态标签——调用方传进来的 state 里没有这一天；"
            "检查状态层是否已拟合（python -m state_layer.cli regime）"
            "且标签已外推到最新交易日")

    data_note = _notes(freshness, freq=("D",), limit=6)
    data_notes = [data_note] if data_note else []

    macro_note = _notes(freshness, freq=("M", "Q"), limit=5)
    macro_notes = [macro_note] if macro_note else []

    # 市场状态由调用方算好传来（state["line"]），因果层不碰状态层
    state_line = (state or {}).get("line")

    full = pkg.make_card(df, spec, models, when, data_notes=data_notes,
                         state_line=state_line)
    full_macro = pkg.make_card(df, spec, models, when, data_notes=macro_notes,
                               state_line=state_line)

    from causual.web import render as narrative

    d = when.date()
    cards = Path(out_dir) / asset / "cards" / f"{d:%Y}" / f"{d:%m}"
    cards.mkdir(parents=True, exist_ok=True)

    # 日频卡：市场状态 + 信号层归因
    card = narrative.render(full, keep=("signal",))
    path = cards / f"card_{d}.md"
    path.write_text(card, encoding="utf-8")
    _write_fingerprint(freshness, path, when, kind="card")

    # 月频卡：结构层 + 供需层，按"数据能支撑的最新月"归档
    month = _month_of(freshness)
    my, mm = month.split("-")
    macro_dir = Path(out_dir) / asset / "cards" / my / mm
    macro_dir.mkdir(parents=True, exist_ok=True)
    mac = narrative.render(
        full_macro, keep=("structure", "supply"),
        title=f"# {pkg.TITLE} · 宏观层 · {month}",
        note=f"数据截止 {d}；宏观卡按月归档，本期覆盖 {_month_end(month)} 及之前的数据。")
    macro_path = macro_dir / f"macro_{month}.md"
    macro_path.write_text(mac, encoding="utf-8")
    _write_fingerprint(freshness, macro_path, when, kind="macro")

    if hasattr(pkg, "make_detail_card"):
        detail_sections = pkg.make_detail_card(df, spec, models, when)
        detail = narrative.render(
            detail_sections,
            keep=("edges", "betas", "lags", "contrib", "freshness"))
        detail_path = cards / f"detail_{d}.md"
        detail_path.write_text(detail, encoding="utf-8")
        _write_fingerprint(freshness, detail_path, when, kind="detail")

        macro_detail = narrative.render(
            detail_sections, keep=("supply", "supply_glossary"),
            title=f"# {pkg.TITLE} · 宏观层 · 详情 · {month}")
        (macro_dir / f"macro_detail_{month}.md").write_text(macro_detail, encoding="utf-8")

    if not quiet:
        print(card.split("\n\n")[0])
        print(f"\n日频卡 -> {path}")
        print(f"月频卡 -> {macro_path}")
        print(f"（{asset} 签名 {spec['data']['label_signature']} / 边快照 {snap_meta['fit_date']}"
              f" / 龄期 {age} 天）")
    return Path(path)


def _month_end(month: str) -> str:
    y, m = map(int, month.split("-"))
    nxt = pd.Timestamp(year=y + (m == 12), month=(m % 12) + 1, day=1)
    return str((nxt - pd.Timedelta(days=1)).date())


def _notes(rows: list[dict] | None, *, freq: tuple[str, ...],
           limit: int, overdue_only: bool = True) -> str:
    """把新鲜度行拼成卡片里那一句「数据滞后」。

    **格式与旧实现逐字一致**（`{表} 停在 {actual}，期望至少 {expect}`，按落后天数
    降序、取前 `limit` 条、`；` 连接，超出的追加 `；等 N 张表`）——卡片上的措辞
    不要改，用户看惯了。

    `overdue_only=True`（默认）：只报**宽限已过**的，也就是旧实现的判定
    （旧代码先要求"今天 >= 期望期 + lag_days"才纳入）。`False` 连"宽限内、源还没
    发布"的一起报，每句后面加一句区分。两个口径差多少，见 CHANGES v5.51。
    """
    items = [r for r in (rows or [])
             if r["freq"] in freq and r["stale"] and (r["overdue"] or not overdue_only)]
    if not items:
        return ""
    items = sorted(items, key=lambda r: -int(r.get("days_behind") or 0))

    def _line(r: dict) -> str:
        s = f"{r['table']} 停在 {r['actual']}，期望至少 {r['expect']}"
        if not overdue_only:
            s += "（宽限已过，疑似未更新）" if r["overdue"] else "（源尚未发布）"
        return s

    head = "；".join(_line(r) for r in items[:limit])
    if len(items) > limit:
        head += f"；等 {len(items)} 张表"
    return head


def _month_of(freshness: list[dict] | None) -> str:
    """月频卡的月份 = 月/季频表的 `actual` 最大值所在月。

    用"数据能支撑的最新月"而不是自然月：11-01 时 CPI 还停在 9 月，
    这时候出「11 月宏观卡」是自欺欺人。

    口径只有一份：`shared.adapter.freshness(panel)` 返回的 `freq` / `actual`。
    以前这里配一份手写的 `MONTH_TABLES`，漂过两次（表改名后 8 个名字只命中 2 个，
    于是 A 股的归档月其实由黄金的表决定）。现在 freshness 是按**本资产的面板**算的，
    天然收窄，不需要再按 `REQUIRED_TABLES` 过滤——这就是为什么本函数不再收 `required`。

    必须与 `CausalPort.exists()` 用同一个口径：`macro_month → exists(asset, month)
    → 生成` 是一条幂等链，两边算出不同月份会让同一张卡被反复重生成。
    """
    rows = [r for r in (freshness or []) if r["freq"] in ("M", "Q")]
    if not rows:
        print("[月频卡] 没有月/季频表的新鲜度信息，归档月退回自然月", file=sys.stderr)
        return pd.Timestamp.today().strftime("%Y-%m")
    return max(r["actual"] for r in rows)[:7]


def _write_fingerprint(freshness: list[dict] | None, path: Path, when,
                       kind: str = "card") -> None:
    """记录这张卡用到的表各自停在哪一天，供"是否需要重生成"判断。

    表集合 = 该面板里对应频率的表（`freq` 由数据层给），不再依赖手写清单。
    """
    import json

    want = ("M", "Q") if kind == "macro" else ("D",)
    fp: dict[str, str] = {r["table"]: r["actual"]
                          for r in (freshness or []) if r["freq"] in want}
    data = {"kind": kind, "date": str(pd.Timestamp(when).date()),
            "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
            "tables": fp}
    path.with_suffix(".meta.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

