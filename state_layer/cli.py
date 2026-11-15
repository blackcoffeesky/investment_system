"""state_layer 命令行：拟合状态模型。

    python -m state_layer.cli regime --asset ashare [--k 3|auto] [--seed 42]

**这是状态层唯一的写入入口**。产物写到 `state_layer/saved_models/{asset}/`。

数据由耦合层统一提供（`get_data(Panel.STATE_<asset>)`）：状态层不再自己拼面板，
`config.REQUIRED_TABLES` 只用于 `port.required_tables()` 的装配清单。
"""

from __future__ import annotations

import argparse

from shared.adapter import get_data

from state_layer import state_assets
from state_layer import fit as fit_mod


def cmd_regime(args) -> int:
    pkg = state_assets.load(args.asset)
    panel = get_data(state_assets.panel_of(args.asset))

    # 只覆盖用户**显式**给的参数，没给就沿用 config.MODEL_PARAMS。
    # `--k auto` 已在 main() 里转成 None —— hmm 的 k=None 分支才是"让模型按 BIC 选"。
    over: dict[str, object] = {}
    if args.k is not None:
        over["k"] = args.k
    if args.seed is not None:
        over["seed"] = args.seed

    res = fit_mod.fit_pipeline(args.asset, panel,
                               labeler=pkg.config.name_states, verbose=True,
                               params={"hmm": over} if over else None)
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
                   help="状态数；不给用配置里的 K，传 auto 让模型按 BIC 自己选")
    r.add_argument("--seed", type=int, default=None,
                   help="HMM 随机种子；不给用配置/默认值 42")
    r.set_defaults(func=cmd_regime)

    a = ap.parse_args(argv)
    if a.k == "auto":
        a.k = None          # None 才是"自己选 K"；以前这里写成了自赋值，等于没做
    elif a.k is not None:
        a.k = int(a.k)
    return int(a.func(a))


if __name__ == "__main__":
    raise SystemExit(main())
