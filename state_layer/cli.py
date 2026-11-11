"""state_layer 命令行：拟合状态模型。

    python -m state_layer.cli regime --asset ashare [--k 3] [--seed 42]

**这是状态层唯一的写入入口**。产物仍写到 `causual/data/regime/{asset}/`
与 `regime_engine/saved_models/{asset}/`（路径不变，减少迁移风险），
但写的人从此是状态层。
"""

from __future__ import annotations

import argparse
import importlib

from state_layer import config as cfg
from state_layer import inference


def _panel_provider(asset: str):
    """按配置里的点号路径找到面板构造函数。"""
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
    provider_mod, prepare = _panel_provider(args.asset)
    series, meta = _load_series(args.asset, provider_mod)

    # 标签词汇表在资产包里（label 模块），状态层只是使用者
    label_mod = importlib.import_module(f"causual.causual_assets.{args.asset}.label")

    panel = prepare(provider_mod.load_raw(series, meta))
    res = inference.fit(args.asset, panel, label_module=label_mod,
                        verbose=True)
    if args.k is not None:
        # 显式指定 K 时重跑一次（fit 内部默认用配置里的 K）
        from state_layer.features import build
        from state_layer.estimators import make

        c = cfg.get(args.asset)
        est = make("hmm", k=args.k, seed=args.seed)
        res = est.fit(build(args.asset, panel),
                      labeler_factory=label_mod.name_states,
                      name_by_value=c.LABEL_NAME,
                      signature_fn=label_mod.signature,
                      asset=args.asset, extreme_params=c.EXTREME_PARAMS,
                      verbose=True)

    p = inference.save(args.asset, res)
    spec = res["spec"]
    print(f"\n状态模型 -> {p}")
    print(f"  K={spec['k']}  签名={spec['label_signature']}  "
          f"用到标签={spec['used_labels']}")
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
