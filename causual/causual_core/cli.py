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


def derived_series(asset: str, port) -> dict[str, pd.Series]:
    """数据层**现算的派生量** → 按 `"表名:列名"` 塞进 series。

    目前只有 A 股的股息率（`DataPort.dividend_yield()`，年度，百分数）。

    ⚠️ 两件事必须记住：
    1. **一次拿全表**：这个调用要扫一遍 `stock_daily`，约 3.7 秒——**不要按年循环**。
    2. **年度值对齐到"次年 1 月 1 日"**：年 Y 的除息日全落在 Y 内，数据到 Y+1 才完整，
       所以 Y 年只能看到 Y-1 的值（防前视）。面板那边只做 ffill。

    取不到就返回空 dict 并**打印原因**——面板会把该列置 NaN，卡片上那一行显示
    「无数据」，不会静默变成"这一行不存在"。
    """
    if asset != "ashare":
        return {}
    try:
        dy = port.dividend_yield()
    except Exception as exc:  # noqa: BLE001
        print(f"[causual] 股息率派生量取不到（{type(exc).__name__}: {exc}）；"
              "DIVIDEND_YIELD 会是整列 NaN", file=sys.stderr)
        return {}
    if dy is None or len(dy) == 0:
        print("[causual] 股息率派生量是空的；DIVIDEND_YIELD 会是整列 NaN",
              file=sys.stderr)
        return {}

    # 只装用到的那一列。将来要 `yield_median`（股息率中位数）就在这里多塞一行——
    # 同一次调用已经把三列都拿回来了，不额外花钱。
    col = "yield_mcap_weighted"
    if col not in dy.columns:
        print(f"[causual] 股息率派生量里没有 {col}（有 {list(dy.columns)}）；"
              "DIVIDEND_YIELD 会是整列 NaN", file=sys.stderr)
        return {}

    s = pd.to_numeric(dy[col], errors="coerce").dropna()
    if s.empty:
        print(f"[causual] 股息率 {col} 全为空；DIVIDEND_YIELD 会是整列 NaN",
              file=sys.stderr)
        return {}
    s.index = pd.DatetimeIndex([
        pd.Timestamp(year=int(y) + 1, month=1, day=1) for y in s.index])
    return {f"dividend_yield:{col}": s.astype(float)}


def _load_series(asset: str, state=None):
    """命令行入口负责装配数据：causual 库代码不碰数据层，入口文件可以。

    传了 `state` 端口就把状态层要的表一起读了（两边共用一份快照）。
    派生量走 `derived_series()`——**service 侧走同一个函数**
    （`CausalPort.derived_series`），不要再各拼一份。
    """
    from datahub.port import get_port

    pkg = load_asset(asset)
    port = get_port()
    tables = list(pkg.REQUIRED_TABLES)
    if state is not None:
        tables += [t for t in state.required_tables(asset) if t not in tables]
    series = port.series_map(tables)
    series.update(derived_series(asset, port))
    return series, port.metadata()


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
