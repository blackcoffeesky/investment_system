"""出卡片：面板 → 信号 → 叙事 → 落盘。不拟合任何模型。

    python -m causual.causual_core.cli daily --asset ashare
    python -m causual.causual_core.cli daily --asset gold --asof 2026-11-03

数据从**外面传进来**（`series` + `meta`），这里不碰数据层：
- service 走 `ctx.services["datahub"]` 读好再传
- 命令行由 `cli.py` 负责装配

拒绝条件（fail-closed）：
1. 该资产没有状态模型 / 没有边模型快照；
2. 快照的标签签名与该资产当前状态模型不一致；
3. 快照超过 max_age 天未重估（除非 force）。
"""

from __future__ import annotations

from pathlib import Path

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
    series: dict,
    meta: pd.DataFrame | None = None,
    *,
    state: dict | None = None,
    asof: str | None = None,
    out_dir: str | Path = OUT,
    max_age: int = 250,
    force: bool = False,
    quiet: bool = False,
) -> Path:
    pkg = load_asset(asset)

    df, spec = pkg.build_panel(asof, series=series, meta=meta, state=state)
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

    from causual.causual_core import freshness

    data_notes: list[str] = []
    # 清单按资产收窄：metadata 是全局的，不收窄就会把黄金表报到 A 股卡上
    daily_tables = freshness.scoped(freshness.WATCH, pkg.REQUIRED_TABLES)
    stale = freshness.stale_only(meta, tables=daily_tables,
                                 today=pd.Timestamp(when))
    if stale:
        data_notes.append(freshness.summary(stale, limit=6))

    macro_notes: list[str] = []
    macro_tables = freshness.scoped(freshness.WATCH_MACRO, pkg.REQUIRED_TABLES)
    stale_m = freshness.stale_only(meta, tables=macro_tables,
                                   today=pd.Timestamp(when))
    if stale_m:
        macro_notes.append(freshness.summary(stale_m, limit=5))

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
    _write_fingerprint(meta, path, when, kind="card")

    # 月频卡：结构层 + 供需层，按"数据能支撑的最新月"归档
    month = _month_of(meta)
    my, mm = month.split("-")
    macro_dir = Path(out_dir) / asset / "cards" / my / mm
    macro_dir.mkdir(parents=True, exist_ok=True)
    mac = narrative.render(
        full_macro, keep=("structure", "supply"),
        title=f"# {pkg.TITLE} · 宏观层 · {month}",
        note=f"数据截止 {d}；宏观卡按月归档，本期覆盖 {_month_end(month)} 及之前的数据。")
    macro_path = macro_dir / f"macro_{month}.md"
    macro_path.write_text(mac, encoding="utf-8")
    _write_fingerprint(meta, macro_path, when, kind="macro")

    if hasattr(pkg, "make_detail_card"):
        detail_sections = pkg.make_detail_card(df, spec, models, when)
        detail = narrative.render(
            detail_sections,
            keep=("edges", "betas", "lags", "contrib", "freshness"))
        detail_path = cards / f"detail_{d}.md"
        detail_path.write_text(detail, encoding="utf-8")
        _write_fingerprint(meta, detail_path, when, kind="detail")

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


def _month_of(meta: pd.DataFrame | None) -> str:
    """月频卡的月份 = 主要宏观表 end_date 的最大值所在月。

    用"数据能支撑的最新月"而不是自然月：11-01 时 CPI 还停在 9 月，
    这时候出「11 月宏观卡」是自欺欺人。

    清单来自 `freshness.MONTH_TABLES`（**只有一份**）。原先这里硬编码了
    `cn_cpi / cn_ppi / cn_m2 / cn_exports` —— 那些名字数据层早就不用了，
    实际只靠 `cb_holdings` / `gold_etf_holdings` 命中，于是 **A 股的归档月
    是由黄金的表决定的**（凑巧都是 09-30 才没露出来）。

    ⚠️ **仍然不分资产**：`CausalPort.macro_month(meta)` 没有 `asset` 参数，
    而 service 的幂等链是「`macro_month(meta)` 给月份 → `exists(asset, month)`
    判断要不要生成 → 生成」。如果这里按资产收窄，gold 会算出 09 而
    `exists` 还在问 10，**那张卡会被反复重生成**。
    所以把两边一起改（port 加 `asset` 参数 + service 调用点）才能收窄；
    在那之前，这里返回**两个资产宏观表的并集月**（当前 = 2026-10，
    对黄金偏乐观一个月）。
    """
    from causual.causual_core import freshness

    ends: list[str] = []
    if meta is not None and len(meta):
        idx = meta.set_index("name")
        for t in freshness.MONTH_TABLES:
            if t in idx.index and idx.loc[t, "end_date"]:
                ends.append(str(idx.loc[t, "end_date"]))
    if not ends:
        print("[月频卡] 没有任何宏观表在 metadata 里，归档月退回自然月",
              file=sys.stderr)
        return pd.Timestamp.today().strftime("%Y-%m")
    return max(ends)[:7]


def _write_fingerprint(meta: pd.DataFrame | None, path: Path, when,
                       kind: str = "card") -> None:
    """记录这张卡用到的表各自停在哪一天，供"是否需要重生成"判断。"""
    import json

    from causual.causual_core import freshness

    fp: dict[str, str] = {}
    if meta is not None and len(meta):
        tables = freshness.WATCH_MACRO if kind == "macro" else freshness.WATCH
        for _, row in meta.iterrows():
            if row["name"] in tables and row.get("end_date"):
                fp[str(row["name"])] = str(row["end_date"])
    data = {"kind": kind, "date": str(pd.Timestamp(when).date()),
            "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
            "tables": fp}
    path.with_suffix(".meta.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def console(asset: str, series: dict, meta: pd.DataFrame | None = None,
            asof: str | None = None) -> str:
    """命令行预览（不落盘）。"""
    pkg = load_asset(asset)
    df, spec = pkg.build_panel(asof, series=series, meta=meta, state=state)
    models, _ = snapshot.load(asset, pkg.EDGES)
    bundle = build_bundle(
        df, models, pkg.EDGES, label_col=pkg.LABEL_COL,
        treatment_col=pkg.TREATMENT_COL, target=pkg.C.TARGET,
        signal_lag=pkg.SIGNAL_LAG)
    bundle.label_signature = spec["data"]["label_signature"]
    when = pd.Timestamp(asof) if asof else df.index[-1]
    return build_console(df, when, models, bundle, pkg.EDGES, spec=spec)
