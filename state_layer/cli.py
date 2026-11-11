"""state_layer 命令行：拟合状态模型。

    python -m state_layer.cli regime --asset ashare [--k 3] [--seed 42]

**这是状态层唯一的写入入口**。产物写到 `state_layer/models/{asset}/`。
"""

from __future__ import annotations

import argparse
import importlib

from state_layer import config as cfg
from state_layer import fit as fit_mod
from state_layer import store


def _panel_provider(asset: str):
    """按配置里的点号路径找到原始面板构造函数。"""
    spec = cfg.get(asset).PANEL_PROVIDER
    mod_name, _, fn_name = spec.partition(":")
    mod = importlib.import_module(mod_name)
    return mod, getattr(mod, fn_name)


def _load_series(asset: str, provider_mod) -> tuple[dict, object]:
    """按面板提供方声明的表清单，从数据层读成 {表名: Series}。"""
    from datahub.port import get_port

    port = get_port()
    tables = getattr(provider_mod, "REQUIRED_TABLES", ())
    series: dict = {}
    for t in tables:
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
    provider_mod, load_raw = _panel_provider(args.asset)
    series, meta = _load_series(args.asset, provider_mod)

    # 标签词汇表在 config 里（与 K / 特征同一份配置）
    label_mod = importlib.import_module(f"state_layer.config.{args.asset}")

    panel = load_raw(series, meta)
    res = fit_mod.fit(args.asset, panel, labeler=label_mod.name_states,
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
    r.add_argument("--k", type=int, default=None)
    r.add_argument("--seed", type=int, default=42)
    r.set_defaults(func=cmd_regime)

    a = ap.parse_args(argv)
    return int(a.func(a))


if __name__ == "__main__":
    raise SystemExit(main())
