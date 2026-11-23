"""state_layer 命令行：拟合状态模型。

    python -m state_layer.cli fit --asset ashare

**这是状态层唯一的写入入口**。产物写到 `state_layer/saved_models/{asset}/`：
`spec.json` / `groups.csv` / `params.pkl`（v2 多分组格式，见 `store.py`）。

数据由耦合层统一提供（`get_data(Panel.STATE_<asset>)`）：状态层不自己拼面板，
`config.REQUIRED_TABLES` 只用于 `port.required_tables()` 的装配清单。

触发方式（定时 / 数据新鲜度）**待定**，当前一律人工执行——见用户 2026-11-22 决定。
"""

from __future__ import annotations

import argparse

import pandas as pd

from shared.adapter import get_data

from state_layer import state_assets, store
from state_layer.model import base


def fit_asset(asset: str, panel: pd.DataFrame | None = None,
              verbose: bool = True) -> dict:
    """训练 + 落盘。返回 `{asset, groups, spec}`。"""
    pkg = state_assets.load(asset)
    est = pkg.estimator()
    if panel is None:
        from shared.adapter import get_data

        # 取**全历史**（不传 start），所以特征预热天然覆盖。
        # 需要按区间取数的装配点（`web/routes.py`）必须把 `start` 往前推
        # `state_assets.base.needed_bars(...)` 个交易日，否则头部整段 NaN（不报错）。
        panel = get_data(state_assets.panel_of(asset))

    est = pkg.estimator()
    results = est.fit(panel)
    spec = est.spec()

    path = store.save_estimate(asset, spec=spec, params=est.to_params(),
                               groups=base.to_long_df(results))
    if verbose:
        summary = ", ".join(
            f"{n}(k={g.k}, {len(g.states)} 天)" for n, g in results.items())
        print(f"[state] {asset} 拟合完成：{summary} → {path}")
    return {"asset": asset, "groups": list(results), "spec": spec}


def cmd_fit(args) -> int:
    if args.k is not None:
        print("[state] --k 已退休：K 由 config.K 决定（自动选 K 于 2026-11-22 移除）")
        return 2
    panel = get_data(state_assets.panel_of(args.asset))
    res = fit_asset(args.asset, panel, verbose=True)

    spec = res["spec"]
    print(f"\n分组 → {list(spec.get('groups') or {})}  primary={spec.get('primary')}")
    for name, g in (spec.get("groups") or {}).items():
        print(f"  {name}: k={g.get('k')} 特征={list(g.get('features') or [])} "
              f"fit_id={g.get('fit_id')}")
    print("\n[注意] 重拟合会改变 label_signature，边快照随之失效——"
          "接着跑 `python -m causual.causual_core.cli edges --asset "
          f"{args.asset}`，否则卡片会因签名不一致被拒绝。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="state_layer.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, help_ in (("fit", "拟合状态模型（estimator 驱动，落 v2 三件套）"),
                        ("regime", "同 fit（旧命令名，保留兼容）")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--asset", required=True)
        p.add_argument("--k", default=None,
                       help="已退休：K 由 config 决定；传任何值都会报错退出")
        p.set_defaults(func=cmd_fit)

    a = ap.parse_args(argv)
    return int(a.func(a))


if __name__ == "__main__":
    raise SystemExit(main())
