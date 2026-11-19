"""跑一遍：建候选矩阵 → 前视检查 → 冗余诊断 → 落盘报告。

    python -m experiment.state_lab.run              # 全量（含个股截面，约 1 分钟）
    python -m experiment.state_lab.run --no-xs      # 跳个股截面（快）

产物：

    experiment/out/state_lab/matrix.csv        候选指标矩阵
    experiment/out/state_lab/report.md         冗余诊断报告
    experiment/out/state_lab/lookahead.csv     前视检查明细
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

OUT = Path(__file__).resolve().parents[1] / "out" / "state_lab"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-xs", action="store_true", help="跳过个股截面离散度（省 30 秒）")
    a = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)

    from experiment.state_lab import lookahead, matrix, redundancy

    print("① 建候选矩阵…")
    feats = matrix.build(include_xs=not a.no_xs)
    feats.to_csv(OUT / "matrix.csv", encoding="utf-8-sig")
    print(f"   {feats.shape[0]} 行 × {feats.shape[1]} 列 → {OUT / 'matrix.csv'}")

    print("② 前视不变量检查…")
    panel = matrix._index_panel()
    lk = lookahead.check(panel)
    lk.to_csv(OUT / "lookahead.csv", index=False, encoding="utf-8-sig")
    bad = lk[~lk["通过"]]
    print("   " + ("全部通过" if bad.empty else f"❌ 未通过：{list(bad['指标'])}"))

    print("③ 冗余诊断…")
    used = [c for c in feats.columns if feats[c].notna().sum() >= 200]
    rep = redundancy.report(feats[used])
    head = (
        "# 市场状态：候选指标与冗余诊断\n\n"
        f"矩阵：{feats.shape[0]} 行 × {feats.shape[1]} 列；参与诊断 {len(used)} 列"
        f"（非空 ≥ 200 天）\n\n"
        f"前视检查：{len(lk)} 个指标，" + ("全部通过" if bad.empty else f"{len(bad)} 个失败") + "\n\n"
        "> **PCA 在这里只用来找重复，不用来决定保留几个。**\n"
        "> 两个指标高度相关 ≠ 可以互换——经济含义没人接替的指标，统计上再重复也要留。\n\n"
    )
    (OUT / "report.md").write_text(head + rep + "\n", encoding="utf-8")
    print(rep)
    print(f"\n→ {OUT / 'report.md'}")
    return 0 if bad.empty else 1


if __name__ == "__main__":
    raise SystemExit(main())
