"""生成 `states_viz.ipynb`（plotly 可视化）。

    python -m experiment.hmm_state.build_viz_notebook
    jupyter nbconvert --to notebook --execute --inplace experiment/hmm_state/states_viz.ipynb

四张图：短价格 / 短波动 / 长价格 / 长波动 —— 价格走势按**过滤状态**着色 + 状态画像柱状图。
两张九宫格：短周期（短价格 × 短波动）、中长期（长价格 × 长波动）——
三层联动：价格按格子着色、3×3 占用热力图、按年份的动画气泡。

**全部用 OOS 段的状态**（过滤概率，不用平滑）。
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

# 让 notebook 在 experiment/hmm_state/ 下也能 import 到仓库根
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
from experiment.hmm_state.stage1 import build_groups, fit_group, ORDER_COL, K

pd.set_option("display.width", 160)
px.defaults.template = "plotly_white"

px_panel = _index_panel()
grids = build_groups(px_panel)
fitted = {}
for name, feats in grids.items():
    fitted[name] = fit_group(feats, ORDER_COL[name])
    print(f"{name}: OOS {len(fitted[name]['states'])} 天，最小成对距离 {fitted[name]['sep']:.3f}")
"""

COLORS = r"""
PALETTE = ["#2E86C1", "#27AE60", "#C0392B", "#8E44AD", "#D68910", "#17A589"]
CELL_COLORS = ["#D6EAF8", "#5DADE2", "#1B4F72",   # 低波：浅→深蓝
               "#F9E79F", "#F4D03F", "#B7950B",   # 中波：浅→深黄
               "#FADBD8", "#E74C3C", "#7B241C"]   # 高波：浅→深红
CELL_ORDER = [f"{p}-{v}" for v in range(3) for p in range(3)]
"""

PLOT_ONE = r"""
def plot_states(name: str) -> go.Figure:
    res = fitted[name]
    states = res["states"]
    close = px_panel["close"].reindex(states.index)
    prof = res["profile"]
    state_ids = sorted(states.unique())

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=False, row_heights=[0.68, 0.32],
        vertical_spacing=0.12,
        subplot_titles=(f"{name}：价格走势（按过滤状态着色）", "状态画像（各特征均值）"))

    fig.add_trace(go.Scatter(x=close.index, y=close.values, mode="lines",
                             line=dict(color="#BDC3C7", width=1),
                             name="价格", showlegend=False), row=1, col=1)

    for i, s in enumerate(state_ids):
        mask = states == s
        fig.add_trace(go.Scatter(
            x=close.index[mask], y=close.values[mask], mode="markers",
            marker=dict(size=4, color=PALETTE[i % len(PALETTE)]),
            name=f"状态 {s}（{int(mask.sum())} 天）"), row=1, col=1)

    # 画像：各状态在每个特征上的均值（横轴 = 特征，纵轴 = 值）
    for i, s in enumerate(state_ids):
        fig.add_trace(go.Bar(x=prof.columns, y=prof.loc[s].values,
                             name=f"状态 {s}", marker_color=PALETTE[i % len(PALETTE)],
                             showlegend=False), row=2, col=1)

    fig.update_layout(height=620, margin=dict(l=40, r=20, t=60, b=40),
                      hovermode="x unified",
                      legend=dict(orientation="h", yanchor="bottom", y=1.02))
    fig.update_yaxes(title_text="价格", row=1, col=1)
    fig.update_yaxes(title_text="特征均值", row=2, col=1)
    return fig
"""

PLOT_GRID = r"""
def build_grid(pkey: str, vkey: str) -> pd.DataFrame:
    sp, sv = fitted[pkey]["states"], fitted[vkey]["states"]
    g = pd.DataFrame({"价格": sp, "波动": sv}).dropna()
    g["格"] = g["价格"].astype(str) + "-" + g["波动"].astype(str)
    g["年份"] = g.index.year
    return g


def plot_grid(pkey: str, vkey: str, title: str) -> go.Figure:
    g = build_grid(pkey, vkey)
    close = px_panel["close"].reindex(g.index)
    share = g["格"].value_counts(normalize=True).reindex(CELL_ORDER).fillna(0) * 100

    heat = np.zeros((3, 3))   # 行 = 波动档，列 = 价格档
    for cell, val in share.items():
        p, v = cell.split("-")
        heat[int(v), int(p)] = val

    fig = make_subplots(
        rows=2, cols=2, row_heights=[0.62, 0.38], column_widths=[0.68, 0.32],
        specs=[[{"colspan": 2}, None], [{"type": "scatter"}, {"type": "heatmap"}]],
        subplot_titles=(f"{title}：价格按九宫格着色", "价格档 × 波动档 的时间占比（%）", ""))

    fig.add_trace(go.Scatter(x=close.index, y=close.values, mode="lines",
                             line=dict(color="#D5D8DC", width=1), name="价格",
                             showlegend=False), row=1, col=1)

    for i, cell in enumerate(CELL_ORDER):
        mask = g["格"] == cell
        if not mask.any():
            continue
        fig.add_trace(go.Scatter(
            x=close.index[mask], y=close.values[mask], mode="markers",
            marker=dict(size=5, color=CELL_COLORS[i]),
            name=f"{cell}（{int(mask.sum())} 天，{share[cell]:.1f}%）"), row=1, col=1)

    # 每格累计收益（相对首日）
    for i, cell in enumerate(CELL_ORDER):
        sub = close[g["格"] == cell]
        if len(sub) < 5:
            continue
        fig.add_trace(go.Scatter(x=sub.index, y=sub.values, mode="lines",
                                 name=cell, line=dict(color=CELL_COLORS[i], width=1),
                                 showlegend=False), row=2, col=1)

    fig.add_trace(go.Heatmap(z=heat, x=["价格0", "价格1", "价格2"],
                             y=["波动0", "波动1", "波动2"],
                             text=np.round(heat, 1), texttemplate="%{text}%",
                             colorscale="Blues", showscale=False), row=2, col=2)

    fig.update_layout(height=720, margin=dict(l=40, r=20, t=70, b=40),
                      hovermode="closest",
                      legend=dict(orientation="h", yanchor="bottom", y=1.06))
    fig.update_yaxes(title_text="价格", row=2, col=1)
    return fig


def plot_animation(pkey: str, vkey: str, title: str):
    g = build_grid(pkey, vkey)
    cnt = g.groupby(["年份", "价格", "波动"]).size().reset_index(name="天数")
    fig = px.scatter(
        cnt, x="价格", y="波动", size="天数", color="天数", animation_frame="年份",
        size_max=45, range_x=[-0.5, 2.5], range_y=[-0.5, 2.5],
        color_continuous_scale="Blues", title=f"{title}：3×3 占用随年份变化")
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
        md("""# 市场状态可视化（plotly）

**看什么**

1. 四个基础 HMM 的状态带：价格按**过滤状态**着色，下面配状态画像（各特征均值）。
2. 两套九宫格：价格按格子着色 + 3×3 占用热力图 + 按年份的占用动画。

**读图注意**

- 用的是 **OOS 段（后 30%）** 的过滤状态 `P(S_t|X_1:t)`，不是平滑概率。
- 状态编号按画像排序（价格按累计收益、波动按波动水平），**0 最弱/最低**。
- 九宫格里同一格子的点数少不代表没意义——**灰线是全部价格，彩点才是该状态的日子**。
"""),
        code(SETUP),
        code(COLORS),
        md("""## 工具函数（先跑这一段，后面直接用）

- `plot_states(name)`：价格按状态着色 + 状态画像
- `plot_grid(pkey, vkey, title)`：九宫格时间线 + 3×3 占比热力图 + 每格价格路径
- `plot_animation(pkey, vkey, title)`：3×3 占用随年份的动画
"""),
        code(PLOT_ONE),
        code(PLOT_GRID),
        md("## 一、短价格（5–10 日）"),
        code("plot_states('短价格').show()"),
        md("## 二、短波动（5–10 日）"),
        code("plot_states('短波动').show()"),
        md("## 三、长价格（20–60 日）"),
        code("plot_states('长价格').show()"),
        md("## 四、长波动（20–60 日）"),
        code("plot_states('长波动').show()"),
        md("""## 五、四个模型的状态画像对照

行 = 模型 × 状态，列 = 该模型的原始特征。**不同模型的列不同，不要横向比较绝对值**，
只看同一模型内状态之间的单调性。
"""),
        code("""for name, res in fitted.items():
    print(f"=== {name} ===")
    display(res["profile"])
    print()"""),
        md("## 六、短周期九宫格（短价格 × 短波动）"),
        code("plot_grid('短价格', '短波动', '短周期').show()"),
        code("plot_animation('短价格', '短波动', '短周期').show()"),
        md("## 七、中长期九宫格（长价格 × 长波动）"),
        code("plot_grid('长价格', '长波动', '中长期').show()"),
        code("plot_animation('长价格', '长波动', '中长期').show()"),
        md("## 八、复核：两套九宫格的风险画像"),
        code("""from experiment.hmm_state.stage1 import risk_by

for label, (pk, vk) in {"短周期": ("短价格", "短波动"),
                        "中长期": ("长价格", "长波动")}.items():
    g = build_grid(pk, vk)
    print(f"=== {label} ===")
    display(risk_by(g["格"], px_panel))
    print()"""),
    ]
    nb.metadata["kernelspec"] = {"display_name": "Python 3", "language": "python",
                                 "name": "python3"}
    return nb


def main() -> int:
    path = HERE / "states_viz.ipynb"
    nbf.write(build(), path)
    print(f"→ {path}")
    print("跑起来：jupyter nbconvert --to notebook --execute --inplace "
          f"{path.relative_to(HERE.parents[1])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
