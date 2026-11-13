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
    pkg = load_asset(args.asset)
    df, spec = pkg.build_panel(args.asof)
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


def _load_series(asset: str, state=None):
    """命令行入口负责装配数据：causual 库代码不碰数据层，入口文件可以。

    传了 `state` 端口就把状态层要的表一起读了（两边共用一份快照）。
    """
    from datahub.port import get_port

    pkg = load_asset(asset)
    port = get_port()
    tables = list(pkg.REQUIRED_TABLES)
    if state is not None:
        tables += [t for t in state.required_tables(asset) if t not in tables]
    return port.series_map(tables), port.metadata()


def cmd_daily(args) -> int:
    from causual.causual_core import pipeline

    # CLI 是允许直接取数的：这里把状态视图一起装配好传下去
    from state_layer.port import get_port as state_port

    sp = state_port()
    series, meta = _load_series(args.asset, state=sp)
    view = sp.causual_view(args.asset, series, meta)
    try:
        pipeline.run(args.asset, series, meta, state=view, asof=args.asof,
                     force=getattr(args, "force", False))
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
