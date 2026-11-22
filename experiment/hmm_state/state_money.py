"""状态 × 资金信号条件转移分析；block-bootstrap，三级证据评级：探索性 / 候选 / 稳健
h=1/5/10/20多预测步长，只针对短周期九宫格
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from experiment.hmm_state.config import HORIZONS, BLOCK_BOOT_BLOCK, BOOT_REPS, MIN_N, MIN_N_SEG, SEED

def _prep(state: pd.Series, signal: pd.Series, h:int):
    df = pd.DataFrame({"s":state, "n":state.shift(-h), "m":signal.reindex(state.index)}).dropna()
    return df.sort_index()


def _bucket_prob(df:pd.DataFrame, s_val, m_val, to_val):
    sub = df[(df["s"]==s_val)&(df["m"]==m_val)]
    if sub.empty:
        return np.nan
    return float((sub["n"] == to_val).mean())


def block_indices(n:int, blk_size:int, rng):
    starts = np.arange(0, n, blk_size)
    pick = rng.integers(0, len(starts), size=len(starts))
    idx = np.concatenate([np.arange(starts[i], min(starts[i]+blk_size, n)) for i in pick])
    return np.sort(idx)


def boot_lift(df:pd.DataFrame, s, m, to, reps=BOOT_REPS, seed=SEED):
    sub_cond = df[(df["s"]==s)&(df["m"]==m)]
    if sub_cond.empty:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    lifts = []
    for _ in range(reps):
        idx = block_indices(len(df), BLOCK_BOOT_BLOCK, rng)
        samp = df.iloc[idx]
        cond_p = _bucket_prob(samp, s, m, to)
        base_mask = samp["s"] == s
        base_p = float((samp[base_mask]["n"] == to).mean()) if base_mask.any() else np.nan
        if np.isfinite(cond_p) and np.isfinite(base_p):
            lifts.append(cond_p-base_p)
    if not lifts:
        return (np.nan, np.nan)
    return float(np.percentile(lifts, 5)), float(np.percentile(lifts,95))


def three_segment_split(ser:pd.Series, k=3):
    idx = ser.dropna().index
    n = len(idx)
    pieces = []
    for i in range(k):
        slc = idx[i*n//k: (i+1)*n//k]
        pieces.append(ser.loc[slc])
    return pieces


def evaluate_one_h(grid_state:pd.Series, sig_bucket:pd.Series, h:int):
    full = _prep(grid_state, sig_bucket, h)
    if len(full) < MIN_N:
        return pd.DataFrame()
    seg_list = three_segment_split(grid_state, k=3)
    seg_dfs = [_prep(s, sig_bucket, h) for s in seg_list]
    early, mid, late = seg_dfs
    rows = []
    for (s, m), group in full.groupby(["s", "m"]):
        if len(group) < MIN_N:
            continue
        for to_state in sorted(full["n"].unique()):
            cond = float((group["n"] == to_state).mean())
            base = float((full[full["s"]==s]["n"] == to_state).mean())
            lift = cond-base
            if abs(lift) < 0.03:
                continue
            lo_all, hi_all = boot_lift(full, s, m, to_state)
            halves = []
            for seg_df in (early, late):
                g2 = seg_df[(seg_df["s"]==s)&(seg_df["m"]==m)]
                b2 = seg_df[seg_df["s"]==s]
                if len(g2) < MIN_N_SEG or len(b2) < MIN_N_SEG:
                    halves.append((np.nan, np.nan, np.nan))
                    continue
                l2 = float((g2["n"] == to_state).mean())-float((b2["n"] == to_state).mean())
                lo2, hi2 = boot_lift(seg_df, s, m, to_state, reps=BOOT_REPS//2)
                halves.append((l2, lo2, hi2))
            lift_early, lo_e, hi_e = halves[0]
            lift_late, lo_l, hi_l = halves[1]
            same_sign = np.isfinite(lift_early) and np.isfinite(lift_late) and np.sign(lift_early)==np.sign(lift_late) and np.sign(lift_early)==np.sign(lift)
            ci_main_ok = np.isfinite(lo_all) and (lo_all>0 or hi_all<0)
            cnt_seg_ci = sum(1 for (lv, llo, lhi) in halves if np.isfinite(llo) and (llo>0 or lhi<0))
            seg2_sig = sum(1 for (lv, llo, lhi) in halves if np.isfinite(llo) and (llo>0 or lhi<0))
            tier = ""
            if same_sign:
                tier = "探索性"
            if same_sign and ci_main_ok and cnt_seg_ci>=1:
                tier = "候选"
            # 晚期复现
            g_late = late[(late["s"]==s)&(late["m"]==m)]
            late_ok = False
            if len(g_late)>=MIN_N_SEG:
                l3 = float((g_late["n"]==to_state).mean())-float((late[late["s"]==s]["n"]==to_state).mean())
                late_ok = np.isfinite(l3) and np.sign(l3) == np.sign(lift)
            if same_sign and ci_main_ok and seg2_sig ==2 and late_ok:
                tier = "稳健"
            rows.append({
                "状态":s, "资金档":m, "去向":to_state, "n":len(group),
                "条件概率":round(cond,3), "基准概率":round(base,3),
                "lift":round(lift,3),
                "CI合并": f"[{lo_all:.2f},{hi_all:.2f}]" if np.isfinite(lo_all) else "-",
                "lift前半": round(lift_early,3) if np.isfinite(lift_early) else np.nan,
                "CI前": f"[{lo_e:.2f},{hi_e:.2f}]" if np.isfinite(lo_e) else "-",
                "lift后半": round(lift_late,3) if np.isfinite(lift_late) else np.nan,
                "CI后": f"[{lo_l:.2f},{hi_l:.2f}]" if np.isfinite(lo_l) else "-",
                "段CI有效数": cnt_seg_ci,
                "等级": tier
            })
    return pd.DataFrame(rows)


def run_all_horizon(grid_cell:pd.Series, bucket_df:pd.DataFrame):
    collect = []
    for sig_name in bucket_df.columns:
        for h in HORIZONS:
            res = evaluate_one_h(grid_cell, bucket_df[sig_name], h)
            if res.empty:
                continue
            res["signal"] = sig_name
            res["horizon"] = h
            collect.append(res)
    out = pd.concat(collect, ignore_index=True)
    tier_sort = {"稳健":0, "候选":1, "探索性":2}
    out["_rank"] = out["等级"].map(tier_sort).fillna(9)
    out = out.sort_values(["_rank", "signal", "horizon"]).drop(columns="_rank")
    return out
