"""生成 `final_states_viz.ipynb`：混合模型（价格=Student-t，波动=Gaussian）+ 资金面板。

    python -m experiment.hmm_state.build_final_notebook
    python -m nbconvert --to notebook --execute --inplace experiment/hmm_state/final_states_viz.ipynb

格数自适应：短期 3×3、长期 2×2（长期 K=2）。
"""

from __future__ import annotations

from pathlib import Path

import nbformat as nbf

HERE = Path(__file__).resolve().parent


def code(src: str):
    return nbf.v4.new_code_cell(src.strip("\n"))


def md(src: str):
    return nbf.v4.new_markdown_cell(src.strip("\n"))


SETUP = r'''
import sys
from pathlib import Path

ROOT = Path.cwd().resolve()
while ROOT.name and not (ROOT / "state_layer").exists():
    ROOT = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots

from experiment.state_lab.matrix import _index_panel
from experiment.hmm_state.features import LONG_DEFAULT, build_groups, order_col_for, agreement
from experiment.hmm_state.final import K_OF, fit_dim, grid
from experiment.hmm_state.money import load_signals, buckets, coverage, SIGNALS

pd.set_option("display.width", 160)
px.defaults.template = "plotly_white"

px_panel = _index_panel()
print("长期窗口:", LONG_DEFAULT, "| 状态数:", K_OF)
groups = build_groups(px_panel, LONG_DEFAULT)

fitted = {}
for name, feats in groups.items():
    fitted[name] = fit_dim(name, feats, order_col_for(name, LONG_DEFAULT), k=K_OF[name])
    print(f"{name:6s} K={K_OF[name]} {fitted[name]['model']:14s} NLL={fitted[name]['nll']:.4f} "
          f"占比={fitted[name]['share']}")
'''

STYLE = r'''
PALETTE = ["#2E86C1", "#27AE60", "#C0392B", "#8E44AD", "#D68910", "#17A589"]
BASE_COLORS = ["#5499C7", "#E67E22", "#CB4335", "#7D3C98"]


def cell_colors(na, nb):
    colors, order = [], []
    for v in range(nb):
        base = BASE_COLORS[v % len(BASE_COLORS)]
        for p in range(na):
            k = 0.45 + 0.55 * (p / max(na - 1, 1))
            rgb = [int(int(base[i:i + 2], 16) * k) for i in (1, 3, 5)]
            colors.append("rgb(%d,%d,%d)" % tuple(rgb))
            order.append(f"{p}-{v}")
    return colors, order


def sizes(pk, vk):
    na = int(fitted[pk]["states"].max()) + 1
    nb = int(fitted[vk]["states"].max()) + 1
    return na, nb


def cells(pk, vk):
    return grid(fitted[pk]["states"], fitted[vk]["states"])
'''

PLOTS = r'''
def plot_states(name):
    r = fitted[name]
    states = r["states"]
    close = px_panel["close"].reindex(states.index)
    prof = r["profile"]

    fig = make_subplots(rows=2, cols=1, shared_xaxes=False, row_heights=[0.68, 0.32],
                        vertical_spacing=0.12,
                        subplot_titles=(f"{name}（{r['model']}, K={len(prof)}）：价格按过滤状态着色",
                                        "状态画像（各特征均值）"))
    fig.add_trace(go.Scatter(x=close.index, y=close.values, mode="lines",
                             line=dict(color="#BDC3C7", width=1), name="价格",
                             showlegend=False), row=1, col=1)
    for i, s in enumerate(sorted(states.unique())):
        m = states == s
        seg = close.where(m)
        c = PALETTE[i % len(PALETTE)]
        fig.add_trace(go.Scatter(x=seg.index, y=seg.values, mode="lines+markers",
                                 line=dict(color=c, width=2.2),
                                 marker=dict(size=4, color=c),
                                 name=f"状态 {s}（{int(m.sum())} 天）"), row=1, col=1)
    for i, s in enumerate(sorted(prof.index)):
        fig.add_trace(go.Bar(x=prof.columns, y=prof.loc[s].values, name=f"状态 {s}",
                             marker_color=PALETTE[i % len(PALETTE)],
                             showlegend=False), row=2, col=1)
    fig.update_layout(height=620, margin=dict(l=40, r=20, t=60, b=40),
                      hovermode="x unified",
                      legend=dict(orientation="h", yanchor="bottom", y=1.02))
    fig.update_yaxes(title_text="价格", row=1, col=1)
    fig.update_yaxes(title_text="特征均值", row=2, col=1)
    return fig


def plot_grid(pk, vk, title):
    na, nb = sizes(pk, vk)
    colors, order = cell_colors(na, nb)
    cell = cells(pk, vk)
    close = px_panel["close"].reindex(cell.index)
    share = cell.value_counts(normalize=True).reindex(order).fillna(0) * 100

    heat = np.zeros((nb, na))
    for c, v in share.items():
        p, vv = c.split("-")
        heat[int(vv), int(p)] = v

    fig = make_subplots(rows=2, cols=2, row_heights=[0.62, 0.38],
                        column_widths=[0.66, 0.34],
                        specs=[[{"colspan": 2}, None], [{}, {"type": "heatmap"}]],
                        subplot_titles=(f"{title}：{na}×{nb} 格着色",
                                        "各格价格路径", f"{na}×{nb} 时间占比（%）"))
    fig.add_trace(go.Scatter(x=close.index, y=close.values, mode="lines",
                             line=dict(color="#D5D8DC", width=1), name="价格",
                             showlegend=False), row=1, col=1)
    for i, c in enumerate(order):
        m = cell == c
        if not m.any():
            continue
        seg = close.where(m)
        fig.add_trace(go.Scatter(x=seg.index, y=seg.values, mode="lines+markers",
                                 line=dict(color=colors[i], width=2.5),
                                 marker=dict(size=4, color=colors[i]),
                                 name=f"{c}（{int(m.sum())} 天，{share[c]:.1f}%）"),
                      row=1, col=1)
    for i, c in enumerate(order):
        sub = close[cell == c]
        if len(sub) < 5:
            continue
        fig.add_trace(go.Scatter(x=sub.index, y=sub.values, mode="lines",
                                 line=dict(color=colors[i], width=1), name=c,
                                 showlegend=False), row=2, col=1)
    fig.add_trace(go.Heatmap(z=heat, x=[f"价格{i}" for i in range(na)],
                             y=[f"波动{j}" for j in range(nb)],
                             text=np.round(heat, 1), texttemplate="%{text}%",
                             colorscale="Blues", showscale=False), row=2, col=2)
    fig.update_layout(height=720, margin=dict(l=40, r=20, t=70, b=40),
                      hovermode="closest",
                      legend=dict(orientation="h", yanchor="bottom", y=1.06))
    return fig


def plot_animation(pk, vk, title):
    na, nb = sizes(pk, vk)
    cell = cells(pk, vk)
    d = pd.DataFrame({"格": cell})
    d["价格"] = d["格"].str.split("-").str[0].astype(int)
    d["波动"] = d["格"].str.split("-").str[1].astype(int)
    d["年份"] = d.index.year
    cnt = d.groupby(["年份", "价格", "波动"]).size().reset_index(name="天数")
    fig = px.scatter(cnt, x="价格", y="波动", size="天数", color="天数",
                     animation_frame="年份", size_max=45,
                     range_x=[-0.5, na - 0.5], range_y=[-0.5, nb - 0.5],
                     color_continuous_scale="Blues",
                     title=f"{title}：{na}×{nb} 占用随年份")
    fig.update_layout(height=460, margin=dict(l=40, r=20, t=60, b=40))
    fig.update_xaxes(tickmode="array", tickvals=list(range(na)),
                     ticktext=[f"价格{i}" for i in range(na)])
    fig.update_yaxes(tickmode="array", tickvals=list(range(nb)),
                     ticktext=[f"波动{j}" for j in range(nb)])
    return fig
'''

MONEY = r'''
z = load_signals(px_panel)
b = buckets(z)
display(coverage(z))


def plot_money(name):
    s = z[name].dropna()
    close = px_panel["close"].reindex(s.index)
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.55, 0.45],
                        vertical_spacing=0.08,
                        subplot_titles=(f"{name}：{SIGNALS[name]}（滚动 z 分数）", "价格"))
    fig.add_trace(go.Scatter(x=s.index, y=s.values, mode="lines",
                             line=dict(color="#8E44AD", width=1), name=name), row=1, col=1)
    fig.add_hline(y=0, line=dict(color="#BDC3C7", width=1, dash="dot"), row=1, col=1)
    fig.add_trace(go.Scatter(x=close.index, y=close.values, mode="lines",
                             line=dict(color="#34495E", width=1), name="价格"), row=2, col=1)
    fig.update_layout(height=480, margin=dict(l=40, r=20, t=60, b=40),
                      hovermode="x unified", showlegend=False)
    return fig
'''


def build() -> nbf.NotebookNode:
    nb = nbf.v4.new_notebook()
    nb.cells = [
        md("""# 市场状态系统雏形：状态 + 资金

**发射分布按维度选**：价格维度 Student-t，波动维度 Gaussian。
**状态数**：短期 3×3（九宫格），长期 2×2（四宫格）——60–120 窗口下长期第三态几乎不存在。

后面几节是资金变量（期货基差/持仓、ETF 份额、两融、成交额），全部先做滚动 z 归一化。
"""),
        code(SETUP),
        code(STYLE),
        md("## 工具函数（先跑）"),
        code(PLOTS),
        md("## 一、短价格（Student-t, K=3）"),
        code("plot_states('短价格').show()"),
        md("## 二、短波动（Gaussian, K=3）"),
        code("plot_states('短波动').show()"),
        md("## 三、长价格（Student-t, K=2）"),
        code("plot_states('长价格').show()"),
        md("## 四、长波动（Gaussian, K=2）"),
        code("plot_states('长波动').show()"),
        md("## 五、短/长维度的区分度"),
        code('print(f"短价格 vs 长价格 一致率 = '
             '{agreement(fitted[\'短价格\'][\'states\'], fitted[\'长价格\'][\'states\']):.1%}")\n'
             'print(f"短波动 vs 长波动 一致率 = '
             '{agreement(fitted[\'短波动\'][\'states\'], fitted[\'长波动\'][\'states\']):.1%}")'),
        md("## 六、四个维度的画像与占比"),
        code("""for name, r in fitted.items():
    print(f"=== {name}（{r['model']}, K={K_OF[name]}）NLL={r['nll']:.4f} 熵={r['entropy']:.3f} ===")
    display(r["profile"])
    print("占比:", r["share"])
    print()"""),
        md("## 七、短周期九宫格（3×3）"),
        code("plot_grid('短价格', '短波动', '短周期').show()"),
        code("plot_animation('短价格', '短波动', '短周期').show()"),
        md("## 八、中长期四宫格（2×2）"),
        code("plot_grid('长价格', '长波动', '中长期').show()"),
        code("plot_animation('长价格', '长波动', '中长期').show()"),
        md("## 九、两套格子的风险画像"),
        code("""from experiment.hmm_state.stage1 import risk_by, nonoverlap_check

for label, (pk, vk) in {"短周期": ("短价格", "短波动"), "中长期": ("长价格", "长波动")}.items():
    cell = cells(pk, vk)
    print(f"=== {label} ===")
    display(risk_by(cell, px_panel))
    print("非重叠复核:", nonoverlap_check(cell, px_panel))
    print()"""),
        md("""## 十、资金变量

四类信号：期货基差（态度）、期货持仓（分歧度）、ETF 份额（中长期资金）、
两融（杠杆）、成交额（活跃度）。全部滚动 z 归一化，避免高波动期天然数值偏大。
"""),
        code(MONEY),
        md("### 各信号走势"),
        code("""for name in z.columns:
    plot_money(name).show()"""),
        md("""### 各格子的资金 z 均值

先把 9 格 / 4 格的编号和各资金 z 并起来，看哪几个信号值得进条件转移分析。
"""),
        code("""for label, (pk, vk) in {"短周期": ("短价格", "短波动"), "中长期": ("长价格", "长波动")}.items():
    cell = cells(pk, vk)
    d = pd.DataFrame({"格": cell}).join(z, how="inner")
    print(f"=== {label}：各格子的资金 z 均值 ===")
    display(d.groupby("格").mean().round(2))
    print()"""),
    ]
    nb.metadata["kernelspec"] = {"display_name": "Python 3", "language": "python",
                                 "name": "python3"}
    return nb


def main() -> int:
    path = HERE / "final_states_viz.ipynb"
    nbf.write(build(), path)
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
