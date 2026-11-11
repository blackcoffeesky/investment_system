"""state_layer 命令行：拟合状态模型。

    python -m state_layer.cli regime --asset ashare [--k 3] [--seed 42]

**这是状态层唯一的写入入口**。产物写到 `state_layer/saved_models/{asset}/`。

数据由状态层自己声明、自己取：`state_assets.<asset>.REQUIRED_TABLES`，
不再借用因果层的面板函数。
"""

from __future__ import annotations

import argparse

from state_layer import state_assets
from state_layer import fit as fit_mod


def load_series(asset: str) -> tuple[dict, object]:
    """按资产包声明的表清单，从数据层读成 {表名: Series}。"""
    from datahub.port import get_port

    port = get_port()
    pkg = state_assets.load(asset)
    series: dict = {}
    for t in pkg.REQUIRED_TABLES:
        s = port.read(t)
        if s is None:
            continue
        if hasattr(s, "columns"):
            for c in s.columns:
                series[f"{t}:{c}"] = s[c]
        else:
            series[t] = s
    return series, port.metadata()


def cmd_regime(args) -> int:
    pkg = state_assets.load(args.asset)
    series, meta = load_series(args.asset)
    panel = pkg.panel(series, meta)

    res = fit_mod.fit(args.asset, panel, labeler=pkg.config.name_states,
                      k=args.k, seed=args.seed, verbose=True)
    p = fit_mod.save(args.asset, res)

    spec = res["spec"]
    m, d, mt = spec["model"], spec["data"], spec["metrics"]
    print(f"\n状态模型 -> {p}")
    print(f"  {m['version']}  签名={d['label_signature']}  "
          f"用到标签={d['used_labels']}")
    print(f"  分离度（{mt['method']}）= {mt['min']:.3f}  "
          f"{'通过' if mt['passed'] else '未达标'}")
    print("\n[注意] 重拟合会改变 label_signature，边快照随之失效——"
          "接着跑 `python -m causual.causual_core.cli edges --asset "
          f"{args.asset}`，否则卡片会因签名不一致被拒绝。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="state_layer.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("regime", help="拟合状态模型（HMM + 极端规则）")
    r.add_argument("--asset", required=True)
    r.add_argument("--k", default=None,
                   help="状态数；默认用配置里的 K，传 auto 让数据自己选")
    r.add_argument("--seed", type=int, default=42)
    r.set_defaults(func=cmd_regime)

    a = ap.parse_args(argv)
    if getattr(a, "k", None) == "auto":
        a.k = "auto"
    elif getattr(a, "k", None) is not None:
        a.k = int(a.k)
    return int(a.func(a))


if __name__ == "__main__":
    raise SystemExit(main())
