"""诊断层页面：状态画像 + 时间轴叠图。

图表用内联 SVG 生成——服务器上没有 matplotlib，装了还要配中文字体，
而这张图本来就要嵌在网页里。SVG 无依赖、手机可看、hover 用 <title>。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from shared.adapter import get_data

from state_layer import state_assets, store
from state_layer.analysis import observations
from state_layer.port import get_port
from service.registry import Module, register

router = APIRouter()
TEMPLATES = Path(__file__).parent / "templates"

ASSETS = ("ashare", "gold")   # 2026-11-22：黄金页开出来（先验两页都能 200）

# 状态配色（标签语义：上行暖色、下行冷色、波动三档由浅到深）
COLORS = {
    "trend_up": "#c9853a",
    "range": "#5a6472",
    "trend_down": "#3d6b9e",
    "low": "#8aa0b8",
    "normal": "#c9853a",
    "high": "#b4472f",
}

#: 每个资产要画几张图、按什么顺序（2026-11-22 用户指定）
# 九宫格 9 格：按**波动档**分三个色系（蓝/橙/红），价格档给浅/中/深
# 目标：相邻格一眼可分辨（用户 2026-11-23：「没有区分度」）
GRID_COLORS = {
    "0-0": "#a8c4e0", "0-1": "#e8b98a", "0-2": "#e89a9a",
    "1-0": "#5d8ec4", "1-1": "#d98b3f", "1-2": "#cf5b5b",
    "2-0": "#2f5f96", "2-1": "#a85c12", "2-2": "#8f1f1f",
}

CHART_ORDER = {
    "ashare": ("short_price", "short_vol", "grid", "long_price"),
    "gold": ("raw",),
}


def _tpl(request: Request):
    return request.app.state.templates


def _profiles(asset: str, panel) -> list[dict]:
    """每组一张画像表（2026-11-23 用户要求：三组都要有）。"""
    from state_layer.analysis import observations, stats

    if not store.has_v2(asset):
        return []
    obs = observations.panel(panel)
    meta = store.load_meta(asset)
    out = []
    for name in CHART_ORDER.get(asset, ()):
        if name == "grid" or name not in (meta.get("groups") or {}):
            continue
        labels = store.load_group_labels(asset, name)
        out.append({"title": str(meta["groups"][name].get("title") or name),
                    "table": stats.profile(asset, obs, labels=labels)})
    return out


def _rows(panel, labels) -> list[dict]:
    """按**该组自己的日期**取行（各组区间不同，不能拿整段面板铺）。"""
    if panel is None or panel.empty or not len(labels):
        return []
    sub = panel.reindex(labels.index)
    amount = sub["amount"] if "amount" in sub.columns else None
    rows = []
    for i, d in enumerate(labels.index):
        rows.append({
            "date": str(d)[:10],
            "close": float(sub["close"].iloc[i]),
            "amount": float(amount.iloc[i]) if amount is not None else 0.0,
            "label": str(labels.iloc[i]),
        })
    return rows


def _grid_labels(asset: str, price: pd.Series, vol: pd.Series):
    """九宫格：格 → 「下跌 - 低波」中文图注（颜色 key 与 hover 用同一套字符串）。"""
    from state_layer.analysis import combine

    names = {"trend_down": "下跌", "range": "震荡", "trend_up": "上涨",
             "low": "低波", "normal": "中波", "high": "高波"}
    raw = combine.grid(price, vol)          # 形如 "range-low"

    def to_cn(s: str) -> str:
        a, _, b = str(s).partition("-")
        return f"{names.get(a, a)} - {names.get(b, b)}"

    return raw.map(to_cn)


def _charts(asset: str, panel) -> list[dict]:
    """按 `CHART_ORDER` 组装图（**全段数据**，默认区间由前端取最近一年）。"""
    if not store.has_v2(asset):
        return []
    df = store.load_groups(asset)
    meta = store.load_meta(asset)
    out: list[dict] = []

    for name in CHART_ORDER.get(asset, ()):
        colors = None
        if name == "grid":
            labels = _grid_labels(asset,
                                  store.series_of(df, "short_price"),
                                  store.series_of(df, "short_vol"))
            title = "短期九宫格（价格档 × 波动档）"
            note = ("格号 = 价格档-波动档（0=下跌/低波，2=上涨/高波）；"
                    "区间取 short_price ∩ short_vol")
            refit: list[str] = []
            pn = {"下跌": "0", "震荡": "1", "上涨": "2"}
            vn = {"低波": "0", "中波": "1", "高波": "2"}
            colors = {f"{pk} - {vk}": GRID_COLORS[pn[pk] + "-" + vn[vk]]
                      for pk in pn for vk in vn}
        else:
            if name not in (meta.get("groups") or {}):
                continue
            labels = store.series_of(df, name)
            g = meta["groups"][name]
            title = str(g.get("title") or name)
            note = (f"{name} · K={g.get('k')} · 该组自己的区间"
                    f"（各组不同，九宫格取交集）")
            refit = list(g.get("refit_dates") or [])
        cid = f"{asset}-{name}"
        out.append({
            "cid": cid,
            "title": title,
            "range": f"{labels.index.min():%Y-%m-%d} ~ {labels.index.max():%Y-%m-%d}"
                     if len(labels) else "—",
            "note": note,
            "svg": _svg(_rows(panel, labels), refit=refit, cid=cid, colors=colors),
        })
    return out


def _svg(rows: list[dict], refit: list[str] | None = None, cid: str = "c",
         colors: dict | None = None, width: int = 900, height: int = 210) -> str:
    """跑段粗线时间轴：**每段一条带色粗线**（首尾共用切换点 → 天然无缝）。

    为什么不用点/背景色块：点阵在切换处会露出背景（看着像黑缝），背景色块在频繁切换时糊成一片。
    粗线段用 `stroke-width=3` + `round` 端帽，颜色就是状态色，视觉上"折线本身有颜色"。
    """
    import json

    if not rows:
        return "<p class='muted'>没有可画的数据</p>"
    closes = [r["close"] for r in rows]
    if not any(c is not None for c in closes):
        return "<p class='muted'>缺少收盘价</p>"

    cmap = dict(COLORS)
    cmap.update(colors or {})
    pad_l, pad_r, pad_t = 44, 10, 10
    h1, h2, gap = 130, 40, 14
    n = len(rows)
    plot_w = width - pad_l - pad_r
    cmin, cmax = min(closes), max(closes)
    span = (cmax - cmin) or 1.0
    amounts = [r["amount"] for r in rows]
    amax = max(amounts) or 1.0

    def x(i: int) -> float:
        return pad_l + plot_w * i / max(1, n - 1)

    def y(v: float) -> float:
        return pad_t + h1 * (1.0 - (v - cmin) / span)

    # 跑段：[a, b] 闭区间，颜色取该段标签
    runs = []
    a = 0
    for i in range(1, n + 1):
        if i == n or rows[i]["label"] != rows[a]["label"]:
            runs.append((a, i - 1, rows[a]["label"]))
            a = i
    idx_of = {r["date"]: i for i, r in enumerate(rows)}
    refit_idx = sorted(idx_of[d] for d in (refit or []) if d in idx_of)

    parts = [f'<svg class="chart" data-cid="{cid}" viewBox="0 0 {width} {height}" '
             f'width="100%" height="{height}" role="img">']
    for i, amt in enumerate(amounts):
        hh = (amt / amax) * h2
        parts.append(f'<rect class="amt" data-t="{i}" x="{x(i) - 0.9:.2f}" '
                     f'y="{pad_t + h1 + gap + h2 - hh:.2f}" width="1.8" '
                     f'height="{hh:.2f}" fill="#2c3540"/>')
    for a_, b_, lab in runs:
        col = cmap.get(lab, "#6b7280")
        txt = lab if lab else "（无状态）"
        parts.append(f'<line class="seg" data-a="{a_}" data-b="{b_}" '
                     f'x1="{x(a_):.2f}" y1="{y(closes[a_]):.2f}" '
                     f'x2="{x(b_):.2f}" y2="{y(closes[b_]):.2f}" '
                     f'stroke="{col}" stroke-width="3" stroke-linecap="round">'
                     f'<title>{rows[a_]["date"]} ~ {rows[b_]["date"]} · {txt}</title></line>')
    for i in refit_idx:
        parts.append(f'<line class="rf" data-t="{i}" x1="{x(i):.2f}" x2="{x(i):.2f}" '
                     f'y1="{pad_t}" y2="{pad_t + h1}" stroke="#e0a23a" '
                     f'stroke-width="1" stroke-dasharray="3 3" opacity="0.55">'
                     f'<title>重估 {rows[i]["date"]}</title></line>')
    parts.append(f'<text x="6" y="{pad_t + 10}" fill="#9aa0a6" font-size="10">{cmax:.0f}</text>')
    parts.append(f'<text x="6" y="{pad_t + h1}" fill="#9aa0a6" font-size="10">{cmin:.0f}</text>')
    parts.append(f'<text x="6" y="{pad_t + h1 + gap + h2}" fill="#9aa0a6" font-size="10">成交额</text>')
    parts.append("</svg>")

    blob = {"n": n, "amounts": amounts, "refit": refit_idx,
            "dates": [r["date"] for r in rows],
            "geo": {"h1": h1, "h2": h2, "gap": gap, "pad_l": pad_l, "pad_t": pad_t,
                    "plot_w": plot_w, "cmin": cmin, "cmax": cmax, "amax": amax}}
    parts.append(f'<script type="application/json" id="d-{cid}">'
                 f'{json.dumps(blob, ensure_ascii=False)}</script>')
    return "".join(parts)


@router.get("/state/chart.js")
def chart_js():
    """前端布局脚本（与 node 测试共用同一份源码）。"""
    from fastapi.responses import FileResponse

    return FileResponse(TEMPLATES.parent / "static" / "chart.js",
                        media_type="application/javascript")


@router.get("/state", response_class=HTMLResponse)
def index(request: Request, asset: str = "ashare", days: int = 250):
    port = get_port()
    # 装配点（AGENTS §4'③：web 路由与 cli 等价）：**取一次面板**，往下全传 DataFrame
    panel = get_data(state_assets.panel_of(asset))

    prof = port.profile(asset, panel)
    tl = port.timeline(asset, panel, days)
    avail = port.availability(panel)
    try:
        cur = port.current_state(asset)
    except FileNotFoundError:
        cur = {}          # 还没拟合过模型：页面照常打开，只是没有状态信息

    # 市场状态区块：**传面板**（2026-11-22 起特征由 estimator 内部构造；
    # 以前这里传的是 features 矩阵，接口已改）
    try:
        cur = port.current_state(asset, panel)
    except Exception as e:  # noqa: BLE001
        print(f"[state] 现算后验失败：{type(e).__name__}: {e}")

    trans = port.transition(asset, panel)

    # 四张图（黄金一张）。**单组用各组自己的区间；九宫格用交集**。
    charts = _charts(asset, panel)
    spec = {}
    try:
        spec = store.spec_any(asset)
    except Exception:  # noqa: BLE001
        pass

    return _tpl(request).TemplateResponse(
        request, "diagnose.html",
        {"asset": asset, "profile": prof, "charts": charts, "avail": avail,
         "profiles": _profiles(asset, panel),
         "current": cur, "spec": spec, "transition": trans, "assets": ASSETS,
         "groups": observations.GROUPS,
         "excluded": observations.EXCLUDED_TABLES, "days": days})


@router.get("/diagnose")
def legacy_redirect():
    """旧地址重定向（书签救一下）。"""
    from fastapi.responses import RedirectResponse

    return RedirectResponse("/state", status_code=307)


def register_module() -> Module:
    return register(Module(
        name="state", label="状态", router=router,
        template_dir=TEMPLATES,
        nav=[("状态", "/state")],
        service_port_factory=get_port))
