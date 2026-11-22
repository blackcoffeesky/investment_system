"""causual 的命令行入口：拟合、审计、出卡。

    python -m causual.causual_core.cli regime --asset gold [--k 5]
    python -m causual.causual_core.cli edges  --asset gold
    python -m causual.causual_core.cli audit  --asset gold
    python -m causual.causual_core.cli daily  --asset gold [--asof 2026-11-03]

**这里是唯一允许 import 数据层的地方**：库代码只吃传进来的数据，
装配（从库里读表）属于入口的职责。
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd

from causual.causual_assets import load as load_asset


def cmd_regime(args) -> int:
    """已迁移到状态层：`python -m state_layer.cli regime --asset X`。

    这里保留转发只是为了不打断旧脚本/书签。
    """
    from state_layer.cli import main as state_main

    print("[提示] regime 拟合已归状态层：python -m state_layer.cli regime")
    argv = ["regime", "--asset", args.asset, "--seed", str(args.seed)]
    if args.k is not None:
        argv += ["--k", str(args.k)]
    return state_main(argv)


def cmd_edges(args) -> int:
    """重估边模型。

    `build_panel` 需要状态层的 spec（标签口径），所以这里和 `cmd_daily` 一样
    先把状态视图装配好——CLI 是允许跨模块取数的唯一地点。
    """
    pkg = load_asset(args.asset)
    panel = _load_panel(args.asset)
    view = state_view(args.asset, panel)
    df, spec = pkg.build_panel(args.asof, panel, state=view)
    pkg.fit_edges(df, spec, seed=args.seed, verbose=True)
    return 0


def cmd_audit(args) -> int:
    from causual.causual_core.edge import index as edge_index
    from causual.causual_core.evaluate import sign_audit
    from causual.causual_core import snapshot

    pkg = load_asset(args.asset)
    models, meta = snapshot.load(args.asset, pkg.EDGES)
    print(f"快照 {meta['path']}  fit_date={meta['fit_date']}  sig={meta['label_signature']}")
    tbl = sign_audit(models)
    print(tbl.round(4).to_string(index=False) if len(tbl) else "（无估计）")
    conflicts = tbl[(tbl["一致性"] == "冲突") & (tbl["显著"] == "是")] if len(tbl) else tbl
    print(f"\n显著冲突 {len(conflicts)} 条")
    if len(conflicts):
        print(conflicts[["edge", "label", "理论", "估计", "估计值", "n"]].to_string(index=False))
    return 0


def _load_panel(asset: str) -> pd.DataFrame:
    """命令行入口负责装配数据：**数据只在这里进来**（`AGENTS.md` §4'③）。

    资产包声明自己要哪个面板（`pkg.PANEL_KIND`），causual 的核心代码只收 DataFrame。
    """
    from shared.adapter import get_data

    return get_data(load_asset(asset).PANEL_KIND)


def _freshness(asset: str, panel: pd.DataFrame, asof: str | None = None) -> list[dict]:
    """新鲜度由**数据层**算（`shared.adapter.freshness`），本模块只挑行。

    `today` 传**卡片日**（数据截止日 / `--asof`）而不是"真实今天"：这样
    "这张卡出的时候数据到哪了"是可复现的，重跑历史 `--asof` 也不会因为过了几天
    就凭空多出告警。
    """
    from shared.adapter import freshness as panel_freshness

    when = str(asof)[:10] if asof else str(pd.DatetimeIndex(panel.index)[-1].date())
    return panel_freshness(load_asset(asset).PANEL_KIND, today=when)


def state_view(asset: str, panel: pd.DataFrame) -> dict:
    """装配状态视图：`state_view` + **画像表**（三个装配点共用）。

    为什么画像不跟 `state_view` 一起返回：v2 的 `spec` 里**没有** profile，
    画像要按标签对观测面板做统计（`StatePort.profile(asset, panel)`），是"现算"；
    而 `state_view` 的契约是"一次给全状态标签那一套"。causual 不 import 状态层，
    所以由装配点补上这一项，核心代码只读 `state["profile"]`。
    """
    from state_layer.port import get_port as state_port

    sp = state_port()
    view = sp.state_view(asset, panel)
    prof = sp.profile(asset, panel)
    # 端口给的表是 `0..K-1` 的数字索引 + 一列 `label`（标签名）；卡片按标签名做索引，
    # 所以这里转成 `index=label`（legacy spec 里的 profile 本来就是这么存的）。
    if isinstance(prof, pd.DataFrame) and "label" in prof.columns:
        prof = prof.set_index(prof["label"].astype(str)).drop(columns=["label"])
    view["profile"] = prof
    return view


def cmd_daily(args) -> int:
    from causual.causual_core import pipeline

    # CLI 是允许直接取数的：这里把面板和状态视图一起装配好传下去
    panel = _load_panel(args.asset)
    view = state_view(args.asset, panel)
    try:
        pipeline.run(args.asset, panel, freshness=_freshness(args.asset, panel, args.asof),
                     state=view, asof=args.asof, force=getattr(args, "force", False))
    except pipeline.CardRejected as e:
        print(f"[拒绝] {e}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="causual.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, fn in (("regime", cmd_regime), ("edges", cmd_edges),
                     ("audit", cmd_audit), ("daily", cmd_daily)):
        p = sub.add_parser(name)
        p.add_argument("--asset", required=True)
        p.add_argument("--k", type=int, default=None)
        p.add_argument("--seed", type=int, default=42)
        p.add_argument("--asof", default=None)
        p.set_defaults(func=fn)

    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
