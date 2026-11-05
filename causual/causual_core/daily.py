"""核心日常入口：出卡片。不拟合任何模型。

    python -m causual.causual_core.daily --asset gold
    python -m causual.causual_core.daily --asset ashare --asof 2026-10-16

拒绝条件（fail-closed）：
1. 该资产没有状态模型 / 没有边模型快照；
2. 快照的标签签名与该资产当前状态模型不一致；
3. 快照超过 max_age 天未重估（除非 --force）。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from causual.causual_core import snapshot
from causual.causual_core.narrative import build_console
from causual.causual_core.signal_layer import build_bundle
from causual.causual_assets import load as load_asset

OUT = Path("causual/output")


def run(
    asset: str,
    *,
    asof: str | None = None,
    out_dir: str | Path = OUT,
    max_age: int = 250,
    force: bool = False,
    verbose: bool = True,
    quiet: bool = False,
    return_path: bool = False,
):
    pkg = load_asset(asset)

    df, spec = pkg.build_panel(asof)
    try:
        models, meta = snapshot.load(asset, pkg.EDGES)
    except FileNotFoundError as e:
        print(f"[拒绝] {e}")
        return 1

    if meta["label_signature"] != spec["label_signature"]:
        print("[拒绝] 标签体系不一致：")
        print(f"   状态模型 {spec['label_signature']}  vs  边快照 {meta['label_signature']}")
        print(f"   -> 需要重估边模型：python -m causual.causual_core.edges fit --asset {asset}")
        return 2

    fit_date = pd.Timestamp(meta["fit_date"])
    age = (df.index[-1] - fit_date).days
    if age > max_age and not force:
        print(f"[拒绝] 边模型快照已 {age} 天未重估（阈值 {max_age}）。")
        print(f"   -> python -m causual.causual_core.edges fit --asset {asset}")
        return 3

    # 卡片日期 = **数据截止日**（面板最后一天），不是外推后的标签日。
    # 外推只是让状态延续到"今天"，但卡片上所有数字都来自数据截止日。
    when = pd.Timestamp(asof) if asof else df.index[-1]

    # 兜底：当日没有状态标签就说明模型产物不全，宁可报错也不出一张空卡
    if str(df.loc[when, pkg.LABEL_COL]) == "":
        raise RuntimeError(
            f"{asset} 在 {when.date()} 没有状态标签——"
            "多半是 regime_engine/saved_models/ 下的 hmm_last.pkl 缺失或与状态模型不一致，"
            "先跑 `python -m causual.causual_core.cli regime --asset "
            f"{asset}`。"
        )

    # 数据新鲜度：指数最新不代表信号层的日频表也最新
    data_notes: list[str] = []
    try:
        from datahub.fin import metadata as _meta

        from causual.causual_core import freshness

        stale = freshness.stale_only(_meta(), today=pd.Timestamp(when))
        if stale:
            data_notes.append(freshness.summary(stale, limit=6))
    except Exception:  # noqa: BLE001
        pass
    full = pkg.make_card(df, spec, models, when, data_notes=data_notes)

    # 宏观层自己有新鲜度清单（月/季频），不能沿用日频那几条
    macro_notes: list[str] = []
    try:
        from datahub.fin import metadata as _meta2

        from causual.causual_core import freshness as _fr

        stale_m = _fr.stale_only(_meta2(), tables=_fr.WATCH_MACRO,
                                 today=pd.Timestamp(when))
        if stale_m:
            macro_notes.append(_fr.summary(stale_m, limit=5))
    except Exception:  # noqa: BLE001
        pass
    full_macro = pkg.make_card(df, spec, models, when, data_notes=macro_notes)

    d = when.date()
    cards = Path(out_dir) / asset / "cards" / f"{d:%Y}" / f"{d:%m}"
    cards.mkdir(parents=True, exist_ok=True)

    from causual.causual_core import narrative

    # 日频卡：市场状态 + 信号层归因
    card = narrative.render(full, keep=("state", "signal"))
    path = cards / f"card_{d}.md"
    path.write_text(card, encoding="utf-8")
    _write_fingerprint(asset, path, when, kind="card")

    # 月频卡：结构层 + 供需层。按"数据能支撑的最新月"命名，每月一张。
    month = _month_of(asset)
    mac = narrative.render(
        full_macro, keep=("structure", "supply"),
        title=f"# {pkg.TITLE} · 宏观层 · {month}",
        note=f"数据截止 {d}；宏观卡按月归档，本期覆盖 {_month_end(month)} 及之前的数据。")
    my, mm = month.split("-")
    macro_dir = Path(out_dir) / asset / "cards" / my / mm
    macro_dir.mkdir(parents=True, exist_ok=True)
    macro_path = macro_dir / f"macro_{month}.md"
    macro_path.write_text(mac, encoding="utf-8")
    _write_fingerprint(asset, macro_path, when, kind="macro")

    detail_path = None
    if hasattr(pkg, "make_detail_card"):
        detail_sections = pkg.make_detail_card(df, spec, models, when)
        detail = narrative.render(
            detail_sections,
            keep=("model", "edges", "betas", "lags", "contrib", "freshness"))
        detail_path = cards / f"detail_{d}.md"
        detail_path.write_text(detail, encoding="utf-8")
        _write_fingerprint(asset, detail_path, when, kind="detail")
        macro_detail = narrative.render(
            detail_sections, keep=("supply", "supply_glossary"),
            title=f"# {pkg.TITLE} · 宏观层 · 详情 · {month}")
        (macro_dir / f"macro_detail_{month}.md").write_text(macro_detail, encoding="utf-8")

    if verbose and not quiet:
        print(card.split("\n\n")[0])
        print(f"\n日频卡 -> {path}")
        print(f"月频卡 -> {macro_path}")
        if detail_path:
            print(f"详情卡 -> {detail_path}")
        print(f"（{asset} 状态模型签名 {spec['label_signature']} / 边快照 {meta['fit_date']} / 龄期 {age} 天）")
    return path if return_path else 0


def _month_end(month: str) -> str:
    """'2026-10' → '2026-10-31'"""
    y, m = map(int, month.split("-"))
    nxt = pd.Timestamp(year=y + (m == 12), month=(m % 12) + 1, day=1)
    return str((nxt - pd.Timedelta(days=1)).date())


def _month_of(asset: str) -> str:
    """月频卡的月份 = 主要宏观表 end_date 的最大值所在月。

    用"数据能支撑的最新月"而不是自然月：11-01 时 CPI 还停在 9 月，
    这时候出「11 月宏观卡」是自欺欺人。
    """
    from datahub.fin import metadata

    macro_tables = ("cn_cpi", "cn_ppi", "cn_m2", "cn_exports", "cb_holdings",
                    "gold_etf_holdings")
    try:
        meta = metadata().set_index("name")
        ends = [str(meta.loc[t, "end_date"]) for t in macro_tables
                if t in meta.index and meta.loc[t, "end_date"]]
    except Exception:  # noqa: BLE001
        ends = []
    if not ends:
        return pd.Timestamp.today().strftime("%Y-%m")
    return max(ends)[:7]


def _write_fingerprint(asset: str, path: Path, when, kind: str = "card") -> None:
    """记录这张卡用到的表各自停在哪一天，供"是否需要重生成"判断。"""
    import json

    try:
        from datahub.fin import metadata
        from causual.causual_core import freshness

        meta = metadata()
        tables = freshness.WATCH if kind != "macro" else freshness.WATCH_MACRO
        fp = {t: str(row["end_date"]) for _, row in meta.iterrows()
              for t in [row["name"]] if t in tables and row.get("end_date")}
    except Exception:  # noqa: BLE001
        fp = {}
    data = {"asset": asset, "kind": kind, "date": str(when.date()),
            "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
            "tables": fp}
    p = path.with_suffix(".meta.json")
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="causual.daily", description="日常出卡片")
    ap.add_argument("--asset", required=True, help="gold / ashare")
    ap.add_argument("--asof", default=None)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--max-age", type=int, default=250)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    return run(args.asset, asof=args.asof, out_dir=args.out,
               max_age=args.max_age, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
