"""装配：跑一个资产的 `estimator`，把结果落成 v2 三件套。

用法（**手动触发**；调度/触发方式待定，2026-11-22 用户决定不在这里做）：

    from state_layer.fit import fit_asset
    fit_asset("ashare")

    # 或命令行
    python -m state_layer.cli fit --asset ashare

`panel=None` 时**在函数内部**从 `shared.adapter` 取数——`shared` 是公共耦合层，
状态层其余部分（model / features / analysis）仍然不取数。
"""

from __future__ import annotations

import pandas as pd

from state_layer import state_assets, store
from state_layer.state_assets import base


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
