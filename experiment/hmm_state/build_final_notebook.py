"""生成 `final_states_viz.ipynb`：最终雏形（价格=Student-t，波动=Gaussian）的可视化。

    python -m experiment.hmm_state.build_final_notebook
    python -m nbconvert --to notebook --execute --inplace experiment/hmm_state/final_states_viz.ipynb
"""

from __future__ import annotations

from pathlib import Path

import nbformat as nbf

HERE = Path(__file__).resolve().parent


def code(src: str):
    return nbf.v4.new_code_cell(src.strip("\n"))


def md(src: str):
    return nbf.v4.new_markdown_cell(src.strip("\n"))


SETUP = r"""
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
from experiment.hmm_state.features import (LONG_DEFAULT, build_groups,
                                           order_col_for, agreement)
from experiment.hmm_state.final import fit_dim, grid, MODEL

pd.set_option("display.width", 160)
px.defaults.template = "plotly_white"

px_panel = _index_panel()
print("长期窗口:", LONG_DEFAULT)
groups = build_groups(px_panel, LONG_DEFAULT)

fitted = {}
for name, feats in groups.items():
    fitted[name] = fit_dim(name, feats, order_col_for(name, LONG_DEFAULT))
    print(f"{name:6s} {fitted[name]['model']:14s} NLL={fitted[name]['nll']:.4f} "
          f"熵={fitted[name]['entropy']:.3f} 占比={fitted[name]['share']}")
"""

STYLE = r"""
PALETTE = ["#2E86C1", "#27AE60", "#C0392B", "#8E44AD", "#D68910", "#17A589"]
# 波动档分色系（蓝/橙/红），价格档调明暗
CELL_COLORS = ["#AED6F1", "#5499C7", "#1A5276",
               "#F9E79F", "#E67E22", "#A04000",
               "#F5B7B1", "#CB4335", "#641E16"]
CELL_ORDER = [f"{p}-{v}" for v in range(3) for p in range(3)]
"""

PLOTS = r"""
def plot_states(name: str) -> go.Figure:
    r = fitted[name]
    states = r["states"]
    close = px_panel["close"].reindex(states.index)
    prof = r["profile"]

    fig = make_subplots(rows=2, cols=1, shared_xaxes=False, row_heights=[0.68, 0.32],
                        vertical_spacing=0.12,
                        subplot_titles=(f"{name}（{r['model']}）：价格按过滤状态着色",
                                        "状态画像（各特征均值）"))
    fig.add_trace(go.Scatter(x=close.index, y=close.values, mode="lines",
                             line=dict(color="#BDC3C7", width=1), name="价格",
                             showlegend=False), row=1, col=1)
    for i, s in enumerate(sorted(states.unique())):
        m = states == s
        seg = close.where(m)
        fig.add_trace(go.Scatter(x=seg.index, y=seg.values, mode="lines+markers",
                                 line=dict(color=PALETTE[i], width=2.2),
                                 marker=dict(size=4, color=PALETTE[i]),
                                 name=f"状态 {s}（{int(m.sum())} 天）"), row=1, col=1)
    for i, s in enumerate(sorted(prof.index)):
        fig.add_trace(go.Bar(x=prof.columns, y=prof.loc[s].values, name=f"状态 {s}",
                             marker_color=PALETTE[i], showlegend=False), row=2, col=1)
    fig.update_layout(height=620, margin=dict(l=40, r=20, t=60, b=40),
                      hovermode="x unified",
                      legend=dict(orientation="h", yanchor="bottom", y=1.02))
    fig.update_yaxes(title_text="价格", row=1, col=1)
    fig.update_yaxes(title_text="特征均值", row=2, col=1)
    return fig


def _cells(pk, vk) -> pd.Series:
    return grid(fitted[pk]["states"], fitted[vk]["states"])


def plot_grid(pk: str, vk: str, title: str) -> go.Figure:
    cell = _cells(pk, vk)
    close = px_panel["close"].reindex(cell.index)
    share = cell.value_counts(normalize=True).reindex(CELL_ORDER).fillna(0) * 100

    heat = np.zeros((3, 3))
    for c, v in share.items():
        p, vv = c.split("-")
        heat[int(vv), int(p)] = v

    fig = make_subplots(rows=2, cols=2, row_heights=[0.62, 0.38],
                        column_widths=[0.66, 0.34],
                        specs=[[{"colspan": 2}, None], [{}, {"type": "heatmap"}]],
                        subplot_titles=(f"{title}：价格按九宫格着色（"
                                        f"{fitted[pk]['model']} × {fitted[vk]['model']}）",
                                        "各格价格路径", "3×3 时间占比（%）"))
    fig.add_trace(go.Scatter(x=close.index, y=close.values, mode="lines",
                             line=dict(color="#D5D8DC", width=1), name="价格",
                             showlegend=False), row=1, col=1)
    for i, c in enumerate(CELL_ORDER):
        m = cell == c
        if not m.any():
            continue
        seg = close.where(m)
        fig.add_trace(go.Scatter(x=seg.index, y=seg.values, mode="lines+markers",
                                 line=dict(color=CELL_COLORS[i], width=2.5),
                                 marker=dict(size=4, color=CELL_COLORS[i]),
                                 name=f"{c}（{int(m.sum())} 天，{share[c]:.1f}%）"),
                      row=1, col=1)
    for i, c in enumerate(CELL_ORDER):
        sub = close[cell == c]
        if len(sub) < 5:
            continue
        fig.add_trace(go.Scatter(x=sub.index, y=sub.values, mode="lines",
                                 line=dict(color=CELL_COLORS[i], width=1),
                                 name=c, showlegend=False), row=2, col=1)
    fig.add_trace(go.Heatmap(z=heat, x=["价格0", "价格1", "价格2"],
                             y=["波动0", "波动1", "波动2"], text=np.round(heat, 1),
                             texttemplate="%{text}%", colorscale="Blues",
                             showscale=False), row=2, col=2)
    fig.update_layout(height=720, margin=dict(l=40, r=20, t=70, b=40),
                      hovermode="closest",
                      legend=dict(orientation="h", yanchor="bottom", y=1.06))
    return fig


def plot_animation(pk: str, vk: str, title: str):
    cell = _cells(pk, vk)
    d = pd.DataFrame({"格": cell})
    d["价格"] = d["格"].str.split("-").str[0].astype(int)
    d["波动"] = d["格"].str.split("-").str[1].astype(int)
    d["年份"] = d.index.year
    cnt = d.groupby(["年份", "价格", "波动"]).size().reset_index(name="天数")
    fig = px.scatter(cnt, x="价格", y="波动", size="天数", color="天数",
                     animation_frame="年份", size_max=45,
                     range_x=[-0.5, 2.5], range_y=[-0.5, 2.5],
                     color_continuous_scale="Blues",
                     title=f"{title}：3×3 占用随年份")
    fig.update_layout(height=460, margin=dict(l=40, r=20, t=60, b=40))
    fig.update_xaxes(tickmode="array", tickvals=[0, 1, 2],
                     ticktext=["价格0(弱)", "价格1(中)", "价格2(强)"])
    fig.update_yaxes(tickmode="array", tickvals=[0, 1, 2],
                     ticktext=["波动0(低)", "波动1(中)", "波动2(高)"])
    return fig
"""


def build() -> nbf.NotebookNode:
    nb = nbf.v4.new_notebook()
    nb.cells = [
        md("""# 最终状态系统雏形

**发射分布按维度选**：价格维度用 Student-t HMM，波动维度用 Gaussian HMM。

理由不是 NLL 更高，而是**应用指标**：价格维度 t 的风险分层更好（ES5 极差 1.94→2.56、
非重叠复核 3.7→6.3），波动维度 t 的分层塌了（9.83→2.98、6.57→3.27）。

看四张状态图 + 两套九宫格（时间线着色 / 3×3 热力图 / 逐年动画）。
"""),
        code(SETUP),
        code(STYLE),
        md("## 工具函数（先跑）"),
        code(PLOTS),
        md("## 一、短价格（Student-t）"),
        code("plot_states('短价格').show()"),
        md("## 二、短波动（Gaussian）"),
        code("plot_states('短波动').show()"),
        md("## 三、长价格（Student-t）"),
        code("plot_states('长价格').show()"),
        md("## 四、长波动（Gaussian）"),
        code("plot_states('长波动').show()"),
        md("""## 五、短/长维度的区分度

两个尺度是不是真的不同，看同一日期两个状态序列的一致率。
一致率高说明两个窗口切出来的东西差不多（那就没有"多尺度"的意义）。
"""),
        code("""print(f"短价格 vs 长价格 一致率 = {agreement(fitted['短价格']['states'], fitted['长价格']['states']):.1%}")
print(f"短波动 vs 长波动 一致率 = {agreement(fitted['短波动']['states'], fitted['长波动']['states']):.1%}")"""),
        md("## 六、四个维度的画像与占比"),
        code("""for name, r in fitted.items():
    print(f"=== {name}（{r['model']}）NLL={r['nll']:.4f} 熵={r['entropy']:.3f} "
          f"最大后验={r['maxp']:.3f} ===")
    display(r["profile"])
    print("占比:", r["share"])
    print()"""),
        md("## 七、短周期九宫格（短价格 × 短波动）"),
        code("plot_grid('短价格', '短波动', '短周期').show()"),
        code("plot_animation('短价格', '短波动', '短周期').show()"),
        md("## 八、中长期九宫格（长价格 × 长波动）"),
        code("plot_grid('长价格', '长波动', '中长期').show()"),
        code("plot_animation('长价格', '长波动', '中长期').show()"),
        md("## 九、两套九宫格的风险画像"),
        code("""from experiment.hmm_state.stage1 import risk_by, nonoverlap_check

for label, (pk, vk) in {"短周期": ("短价格", "短波动"),
                        "中长期": ("长价格", "长波动")}.items():
    cell = _cells(pk, vk)
    print(f"=== {label} ===")
    display(risk_by(cell, px_panel))
    print("非重叠复核:", nonoverlap_check(cell, px_panel))
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
