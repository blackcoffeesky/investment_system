"""causual 模块的页面：卡片浏览 + 模型拟合。

路由、模板、端口注册都在这一个文件里（模板在 `templates/`）。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from causual.port import ASSET_NAME, get_port
from service.registry import Module, register

router = APIRouter()
TEMPLATES = Path(__file__).parent / "templates"

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "causual" / "data"
SAVED = ROOT / "regime_engine" / "saved_models"
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


def _series_for(request: Request, asset: str) -> tuple[dict, Any]:
    """从 service 给的端口读数据，组装成 series（路由不直接 import 数据层）。"""
    services = request.app.state.services
    data = services["datahub"]
    causal = get_port()
    out: dict = {}
    for t in causal.required_tables(asset):
        s = data.read(t)
        if s is None:
            continue
        if hasattr(s, "columns"):
            for c in s.columns:
                out[f"{t}:{c}"] = s[c]
        else:
            out[t] = s
    return out, data.metadata()


@router.post("/card/{asset}/{date}/regenerate")
def regenerate(request: Request, asset: str, date: str):
    if asset not in ASSET_NAME:
        raise HTTPException(404, f"未知资产：{asset}")
    try:
        series, meta = _series_for(request, asset)
        get_port().regenerate(asset, date, series, meta)
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


def _asset_state(asset: str, meta) -> dict:
    """一个资产的产物状态：状态模型、边快照、数据覆盖。"""
    out: dict = {"asset": asset}

    cur = DATA / "regime" / asset / "current.json"
    if cur.exists():
        j = json.loads(cur.read_text(encoding="utf-8"))
        out["regime_fit"] = j.get("fit_date")
        out["regime_age"] = _age_days(j.get("fit_date"))
        out["k"] = j.get("k")
        out["signature"] = j.get("label_signature")

    lab = DATA / "regime" / asset / "labels.csv"
    if lab.exists():
        lines = lab.read_text(encoding="utf-8").strip().splitlines()
        out["labels_end"] = lines[-1].split(",")[0] if len(lines) > 1 else ""

    snap = DATA / "snapshots" / asset / "latest.json"
    if snap.exists():
        j = json.loads(snap.read_text(encoding="utf-8"))
        out["edges_fit"] = j.get("fit_date")
        out["edges_age"] = _age_days(j.get("fit_date"))
        out["edges_sig"] = j.get("label_signature")

    out["hmm_pkl"] = (SAVED / asset / "hmm_last.pkl").exists()

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
    assets = [_asset_state(a, meta) for a in ("ashare", "gold")]
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
