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
    from state_layer import profile as core_profile

    if not store.has_v2(asset):
        return []
    meta = store.load_meta(asset)
    out = []
    for name in CHART_ORDER.get(asset, ()):
        if name == "grid" or name not in (meta.get("groups") or {}):
            continue
        labels = store.load_group_labels(asset, name)
        out.append({"title": str(meta["groups"][name].get("title") or name),
                    "table": core_profile.profile(asset, core_profile.observations(panel), labels=labels)})
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
    from state_layer.web import combine

    names = {"trend_down": "下跌", "range": "震荡", "trend_up": "上涨",
             "low": "低波", "normal": "中波", "high": "高波"}
    raw = combine.grid(price, vol)          # 形如 "range-low"

    def to_cn(s: str) -> str:
        a, _, b = str(s).partition("-")
        return f"{names.get(a, a)} - {names.get(b, b)}"

    return raw.map(to_cn)


def _window(n: int, lo, hi) -> tuple[int, int]:
    """服务端夹紧窗口：默认最近一年，窗口至少 20 天。参数非法时退回默认。"""
    if n <= 1:
        return 0, max(0, n - 1)
    try:
        lo_i = int(lo) if lo not in (None, "") else None
        hi_i = int(hi) if hi not in (None, "") else None
    except (TypeError, ValueError):
        lo_i = hi_i = None
    lo_i = max(0, min(lo_i, n - 1)) if lo_i is not None else max(0, n - 251)
    hi_i = max(0, min(hi_i, n - 1)) if hi_i is not None else n - 1
    if hi_i - lo_i < 20:
        hi_i = min(n - 1, lo_i + 20)
        lo_i = max(0, hi_i - 20)
    return lo_i, hi_i


def _charts(asset: str, panel, lo=None, hi=None) -> list[dict]:
    """按 `CHART_ORDER` 组装图。

    标签 = **存盘标签 + 一次性 predict 外推到面板最后一天**。
    每组只算一次（整页只跑一次 `estimator.predict`），外推段单独标出。
    """
    if not store.has_v2(asset):
        return []
    df = store.load_groups(asset)
    meta = store.load_meta(asset)
    cfg = state_assets.load(asset).config
    out: list[dict] = []

    # 整页只预测一次（以前每组各调一次 extend → 一次请求 15 次预测，页面几十秒）
    try:
        est = state_assets.load(asset).estimator.load(asset)
        predicted = {n: g.label_series() for n, g in est.predict(panel).items()}
    except Exception as exc:  # noqa: BLE001
        print(f"[state] {asset} 外推预测失败：{type(exc).__name__}: {exc}")
        predicted = {}

    def _extended(name: str):
        stored = store.load_group_labels(asset, name)
        pred = predicted.get(name)
        if pred is None or not len(pred):
            return stored, stored, set()
        add = pred.loc[pred.index > stored.index.max()]
        merged = pd.concat([stored, add]).sort_index() if len(add) else stored
        return stored, merged, {str(d)[:10] for d in add.index}

    for name in CHART_ORDER.get(asset, ()):
        colors = None
        if name == "grid":
            _, p_lab, p_ext = _extended("short_price")
            _, v_lab, v_ext = _extended("short_vol")
            labels = _grid_labels(asset, p_lab, v_lab)
            ext_dates = p_ext | v_ext
            title = "短期九宫格（价格档 × 波动档）"
            note = "格号 = 价格档-波动档（0=下跌/低波，2=上涨/高波）；区间取 short_price ∩ short_vol"
            refit: list[str] = []
            pn = {"下跌": "0", "震荡": "1", "上涨": "2"}
            vn = {"低波": "0", "中波": "1", "高波": "2"}
            colors = {f"{pk} - {vk}": GRID_COLORS[pn[pk] + "-" + vn[vk]]
                      for pk in pn for vk in vn}
        else:
            if name not in (meta.get("groups") or {}):
                continue
            stored, labels, ext_dates = _extended(name)
            g = meta["groups"][name]
            title = str(g.get("title") or name)
            note = f"{name} · K={g.get('k')} · 该组自己的区间"
            refit = list(g.get("refit_dates") or [])

        n = len(labels)
        if not n:
            continue
        w_lo, w_hi = _window(n, lo, hi)
        rows = _rows(panel, labels)[w_lo:w_hi + 1]
        n_ext_in_win = sum(1 for r in rows if r["date"] in ext_dates)
        tail = f"（末 {n_ext_in_win} 天为外推）" if n_ext_in_win else ""
        out.append({
            "title": title,
            "n": n, "lo": w_lo, "hi": w_hi, "max": max(0, n - 1),
            "full": f"{labels.index.min():%Y-%m-%d} ~ {labels.index.max():%Y-%m-%d}{tail}",
            "range": (f"{rows[0]['date']} ~ {rows[-1]['date']}（{len(rows)} 天）{tail}"
                      if rows else "—"),
            "note": note,
            "n_ext": n_ext_in_win,
            "svg": _svg(rows, refit=refit, colors=colors, ext_dates=ext_dates),
        })
    return out


def _svg(rows: list[dict], refit: list[str] | None = None,
         colors: dict | None = None, ext_dates: set[str] | None = None,
         width: int = 900, height: int = 210) -> str:
    """跑段/点对坐标 + 成交额 + 重估虚线。**只画传进来的这些行**（窗口由调用方决定）。"""
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

    runs = []
    a = 0
    for i in range(1, n + 1):
        if i == n or rows[i]["label"] != rows[a]["label"]:
            runs.append((a, i - 1, rows[a]["label"]))
            a = i
    idx_of = {r["date"]: i for i, r in enumerate(rows)}
    refit_idx = sorted(idx_of[d] for d in (refit or []) if d in idx_of)

    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" width="100%" '
             f'height="{height}" role="img">']
    for i, amt in enumerate(amounts):
        hh = (amt / amax) * h2
        parts.append(f'<rect class="amt" x="{x(i) - 0.9:.2f}" '
                     f'y="{pad_t + h1 + gap + h2 - hh:.2f}" width="1.8" '
                     f'height="{hh:.2f}" fill="#2c3540"/>')
    ext = ext_dates or set()
    for i in range(n - 1):
        col = cmap.get(rows[i]["label"], "#6b7280")
        # 外推段（模型没重拟合、当下补出来的）用虚线 + 半透明，绝不与拟合段同形
        is_ext = rows[i]["date"] in ext or rows[i + 1]["date"] in ext
        cls = "seg ext" if is_ext else "seg"
        extra = (' stroke-dasharray="3 2" opacity="0.5"' if is_ext else "")
        parts.append(f'<line class="{cls}" x1="{x(i):.2f}" y1="{y(closes[i]):.2f}" '
                     f'x2="{x(i + 1):.2f}" y2="{y(closes[i + 1]):.2f}" '
                     f'stroke="{col}" stroke-width="1.8" stroke-linecap="butt"{extra}/>')
    for a_, b_, lab in runs:
        txt = lab if lab else "（无状态）"
        parts.append(f'<line class="hit" x1="{x(a_):.2f}" y1="{y(closes[a_]):.2f}" '
                     f'x2="{x(b_):.2f}" y2="{y(closes[b_]):.2f}" '
                     f'stroke="transparent" stroke-width="9">'
                     f'<title>{rows[a_]["date"]} ~ {rows[b_]["date"]} · {txt}</title></line>')
    for i in refit_idx:
        parts.append(f'<line class="rf" x1="{x(i):.2f}" x2="{x(i):.2f}" '
                     f'y1="{pad_t}" y2="{pad_t + h1}" stroke="#e0a23a" '
                     f'stroke-width="1" stroke-dasharray="3 3" opacity="0.55">'
                     f'<title>重估 {rows[i]["date"]}</title></line>')
    parts.append(f'<text x="6" y="{pad_t + 10}" fill="#9aa0a6" font-size="10">{cmax:.0f}</text>')
    parts.append(f'<text x="6" y="{pad_t + h1}" fill="#9aa0a6" font-size="10">{cmin:.0f}</text>')
    parts.append(f'<text x="6" y="{pad_t + h1 + gap + h2}" fill="#9aa0a6" font-size="10">成交额</text>')
    parts.append("</svg>")
    return "".join(parts)


@router.get("/state", response_class=HTMLResponse)
def index(request: Request, asset: str = "ashare", days: int = 250,
          lo: str | None = None, hi: str | None = None):
    port = get_port()
    # 装配点（AGENTS §4'③：web 路由与 cli 等价）：**取一次面板**，往下全传 DataFrame
    panel = get_data(state_assets.panel_of(asset))

    prof = port.profile(asset, panel)
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
    charts = _charts(asset, panel, lo, hi)
    try:
        _meta = store.load_meta(asset)
        n_ext_all = max((c.get("n_ext") or 0) for c in charts) if charts else 0
        fit_info = (f"最近一次拟合：{_meta.get(chr(102) + chr(105) + chr(116) + chr(95) + chr(100) + chr(97) + chr(116) + chr(101))}"
                    f" · 之后 {n_ext_all} 天为外推（重跑 "
                    f"python -m state_layer.cli fit --asset {asset} 可更新）")
    except Exception as e:  # noqa: BLE001
        print(f"[state] 读 spec 失败：{type(e).__name__}: {e}")
        fit_info = ""
    spec = {}
    try:
        spec = store.spec_any(asset)
    except Exception:  # noqa: BLE001
        pass

    return _tpl(request).TemplateResponse(
        request, "diagnose.html",
        {"asset": asset, "profile": prof, "charts": charts, "avail": [],
         "profiles": _profiles(asset, panel),
         "current": cur, "spec": spec, "transition": trans, "assets": ASSETS,
         "groups": {},
         "excluded": [], "days": days, "lo": lo, "hi": hi,
         "fit_info": fit_info})


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
