"""causual 模块的页面：卡片浏览 + 模型拟合。

路由、模板、端口注册都在这一个文件里（模板在 `templates/`）。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
import pandas as pd

from causual.port import ASSET_NAME, get_port
from service.registry import Module, register

router = APIRouter()
TEMPLATES = Path(__file__).parent / "templates"

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "causual" / "data"
FIT_LOG = ROOT / "logs" / "fit.log"

FIT_LOCK = threading.Lock()


def _tpl(request: Request):
    return request.app.state.templates


# ============================================================ 卡片
@router.get("/cards", response_class=HTMLResponse)
def index(request: Request, asset: str = "ashare", limit: int = 40,
          type: str = "daily"):
    port = get_port()
    kinds = ("macro",) if type == "macro" else ("card",)
    items = [c for c in port.list_cards(asset) if c.kind in kinds][:limit]
    return _tpl(request).TemplateResponse(
        request, "causual_index.html",
        {"asset": asset, "assets": ASSET_NAME, "items": items, "type": type})


@router.get("/card/{asset}/{date}", response_class=HTMLResponse)
def view(request: Request, asset: str, date: str, kind: str = "card"):
    if asset not in ASSET_NAME:
        raise HTTPException(404, f"未知资产：{asset}")
    port = get_port()
    try:
        text = port.get_card(asset, date, kind)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    return _tpl(request).TemplateResponse(
        request, "causual_card.html",
        {"asset": asset, "date": date, "kind": kind,
         "body": port.to_html(text), "asset_name": ASSET_NAME[asset]})


def _panel_for(request: Request, asset: str) -> pd.DataFrame:
    """装配点：路由等价于该模块的 CLI，可以在这里取数（`AGENTS.md` §4'③）。

    要哪个面板由资产包声明（`CausalPort.panel_kind`），核心代码只收 DataFrame。
    用的是 `services["datahub"]` 那个同一个单例（`ports()` 里就是 `get_port`）。
    """
    from shared.adapter import get_data

    return get_data(get_port().panel_kind(asset))


@router.post("/card/{asset}/{date}/regenerate")
def regenerate(request: Request, asset: str, date: str):
    """重生成某一天的卡（不改结论，只重跑同一条链路）。

    **必须把 `state` 一起装配**（`state_view(asset, panel)`）：`build_panel` 要
    `state["spec"]`（状态模型参数）和 `state["labels"]`，少了它直接
    `RuntimeError: 缺少状态模型的 spec`——2026-11-18 用户点按钮就是这个 500。

    ⚠️ 用的是**最新**的 `state_view`，不是"回到 date 那一刻"的状态：
    - `asof=date` 已经决定卡片渲染哪一天（`when = asof`），标签本身是全历史序列，
      历史日期能取到当天的标签；
    - 这也正是当初生成那张卡的路径（`daily` 用当时的全量面板 + 同一个 state），
      所以"重生成"= 逐字重跑，不引入新结论；
    - 真要"以 date 为 asof 重算状态"，得 `state_view(asset, panel.loc[:date])` 再外推，
      那是另一种产品语义（会得到不同的卡），不归这个按钮。
    """
    if asset not in ASSET_NAME:
        raise HTTPException(404, f"未知资产：{asset}")
    try:
        from shared.adapter import freshness as panel_freshness

        panel = _panel_for(request, asset)
        rows = panel_freshness(get_port().panel_kind(asset), today=date)
        # 状态视图 + 画像表：和 CLI 同一套装配（`state["profile"]` 是 v2 卡片的画像来源）
        from causual.causual_core.cli import state_view

        view = state_view(asset, panel)
        get_port().regenerate(asset, date, panel, state=view, freshness=rows)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"重生成失败：{type(e).__name__}: {e}") from e
    return RedirectResponse(f"/card/{asset}/{date}", status_code=303)


# ============================================================ 拟合
def _age_days(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(str(iso)[:19])).days
    except Exception:  # noqa: BLE001
        return None


def _asset_state(asset: str, meta, st) -> dict:
    """一个资产的产物状态：状态模型、边快照、数据覆盖。

    **状态模型的信息一律走 `StatePort`**（`spec` / `labels`）。这里以前直接读
    `causual/data/regime/{asset}/current.json` + `labels.csv` —— 状态层把产物搬到
    `state_layer/saved_models/{asset}/spec.json` + `groups.csv` 之后，那两个文件
    永远不存在，于是「状态模型 / K / 签名 / 标签」一栏**永远空白且不报错**（静默失败）。
    2026-11-23 修。

    v2 的 `spec` 里**没有拟合日期**（只有 `fit_id`），所以「状态模型拟合」那行
    v2 下显示 `—`，「模型 ID」另起一行显示 `fit_id`；不拿文件 mtime 冒充拟合时间。
    """
    out: dict = {"asset": asset, "regime_age": None, "edges_age": None}

    try:
        spec = st.spec(asset)
    except Exception:  # noqa: BLE001  （没拟合过就是没产物，不是错误）
        spec = None
    if spec:
        if spec.get("schema") == "estimator-v2":
            prim = spec.get("primary")
            grp = (spec.get("groups") or {}).get(prim, {}) if prim else {}
            out["group"] = prim
            out["k"] = grp.get("k")
            out["signature"] = grp.get("label_signature")
            out["model_id"] = grp.get("fit_id")
        else:                       # legacy 三段式
            data = spec.get("data") or {}
            out["k"] = spec.get("k") or (spec.get("model") or {}).get("k")
            out["signature"] = data.get("label_signature")
            out["model_id"] = data.get("fit_id")
            out["regime_fit"] = data.get("fit_date")
            out["regime_age"] = _age_days(data.get("fit_date"))

    try:
        lab = st.labels(asset)
        if len(lab):
            out["labels_end"] = str(pd.DatetimeIndex(lab.index)[-1].date())
            out["labels_n"] = int(len(lab))
    except Exception:  # noqa: BLE001
        pass

    snap = DATA / "snapshots" / asset / "latest.json"
    if snap.exists():
        j = json.loads(snap.read_text(encoding="utf-8"))
        out["edges_fit"] = j.get("fit_date")
        out["edges_age"] = _age_days(j.get("fit_date"))
        out["edges_sig"] = j.get("label_signature")

    # 不再显示 "HMM 模型文件在不在"：`hmm_last.pkl` 已降级成遗留产物
    # （重拟合后是旧货、也没人再写它），那个指示灯会永远说"在"。
    # 模型是否可用看上面的「状态模型拟合」日期 + 龄期。

    # 数据覆盖：所有表 end_date 的 min / max（service 提供的端口）
    try:
        md = meta.set_index("name")
        ends = [str(v) for v in md["end_date"].dropna()]
        out["data_min_end"] = min(ends) if ends else ""
        out["data_max_end"] = max(ends) if ends else ""
    except Exception:  # noqa: BLE001
        out["data_min_end"] = out["data_max_end"] = ""

    return out


@router.get("/fit", response_class=HTMLResponse)
def fit_index(request: Request):
    services = request.app.state.services
    meta = services["datahub"].metadata()
    assets = [_asset_state(a, meta, services["state"]) for a in ("ashare", "gold")]
    log_tail = ""
    if FIT_LOG.exists():
        log_tail = "\n".join(
            FIT_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-12:])
    return _tpl(request).TemplateResponse(
        request, "fit_index.html",
        {"assets": assets, "running": FIT_LOCK.locked(), "log_tail": log_tail})


def _fit_job(asset: str, step: str) -> None:
    import subprocess
    import sys

    FIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "causual.causual_core.cli", step, "--asset", asset]
    try:
        with FIT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} {step} {asset} ===\n")
            fh.flush()
            subprocess.run(cmd, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT,
                           check=False)
    finally:
        FIT_LOCK.release()


@router.post("/fit/run")
async def run_fit(asset: str = Form(...), step: str = Form("edges")):
    if asset not in ("ashare", "gold"):
        raise HTTPException(400, "未知资产")
    if step not in ("regime", "edges", "audit"):
        raise HTTPException(400, "未知步骤")
    if not FIT_LOCK.acquire(blocking=False):
        raise HTTPException(409, "已有拟合在跑")
    threading.Thread(target=_fit_job, args=(asset, step), daemon=True).start()
    return RedirectResponse("/fit?started=1", status_code=303)


# ============================================================ 注册
def register_module() -> Module:
    return register(Module(
        name="causual", label="卡片", router=router,
        template_dir=TEMPLATES,
        nav=[("卡片", "/cards"), ("拟合", "/fit")],
        service_port_factory=get_port))
