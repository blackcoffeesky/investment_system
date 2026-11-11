"""诊断层页面：状态画像 + 时间轴叠图。

图表用内联 SVG 生成——服务器上没有 matplotlib，装了还要配中文字体，
而这张图本来就要嵌在网页里。SVG 无依赖、手机可看、hover 用 <title>。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from state_layer import observations, store
from state_layer.port import get_port
from service.registry import Module, register

router = APIRouter()
TEMPLATES = Path(__file__).parent / "templates"

ASSETS = ("ashare",)

# 状态配色（与卡片语义一致：上行暖色、下行冷色、极端更深）
COLORS = {
    "trend_up": "#c9853a",
    "range": "#5a6472",
    "trend_down": "#3d6b9e",
    "extreme_up": "#b4472f",
    "extreme_down": "#2f4b7c",
    "extreme_vol": "#7a3fa0",
}


def _tpl(request: Request):
    return request.app.state.templates


def _series_for(request: Request) -> dict:
    data = request.app.state.services["datahub"]
    port = get_port()
    out: dict = {}
    for t in port.required_tables():
        s = data.read(t)
        if s is None:
            continue
        if hasattr(s, "columns"):
            for c in s.columns:
                out[f"{t}:{c}"] = s[c]
        else:
            out[t] = s
    return out


def _svg(rows: list[dict], width: int = 900, height: int = 260) -> str:
    """三段式时间轴：上=收盘+状态色带，中=成交额柱，下=涨跌家数比。"""
    if not rows:
        return "<p class='muted'>没有可画的数据</p>"

    xs = list(range(len(rows)))
    closes = [r["close"] for r in rows]
    closes_ok = [c for c in closes if c is not None]
    if not closes_ok:
        return "<p class='muted'>缺少收盘价</p>"

    pad_l, pad_r, pad_t = 44, 8, 10
    h1, h2, h3 = 130, 50, 46
    gap = 14
    plot_w = width - pad_l - pad_r
    x_at = lambda i: pad_l + (plot_w * i / max(1, len(xs) - 1))  # noqa: E731

    cmin, cmax = min(closes_ok), max(closes_ok)
    span = (cmax - cmin) or 1.0
    y1 = lambda v: pad_t + h1 - (v - cmin) / span * h1  # noqa: E731

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" '
             f'style="background:#0f1115;border-radius:8px">']

    # ① 状态色带（背景分段）
    seg_start = 0
    for i in range(1, len(rows) + 1):
        if i == len(rows) or rows[i]["label"] != rows[seg_start]["label"]:
            lab = rows[seg_start]["label"]
            if lab:
                x0, x1 = x_at(seg_start), x_at(i - 1)
                parts.append(
                    f'<rect x="{x0:.1f}" y="{pad_t}" width="{max(1, x1 - x0):.1f}" '
                    f'height="{h1}" fill="{COLORS.get(lab, "#333")}" opacity="0.22">'
                    f'<title>{lab}</title></rect>')
            seg_start = i

    # ② 收盘价
    pts = " ".join(f"{x_at(i):.1f},{y1(c):.1f}"
                   for i, c in enumerate(closes) if c is not None)
    parts.append(f'<polyline points="{pts}" fill="none" stroke="#e6e6e6" '
                 f'stroke-width="1.4"/>')

    # ③ 成交额柱
    amts = [r["amount"] or 0 for r in rows]
    amax = max(amts) or 1.0
    base2 = pad_t + h1 + gap + h2
    bw = max(1.0, plot_w / max(1, len(rows)) * 0.7)
    for i, a in enumerate(amts):
        h = (a / amax) * h2 if a else 0
        parts.append(
            f'<rect x="{x_at(i) - bw / 2:.1f}" y="{base2 - h:.1f}" width="{bw:.1f}" '
            f'height="{h:.1f}" fill="#3d6b9e" opacity="0.7">'
            f'<title>{rows[i]["date"]} 成交额 {a:.0f} 亿</title></rect>')

    # ④ 涨跌家数比
    ratios = [r["advance_ratio"] for r in rows]
    base3 = height - 6
    parts.append(f'<line x1="{pad_l}" y1="{base3 - h3 / 2:.1f}" '
                 f'x2="{width - pad_r}" y2="{base3 - h3 / 2:.1f}" '
                 f'stroke="#2a2e37" stroke-dasharray="3 3"/>')
    rpts = " ".join(
        f"{x_at(i):.1f},{base3 - (r or 0) * h3:.1f}"
        for i, r in enumerate(ratios) if r is not None)
    if rpts:
        parts.append(f'<polyline points="{rpts}" fill="none" stroke="#3fb950" '
                     f'stroke-width="1.2"/>')

    # ⑤ 轴的文字标注
    parts.append(f'<text x="6" y="{pad_t + 10}" fill="#9aa0a6" font-size="10">'
                 f'{cmax:.0f}</text>')
    parts.append(f'<text x="6" y="{pad_t + h1}" fill="#9aa0a6" font-size="10">'
                 f'{cmin:.0f}</text>')
    parts.append(f'<text x="6" y="{pad_t + h1 + gap + 10}" fill="#9aa0a6" '
                 f'font-size="10">成交额</text>')
    parts.append(f'<text x="6" y="{base3 - h3 / 2 - 4}" fill="#9aa0a6" '
                 f'font-size="10">宽度</text>')
    parts.append(f'<text x="{pad_l}" y="{height - 4}" fill="#9aa0a6" font-size="10">'
                 f'{rows[0]["date"]}</text>')
    parts.append(f'<text x="{width - pad_r - 60}" y="{height - 4}" fill="#9aa0a6" '
                 f'font-size="10">{rows[-1]["date"]}</text>')
    parts.append("</svg>")
    return "".join(parts)


@router.get("/state", response_class=HTMLResponse)
def index(request: Request, asset: str = "ashare", days: int = 250):
    port = get_port()
    series = _series_for(request)
    prof = port.profile(asset, series)
    tl = port.timeline(asset, series, days)
    avail = port.availability(series)
    try:
        cur = port.current_state(asset)
    except FileNotFoundError:
        cur = {}          # 还没拟合过模型：页面照常打开，只是没有状态信息

    # 市场状态区块：特征由状态层自己的资产包算，不经过因果层
    feats = None
    try:
        from state_layer import state_assets
        from state_layer.cli import load_series

        pkg = state_assets.load(asset)
        series_raw, meta = load_series(asset)
        feats = pkg.features.build(pkg.panel(series_raw, meta))
        cur = port.current_state(asset, feats)
    except Exception as e:  # noqa: BLE001
        print(f"[state] 现算后验失败：{type(e).__name__}: {e}")

    trans = port.transition(asset, feats) if feats is not None else None
    spec = {}
    try:
        spec = store.load_spec(asset)
    except Exception:  # noqa: BLE001
        pass

    return _tpl(request).TemplateResponse(
        request, "diagnose.html",
        {"asset": asset, "profile": prof, "svg": _svg(tl), "avail": avail,
         "current": cur, "spec": spec, "transition": trans,
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
