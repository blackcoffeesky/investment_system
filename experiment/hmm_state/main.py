"""完整流水线入口
1.读取指数面板
2.生成特征组（短价格/短波动/长价格）
3.分别拟合HMM：短价格t-HMM K=3，短波动Gaussian K=3，长价格t-HMM K=2
4.构建短周期九宫格（短价格 × 短波动）
5.加载全部资金信号，生成三档分桶
6.运行条件转移+bootstrap三级证据检验
7.输出结果csv
"""
from __future__ import annotations
from pathlib import Path
import pandas as pd
from experiment.hmm_state.matrix import _index_panel
from experiment.hmm_state.features import build_feature_groups, order_column_map
from experiment.hmm_state.hmm_core import fit_single_model, grid_join
from experiment.hmm_state.money import load_signals, buckets, coverage, SIGNALS, KIND
from experiment.hmm_state.state_money import run_all_horizon
from experiment.hmm_state.config import K_SHORT_PRICE, K_SHORT_VOL, K_LONG_PRICE

OUT_FOLDER = Path(__file__).resolve().parents[1] / "out" / "hmm_state"
OUT_FOLDER.mkdir(parents=True, exist_ok=True)


def main():
    print("读取指数面板...")
    px = _index_panel()
    groups = build_feature_groups(px)
    fitted = {}
    # 拟合三个模型
    spec = [
        ("短价格", groups["短价格"], "t", K_SHORT_PRICE),
        ("短波动", groups["短波动"], "g", K_SHORT_VOL),
        ("长价格", groups["长价格"], "t", K_LONG_PRICE),
    ]
    for name, feat_df, dist, k in spec:
        oc = order_column_map(name)
        print(f"拟合 {name} dist={dist}, K={k}")
        res = fit_single_model(feat_df, k, dist, oc)
        fitted[name] = res
        print(f"    NLL={res['nll']:.4f}, entropy={res['entropy']:.3f}, maxp={res['maxp']:.3f}")
        print(f"    状态占比 {res['share']}")

    # 构建短周期九宫格
    grid_series = grid_join(fitted["短价格"]["states"], fitted["短波动"]["states"])
    print(f"短周期九宫格样本量 {len(grid_series)}")

    # 资金信号
    z_sig = load_signals(px)
    bk_df = buckets(z_sig)
    cov_df = coverage(z_sig)
    cov_df.to_csv(OUT_FOLDER / "money_coverage.csv", index=False, encoding="utf-8-sig")
    print("\n资金信号覆盖")
    print(cov_df.to_string(index=False))

    # 条件转移分析
    cond_result = run_all_horizon(grid_series, bk_df)
    cond_result.to_csv(OUT_FOLDER / "state_money_l1.csv", index=False, encoding="utf-8-sig")
    print("\n条件转移结果等级分布")
    print(cond_result.groupby("等级").size().to_string())
    print(f"\n全部输出文件写入 {OUT_FOLDER.resolve()}")
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
