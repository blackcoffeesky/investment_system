"""维护 CLI：拟合状态模型 / 拟合边模型。

    python -m causual.causual_core.cli regime --asset gold [--k 5]
    python -m causual.causual_core.cli edges  --asset gold
    python -m causual.causual_core.cli audit  --asset gold
"""

from __future__ import annotations

import argparse

from causual.causual_assets import load as load_asset


def cmd_regime(args) -> int:
    pkg = load_asset(args.asset)
    df = pkg.prepare(pkg.load_raw())
    res = pkg.fit_regime(df, k=args.k, seed=args.seed, verbose=True)
    p = pkg.save_regime(res)
    spec = res["spec"]
    print(f"\n状态模型 -> {p}")
    print(f"  K={spec['k']}  签名={spec['label_signature']}  用到标签={spec['used_labels']}")
    return 0


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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="causual.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, fn in (("regime", cmd_regime), ("edges", cmd_edges), ("audit", cmd_audit)):
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
