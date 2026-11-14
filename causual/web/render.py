"""卡片渲染。与资产无关，标签的中文名与状态画像由资产插件提供。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from causual.causual_core.edge import Edge
from causual.causual_core.estimators import EdgeModel
from causual.causual_core.signal_layer import (
    PURITY_MIN,
    Contribution,
    SignalBundle,
    contributions_at,
    purity,
)
from causual.causual_core.macro_layer import HORIZONS, KIND_DESC, KIND_NAME


class Section:
    """卡片的一节：key 用来选，title 用来渲染。"""

    __slots__ = ("key", "title", "lines")

    def __init__(self, key: str, title: str):
        self.key = key
        self.title = title
        self.lines: list[str] = []


class _Builder:
    """按 section 收集文本行。header 是第 0 节（无标题）。"""

    def __init__(self) -> None:
        self._cur = Section("header", "")
        self._done: list[Section] = []
        self._no = 0

    def a(self, line: str = "") -> None:
        self._cur.lines.append(line)

    def s(self, key: str, title: str) -> None:
        self._done.append(self._cur)
        self._no += 1
        self._cur = Section(key, title)

    def sections(self) -> list[Section]:
        return self._done + [self._cur]


def render(sections: list[Section], keep: tuple[str, ...] | None = None,
           title: str | None = None, note: str = "",
           header_extra: list[str] | None = None) -> str:
    """把 section 列表拼成 markdown。keep=None 表示全要。"""
    out: list[str] = []
    n = 0
    for sec in sections:
        if sec.key == "header":
            lines = list(sec.lines)
            if title and lines and lines[0].startswith("# "):
                lines[0] = title
            if header_extra:
                lines = lines + header_extra
            if note:
                lines = lines + ["", f"> {note}"]
            out.extend(lines)
            continue
        if keep is not None and sec.key not in keep:
            continue
        n += 1
        out.extend(["", f"## {n}. {sec.title}", ""])
        out.extend(sec.lines)
    text = "\n".join(out)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip() + "\n"


def _bp(x: float) -> str:
    return "—" if not np.isfinite(x) else f"{x * 100:+.0f}bp"


def _n(x: float, nd: int = 2) -> str:
    return "—" if not np.isfinite(x) else f"{x:+.{nd}f}"


def _mark(c: Contribution, lag0: float | None = None, lagged: float | None = None) -> str:
    """一致性标记。DL 边的 α 只取 lag0，所以按 lag0 判定；滞后段单独提示。"""
    if not np.isfinite(c.beta) or c.beta == 0:
        return "—"
    if c.purity < PURITY_MIN:
        return f"⚠️ 机制不显著（纯度 {c.purity:.2f}）"

    theory = c.edge.dir_for(c.label)
    if getattr(c.edge, "direction_unclear", False):
        return "关联·方向不定（不判冲突）"
    if lag0 is not None and np.isfinite(lag0) and lag0 != 0:
        tail = ""
        if lagged is not None and np.isfinite(lagged) and lagged != 0 \
                and np.sign(lagged) != np.sign(lag0):
            tail = "｜滞后段反向（时间结构）"
        if np.sign(lag0) != np.sign(theory):
            return f"⚠ 当期反向（反向因果嫌疑）{tail}"
        return f"✓（纯度 {c.purity:.2f}）{tail}"

    if not c.sign_ok:
        return "⚠ 方向冲突"
    return f"✓（纯度 {c.purity:.2f}）"


_GROUP_NAME = {"bull": "长期上行", "range": "长期震荡", "bear": "长期下行"}


def _group_at(model, df: pd.DataFrame, when) -> str | None:
    """该边当期分组取自哪个长期状态（与 z 的期别一致：取最近一个月末值）。"""
    if not getattr(model, "path_by_group", None):
        return None
    if "long_regime" not in df.columns:
        return None
    freq = model.freq_used or model.edge.resolved_freq()
    # 必须和 dl_aggregate 完全一致：先重采样到月末，再 ffill 回日频，
    # 取 when 当天可见的值。否则月中会拿到"当月"，而后验/β 用的是"上月"。
    s = df["long_regime"].resample(freq).last().reindex(df.index, method="ffill")
    s = s.loc[:when].dropna()
    return str(s.iloc[-1]) if len(s) else None


def _unit(edge: Edge, source_col: dict[str, str] | None = None) -> str:
    """读数单位。列名带 _BP 的按 bp 显示，避免把 bp 写成 %。"""
    col = (source_col or {}).get(edge.source, "")
    if str(col).endswith("_BP"):
        return "bp"
    return {"diff": "%", "return": "%", "level_z": "", "event": "点",
            "net_flow": "亿"}.get(edge.reading_mode, "")


def build_card(
    asset: str,
    title: str,
    df: pd.DataFrame,
    when: pd.Timestamp,
    *,
    label_col: str,
    label_name: dict[str, str],
    label_order: list[str],
    profile: pd.DataFrame,
    edges: list[Edge],
    models: dict[tuple[str, str], EdgeModel],
    bundle: SignalBundle,
    treatment_col: dict[str, str],
    source_col: dict[str, str],
    data_notes: list[str] | None = None,
    state_line: str | None = None,
    target: str,
    target_cols: dict[str, str],
    separation: dict | None = None,
    extra_header: dict[str, str] | None = None,
    transition: dict | None = None,
    structural: pd.DataFrame | None = None,
    supply: list[dict] | None = None,
) -> str:
    contribs = contributions_at(
        df, models, bundle, edges, when, label_col=label_col, source_col=source_col
    )
    # 只有信号层进 α；结构层/供需层是长期背景，不进当日归因
    direct = [c for c in contribs
              if c.edge.target == target and c.edge.tier == "signal"]
    active = [c for c in direct if c.active]
    label = str(df.loc[when, label_col])
    name = label_name.get(label, label)

    predicted = float(bundle.predicted.loc[when])
    alpha = float(bundle.alpha.loc[when])
    alpha_h = float(bundle.alpha_h.loc[when]) if len(getattr(bundle, "alpha_h", [])) else alpha
    predicted_h = (
        float(bundle.predicted_h.loc[when])
        if len(getattr(bundle, "predicted_h", [])) else predicted
    )
    lean = "偏多" if predicted > 0.2 else ("偏空" if predicted < -0.2 else "中性（弱信号）")

    _b = _Builder()
    a = _b.a
    a(f"# {title} · {when.date()}")
    a("")
    if extra_header:
        for k, v in extra_header.items():
            a(f"> {k}：{v}  ")
    a(f"> 激活机制 {len(active)}/{len(direct)} 条"
      f"（其中关联层 {len([c for c in active if c.edge.layer == 'associative'])} 条）")
    a(f"> α(当期归因) = {alpha:+.2f}　｜　当期归因原值 {_bp(predicted)}")
    if state_line:
        # 市场状态归状态层：这里只留一行，细节看 /state
        a(f"> 当前状态：{state_line}")
    a("")
    a("> α 只做**当期归因**（t 日读数 → t 日收益，分布滞后边只取 lag0），"
      "不含任何滞后累加；滞后效应见详情卡，仅作时间结构参考。")
    a("")
    for note in (data_notes or []):
        a(f"> ⚠ **数据滞后**：{note}。卡片里用到它们的部分按旧值计算，"
          "解读时要打折扣。")
        a("")

    # ---- 1. 状态
    _b.s("state", "市场状态")
    a("")
    a("| 项目 | 数值 |")
    a("|---|---|")
    a(f"| 当前状态 | **{name}**（{label}） |")
    if separation:
        a(f"| 状态分离 | 最小 Bhattacharyya = {separation.get('min_dist', float('nan')):.3f}"
          f"（阈值 0.40，{'✓' if separation.get('passed') else '✗'}） |")
    if target_cols.get("close") in df.columns:
        close = float(df.loc[when, target_cols["close"]])
        ret = float(df.loc[when, target_cols["ret"]]) if target_cols.get("ret") in df.columns else float("nan")
        a(f"| 标的 | {close:.2f} ｜ 当日 {_n(ret)}% |")
    a("")

    if isinstance(profile, pd.DataFrame) and len(profile):
        a(f"**全部状态画像**（K={len(profile)}）")
        a("")
        a("| 标签 | 名称 | 占比 | 平均持续(日) | 日均收益 | 日波动 | 当前 |")
        a("|---|---|---|---|---|---|---|")
        for lab, r in profile.iterrows():
            cur = "◀" if lab == label else ""
            a(f"| {lab} | {label_name.get(lab, lab)} | {r.get('share', 0):.0%} | "
              f"{r.get('avg_duration', 0):.0f} | {r.get('mean_ret', float('nan')):+.3f}% | "
              f"{r.get('vol', float('nan')):.3f}% | {cur} |")
        a("")

    if transition:
        stay = transition.get("stay_label", "")
        p_stay = transition.get("stay_prob", float("nan"))
        nxt = [(k, v) for k, v in transition.get("p_next", {}).items() if k != stay]
        dur = transition.get("durations", {}).get(label, float("nan"))
        a(f"**状态黏性**（今日后验 × 转移矩阵：{p_stay:.0%} 概率留在「{label_name.get(stay, stay)}」，"
          f"该状态平均停留 {dur:.1f} 个交易日）")
        a("")
        if transition.get("ambiguous"):
            pl = transition.get("posterior_label", "")
            pp = transition.get("posterior_prob", float("nan"))
            a(f"> ⚠ **状态模糊**：路径判定（Viterbi）是「{label_name.get(label, label)}」，"
              f"但边际后验有 {pp:.0%} 指向「{label_name.get(pl, pl)}」——"
              f"这本身就是**可能正在切换**的信号。")
            a("")
        if nxt:
            a("| 若离开，最可能转向 | 概率 |")
            a("|---|---|")
            for k, v in nxt[:2]:
                a(f"| {label_name.get(k, k)} | {v:.0%} |")
            a("")
        a("> 这是当前状态的自相关结构，用于判断**黏性与切换**，不是对明天的预测。")
        a("")

    # ---- 2. 结构层（定性）
    if structural is not None:
        _b.s("structure", "结构层（制度与政策，仅定性）")
        a("")
        if len(structural) == 0:
            a("> 尚未录入。在 `data/manual/structural_state.csv` 里写判断、证据与证伪条件。")
            a("")
        else:
            a("| 领域 | 判断 | 证据 | 证伪条件 | 更新 |")
            a("|---|---|---|---|---|")
            for _, r in structural.iterrows():
                a(f"| {r.get('area','')} | {r.get('judgement','')} | "
                  f"{r.get('evidence','')} | {r.get('falsifier','')} | "
                  f"{pd.Timestamp(r['date']).date()} |")
            a("")
            a("> 结构层不进入 α，也不做定量估计——一年只有几次变化，回归无法识别。")
            a("")

    # ---- 3. 供需层（位置 × 边际）
    if supply is not None:
        _b.s("supply", "供需层（位置 × 边际）")
        a("")
        a("> **不是因果估计**：只回答两个问题——这个指标在自身历史的什么位置、"
          "它在变好还是变差。两者都只给分位，不给绝对值（量纲不同，没法横向比）。")
        a("")
        groups: dict[str, list[dict]] = {}
        for r in supply:
            groups.setdefault(r.get("group") or "其他", []).append(r)

        for gi, (gname, items) in enumerate(groups.items(), start=1):
            # 整组都没有边际口径（如年度股息率：11 个点算不出可信 yoy）时，
            # 索性不出边际两列——摆一列全是「—」，读者会以为数据缺了。
            any_mom = any(not r.get("mom_off") for r in items)
            a(f"**3.{gi} {gname}**")
            a("")
            if any_mom:
                a("| 指标 | 位置口径 | 位置分位 | 边际口径 | 边际分位 | 组合读法 | 备注 |")
                a("|---|---|---|---|---|---|---|")
            else:
                a("| 指标 | 口径 | 位置分位 | 读法 | 备注 |")
                a("|---|---|---|---|---|")
            for r in items:
                if r.get("missing"):
                    if any_mom:
                        a(f"| {r['label']} | — | — | — | — | 无数据 | — |")
                    else:
                        a(f"| {r['label']} | — | — | 无数据 | — |")
                    continue
                pp = f"{r['pos_pct']:.0f}%" if np.isfinite(r.get("pos_pct", np.nan)) else "—"
                if not any_mom:
                    a(f"| {r['label']} | {r['pos_name']} | {pp} | "
                      f"{r.get('combo', '—')} | {r.get('note', '') or '—'} |")
                    continue
                mp = f"{r['mom_pct']:.0f}%" if np.isfinite(r.get("mom_pct", np.nan)) else "—"
                a(f"| {r['label']} | {r['pos_name']} | {pp} | {r['mom_name']} | {mp} | "
                  f"{r.get('combo', '—')} | {r.get('note', '') or '—'} |")
            a("")

        a("> **读法按方向分化**：同一个「高位 + 改善」，对正向指标（资金、需求）是"
          "「强势且仍在增强（拥挤度上升）」，对负向指标（供给）是「压力持续加大」。"
          "不预设「高就是坏」。")
        a("")
        a("> 位置与边际的分位窗口统一（最多 60 期）：位置看原始值/滚动和，"
          "边际看同比、环比差或同比 MA3 的一阶差分——**口径不同但分位可比**。")
        a("")
        a("> 供需层不进 α。它回答的是「现在的结构对多头有利还是不利」，不是「明天涨跌」。")

    # ---- 4. 净指向（信号层）
    _b.s("signal", "净指向分解（信号层，计入 α）")
    a("")
    a("> 本表是 **t 日读数** 对 **t 日收益** 的当期归因，**只覆盖信号层**。"
      "长期趋势（结构层、供需层）不进 α，见月频卡与 /state 页。"
      "「边全正但市场在跌」是正常现象：只是部分归因。")
    a("")
    a("| 机制 | 关系 | 读数 | z | 敏感度 | 权重 | 贡献 | 一致性 |")
    a("|---|---|---|---|---|---|---|---|")
    for c in sorted(direct, key=lambda x: -abs(x.contrib)):
        star = "" if c.edge.use_in_alpha else " *"
        if c.stale_days > 0:
            reading = "—"
        elif c.edge.reading_mode == "net_flow":
            v = c.reading
            word = "净流入" if v > 0 else ("净流出" if v < 0 else "持平")
            reading = f"{word} {abs(v):.0f} 亿" if v != 0 else "持平"
        elif c.edge.reading_mode in ("level_z", "event"):
            reading = f"{_n(c.reading_z)}σ"
        else:
            reading = f"{_n(c.reading, 3)}{_unit(c.edge, source_col)}"

        m = models.get(c.edge.key)
        lag0 = lagged = None
        if m is not None and m.path:
            lag0 = float(m.path.get(0, float("nan")))
            lagged = float(sum(v for k, v in m.path.items() if k >= 1))
            # 当期系数可能来自"长期状态分组"而不是全样本，必须标出来
            g = _group_at(m, df, when)
            if g and m.path_by_group.get(g, {}).get(0) is not None:
                b_g = float(m.path_by_group[g][0])
                lag0 = b_g
                sens = (f"当期 {b_g:+.1f}%（进 α，{_GROUP_NAME.get(g, g)} n={m.n_by_group.get(g, 0)}）"
                        f"<br>全样本 {m.path.get(0, float('nan')):+.1f}%｜参考 lag1+ {lagged:+.1f}%")
            else:
                sens = (f"当期 {lag0:+.1f}%（进 α，全样本）"
                        f"<br>参考 lag1+ {lagged:+.1f}%")
        elif c.edge.estimator == "event_study" or (m and (m.estimator_used or "").endswith("event_study")):
            lo_w, hi_w = c.edge.event_window
            state = ("已激活" if c.active and c.edge.event_from_flag or
                     (c.reading_z >= c.edge.event_threshold) else "未激活")
            zt = f"{c.reading_z:+.2f}"
            sens = (f"{c.beta * 100:+.0f}bp/事件"
                    f"<br>窗口 {lo_w}~{hi_w} 日｜{state}（z={zt}）")
        elif np.isfinite(c.monthly_beta):
            sens = f"{c.beta * 100:+.0f}bp/日"
        else:
            sens = f"{c.beta * 100:+.0f}bp/σ"

        rel = "关联" if c.edge.layer == "associative" else "因果"
        a(f"| {c.edge.mechanism}{star} | {rel} | "
          f"{reading} | {_n(c.reading_z)} | {sens} | {c.edge.magnitude:.2f} | "
          f"{_bp(c.contrib)} | {_mark(c, lag0, lagged)} |")
    total = float(np.nansum([c.contrib for c in direct if c.edge.use_in_alpha]))
    a(f"| **合计（tanh 后 α）** | | | | | | **{_bp(total)}** → α={alpha:+.2f} | |")
    a("")
    excluded = [c for c in direct if not c.edge.use_in_alpha]
    if excluded:
        a("* 标星的机制不进 α（样本不足或方向不可靠），仅作结构参考。")
        a("")

    return _b.sections()


def build_detail_card(
    asset: str,
    title: str,
    df: pd.DataFrame,
    when: pd.Timestamp,
    *,
    label_col: str,
    label_name: dict[str, str],
    profile: pd.DataFrame,
    spec: dict,
    edges: list[Edge],
    models: dict[tuple[str, str], EdgeModel],
    target: str,
    source_col: dict[str, str],
    treatment_col: dict[str, str],
    bundle: SignalBundle | None = None,
    supply_detail: list[dict] | None = None,
) -> str:
    """模型详情卡：给人检查"这套估计到底靠不靠谱"用，不用于交易。"""
    _b = _Builder()
    a = _b.a
    a(f"# {title} · 模型详情 · {when.date()}")
    a("")

    _b.s("model", "状态模型")
    a("")
    a("| 项目 | 值 |")
    a("|---|---|")
    a(f"| 资产 | {asset} |")
    a(f"| K | {spec.get('k')} |")
    a(f"| 标签签名 | `{spec.get('label_signature')}` |")
    a(f"| 用到标签 | {', '.join(spec.get('used_labels', []))} |")
    a(f"| Bhattacharyya | {float(spec.get('min_bhattacharyya', 0)):.3f}（阈值 0.40） |")
    a(f"| 拟合截止 | {spec.get('fit_date', '')} |")
    a(f"| 数据截止 | {when.date()} |")
    a("")
    # 状态标签会外推到最新一天，但卡片上的数字都来自数据截止日——要说清楚
    if label_col in df.columns:
        lab = df.index[df[label_col].astype(str) != ""]
        if len(lab) and lab[-1] > pd.Timestamp(when):
            a(f"> 标签外推至 {lab[-1].date()}（数据截止 {pd.Timestamp(when).date()}）。"
              "卡片上的读数与归因都基于数据截止日。")
            a("")
    if isinstance(profile, pd.DataFrame) and len(profile):
        a("| 标签 | 名称 | 占比 | 持续(日) | 日均收益 | 日波动 |")
        a("|---|---|---|---|---|---|")
        for lab, r in profile.iterrows():
            a(f"| {lab} | {label_name.get(lab, lab)} | {r.get('share', 0):.0%} | "
              f"{r.get('avg_duration', 0):.0f} | {r.get('mean_ret', float('nan')):+.3f}% | "
              f"{r.get('vol', float('nan')):.3f}% |")
        a("")

    _b.s("edges", "边清单")
    a("")
    a("| 边 | 估计器 | 频率 | 最大滞后 | 层 | 进α | 时滞 | n |")
    a("|---|---|---|---|---|---|---|---|")
    for e in edges:
        m = models.get(e.key)
        freq = e.freq if e.estimator == "distributed_lag" else "—"
        lag = e.max_lag if e.estimator == "distributed_lag" else "—"
        a(f"| {e.source}→{e.target} | {e.estimator} | {freq} | {lag} | "
          f"{e.layer} | {'是' if e.use_in_alpha else '否'} | {e.latency} | "
          f"{m.n_obs if m else '—'} |")
    a("")

    if supply_detail is not None and len(supply_detail):
        _b.s("supply", "供需层明细（位置 × 边际）")
        a("")
        a("> 位置与边际都只给分位；这里的原始值是给人核对用的，不参与判断。")
        a("")
        a("| 指标 | 方向 | 位置口径 | 位置值 | 位置分位 | 边际口径 | 边际值 | 边际分位 | 读法 | 期别 |")
        a("|---|---|---|---|---|---|---|---|---|---|")
        for r in supply_detail:
            if r.get("missing"):
                a(f"| {r['label']} | — | — | — | — | — | — | — | 无数据 | — |")
                continue
            d = r.get("direction", "+")
            dname = {"+": "正", "-": "负", "0": "不定"}.get(d, d)
            pv = (f"{r['pos_value']:.2f}" if np.isfinite(r.get("pos_value", np.nan)) else "—")
            mv = (f"{r['mom_value']:+.2f}" if np.isfinite(r.get("mom_value", np.nan)) else "—")
            pp = (f"{r['pos_pct']:.0f}% <sub>{r.get('pos_window', 0)}期</sub>"
                  if np.isfinite(r.get("pos_pct", np.nan)) else "—")
            mp = (f"{r['mom_pct']:.0f}% <sub>{r.get('mom_window', 0)}期</sub>"
                  if np.isfinite(r.get("mom_pct", np.nan)) else "—")
            a(f"| {r['label']} | {dname} | {r['pos_name']} | {pv} | {pp} | "
              f"{r['mom_name']} | {mv} | {mp} | {r.get('combo', '—')} | {r['asof']} |")
        a("")

    _b.s("betas", "分状态系数（β / 95%CI / 显著性）")
    a("")
    dl_only = []
    for e in edges:
        m = models.get(e.key)
        if not m:
            continue
        if (m.estimator_used or e.resolve_estimator()) == "distributed_lag":
            dl_only.append(e)
            continue
        if not m.beta_lead:
            continue
        a(f"**{e.source}→{e.target}**　{e.mechanism}")
        a("")
        a("| 标签 | β | 95% CI | 显著 | 理论方向 |")
        a("|---|---|---|---|---|")
        for lab, b in m.beta_lead.items():
            lo, hi = m.beta_ci.get(lab, (float("nan"), float("nan")))
            sig = bool(np.isfinite(lo) and np.isfinite(hi) and not (lo <= 0 <= hi))
            a(f"| {lab} | {b:+.4f} | [{lo:+.3f}, {hi:+.3f}] | {'是' if sig else '否'} | "
              f"{e.dir_for(lab):+d} |")
        a("")
        a(f"> {m.notes}　n={m.n_obs}"
          + (f"，拟合截止 {m.fitted_until.date()}" if m.fitted_until is not None else ""))
        a("")
    if dl_only:
        a(f"> 分布滞后边（{len(dl_only)} 条）不做状态交互，β 见第 4 节逐期表。")
        a("")

    dl = [(e, models.get(e.key)) for e in edges
          if models.get(e.key) is not None
          and (models[e.key].estimator_used or e.resolve_estimator()) == "distributed_lag"]
    dl = [(e, m) for e, m in dl if m and m.path]
    if dl:
        _b.s("lags", "分布滞后的逐期系数（lag0 ~ lagN）")
        a("")
        a("> lag0 是**当期关联**（含同期性，不能单独作因果解释），进 α；"
          "lag1+ 在时间上单向（昨天的读数 → 今天的收益），作参考。")
        a("")
        for e, m in dl:
            mfreq = m.freq_used or e.resolved_freq()
            a(f"**{e.source}→{e.target}**（{mfreq}，每 1σ，单位 %）")
            a("")
            a("| 期 | 系数 | 95% CI | 含 0 | 纯度 | 用途 |")
            a("|---|---|---|---|---|---|")
            for k in sorted(m.path):
                lo, hi = m.path_ci.get(k, (float("nan"), float("nan")))
                contains = "是" if (np.isfinite(lo) and lo <= 0 <= hi) else "否"
                p = (float(purity(m.path[k], lo, hi))
                     if np.isfinite(lo) and np.isfinite(hi) else 0.0)
                use = "进 α（当期关联）" if int(k) == 0 else "参考（滞后）"
                a(f"| lag{int(k)} | {m.path[k]:+.3f} | [{lo:+.3f}, {hi:+.3f}] | "
                  f"{contains} | {p:.2f} | {use} |")
            a("")
            a(f"> lag0 的 p = {m.p_value:.3f}；lag1+ 合计 = "
              f"{sum(v for k, v in m.path.items() if int(k) >= 1):+.3f}%（仅参考，无 CI）")
            a("")
            if m.path_by_group:
                a("**按长期状态分组的当期系数**（只估 lag0，不带滞后项与控制变量，"
                  "与上表是**不同模型**）：")
                a("")
                a("| 长期状态 | lag0 | 95% CI | 含 0 | 样本(月) | 当前 |")
                a("|---|---|---|---|---|---|")
                cur = _group_at(m, df, when)
                for g in ("bull", "range", "bear"):
                    if g not in m.path_by_group:
                        a(f"| {_GROUP_NAME[g]} | — | — | — | <{18} | "
                          f"{'◀' if g == cur else ''} |")
                        continue
                    b_g = m.path_by_group[g][0]
                    lo_g, hi_g = m.path_ci_by_group[g][0]
                    c0 = "是" if (np.isfinite(lo_g) and lo_g <= 0 <= hi_g) else "否"
                    a(f"| {_GROUP_NAME[g]} | {b_g:+.3f} | [{lo_g:+.3f}, {hi_g:+.3f}] | "
                      f"{c0} | {m.n_by_group.get(g, 0)} | {'◀' if g == cur else ''} |")
                a("")
                a("> 当前组样本不足时回退全样本。分组只用于判断"
                  "「同一机制在不同长期状态下的强度是否不同」，不改变 lag1+ 的估计。")
                a("")

    _b.s("contrib", "激活路径的贡献计算")
    a("")
    if bundle is None:
        a("（本次未提供信号包，跳过）")
    else:
        contribs = contributions_at(
            df, models, bundle, edges, when, label_col=label_col, source_col=source_col
        )
        direct = [c for c in contribs if c.edge.target == target and c.edge.use_in_alpha]
        a("公式：`贡献 = 权重 × β × z × gate × active × 纯度`")
        a("")
        a("| 机制 | 权重 | β | z | gate | active | 纯度 | 贡献(bp) |")
        a("|---|---|---|---|---|---|---|---|")
        total = 0.0
        for c in sorted(direct, key=lambda x: -abs(x.contrib)):
            g = float(bundle.gate.loc[when, bundle.column[c.edge.key]])
            act = 1.0 if c.active else 0.0
            a(f"| {c.edge.source} | {c.edge.magnitude:.2f} | {c.beta * 100:+.1f}bp | "
              f"{c.reading_z:+.2f} | {g:.0f} | {act:.0f} | {c.purity:.2f} | "
              f"{c.contrib * 100:+.1f} |")
            total += float(c.contrib) if np.isfinite(c.contrib) else 0.0
        a(f"| **合计** | | | | | | | **{total * 100:+.1f}** |")
        a("")
        a(f"> 未加权前的原始预测 {float(bundle.predicted.loc[when]) * 100:+.1f}bp，"
          f"tanh(x/1%) 后 α = {float(bundle.alpha.loc[when]):+.3f}")
    a("")

    _b.s("freshness", "数据新鲜度")
    a("")
    a("| 列 | 最后更新 | 距今(日) |")
    a("|---|---|---|")
    for col in sorted({c for c in source_col.values() if c in df.columns}):
        s = df[col].loc[:when].dropna()
        if len(s):
            last = s.index[-1]
            a(f"| {col} | {last.date()} | {(when - last).days} |")
    a("")
    _b.s("supply_glossary", "供需层口径说明（位置 × 边际）")
    a("")
    a("供需层只回答两件事：**位置**（在自身历史的什么位置）和**边际**（在变好还是变差）。")
    a("两者都只输出分位——不同指标的绝对量纲不同，没法横向比。")
    a("")
    a("| 概念 | 怎么算 | 取值范围 |")
    a("|---|---|---|")
    a("| 位置分位 | 位置口径序列在当前值之前的 N 期中的百分位 | 0–100% |")
    a("| 边际分位 | 边际口径序列在当前值之前的 N 期中的百分位 | 0–100% |")
    a("")
    a("**位置口径**（看「现在多高」）：")
    a("")
    a("| 口径 | 适用 | 说明 |")
    a("|---|---|---|")
    a("| 原始值 | 存量、比率、扩散指数 | 直接用当月值 |")
    a("| 滚动和 | 流量类（融资、增持、购金、ETF 流入） | 3 月滚动和，压住单月为负的噪声 |")
    a("")
    a("**边际口径**（看「在变好还是变差」）：")
    a("")
    a("| 口径 | 适用 | 说明 |")
    a("|---|---|---|")
    a("| 同比 | 存量、比率 | 本期 / 去年同月 − 1 |")
    a("| 原始值 | 流量类 | 流量本身就是变化量，再取同比会变成二阶差分 |")
    a("| 环比差 | 扩散指数（PMI） | 本月 − 上月；PMI 本身就是环比概念 |")
    a("| 同比 MA3 一阶差 | 累计同比/同比类（社零、CPI、PPI、出口） | 先 MA3 平滑，再取一阶差分——看趋势在加速还是减速 |")
    a("")
    a("**读法按方向分化**：")
    a("")
    a("| 位置 | 边际 | 正向指标（+） | 负向指标（−） |")
    a("|---|---|---|---|")
    a("| 高 | 改善 | 强势且仍在增强（拥挤度上升） | 压力持续加大 |")
    a("| 高 | 恶化 | 见顶信号 | 压力见顶（转好） |")
    a("| 低 | 改善 | 左侧机会 | 压力开始累积 |")
    a("| 低 | 恶化 | 右侧回避 | 压力出清中 |")
    a("")
    a("> 方向为「不定」的指标（如 CPI）不做自动判读——它的符号随位置变化"
      "（低通胀偏正、高通胀偏负），硬给一个方向会误导。")
    a("")
    a("> 全部指标的分位窗口统一为 min(60, 可用期数 − 1)，位置与边际**用同一个窗口**，"
      "否则两个分位不可比。")
    a("")

    a("---")
    a("")
    a("> 本卡片只描述模型，不构成交易建议。要看「今天该怎么做」，看同日期的 `card_*.md`。")
    return _b.sections()


def build_console(
    df: pd.DataFrame,
    when: pd.Timestamp,
    models: dict[tuple[str, str], EdgeModel],
    bundle: SignalBundle,
    edges: list[Edge],
    *,
    label_col: str,
    source_col: dict[str, str],
    target: str,
) -> str:
    contribs = contributions_at(
        df, models, bundle, edges, when, label_col=label_col, source_col=source_col
    )
    direct = [c for c in contribs if c.edge.target == target]
    label = str(df.loc[when, label_col])
    lines = [
        f"{when.date()}  {label}  预期次日={_bp(float(bundle.predicted.loc[when]))} "
        f"(α={float(bundle.alpha.loc[when]):+.2f})",
        "-" * 78,
    ]
    for c in sorted(direct, key=lambda x: -abs(x.contrib)):
        flag = "OK" if c.sign_ok else "FLIP"
        lines.append(
            f"{str(c.edge):>28s}  z={c.reading_z:+.2f}  beta={c.beta * 100:+6.1f}bp  "
            f"contrib={c.contrib * 100:+6.1f}bp  {flag}"
        )
    return "\n".join(lines)
