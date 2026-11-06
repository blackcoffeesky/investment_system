"""模型拟合页：看上次拟合时间、数据覆盖到哪天，并能触发重拟合。"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from service.registry import Module, register

router = APIRouter()
TEMPLATES = Path(__file__).parent / "templates"

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "causual" / "data"
SAVED = ROOT / "regime_engine" / "saved_models"

FIT_LOCK = threading.Lock()
FIT_LOG = ROOT / "logs" / "fit.log"


def _tpl(request: Request):
    return request.app.state.templates


def _age_days(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(str(iso)[:19])).days
    except Exception:  # noqa: BLE001
        return None


def _asset_state(asset: str) -> dict:
    """一个资产的产物状态：状态模型、边快照、特征覆盖。"""
    out = {"asset": asset}

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

    # 特征覆盖：数据层里最靠后的那个"最后一天"决定能拟合到哪天
    try:
        from datahub.fin import metadata

        meta = metadata().set_index("name")
        ends = {t: str(meta.loc[t, "end_date"]) for t in meta.index}
        out["data_min_end"] = min(ends.values()) if ends else ""
        out["data_max_end"] = max(ends.values()) if ends else ""
    except Exception:  # noqa: BLE001
        out["data_min_end"] = out["data_max_end"] = ""

    return out


@router.get("/fit", response_class=HTMLResponse)
def index(request: Request):
    assets = [_asset_state(a) for a in ("ashare", "gold")]
    running = FIT_LOCK.locked()
    log_tail = ""
    if FIT_LOG.exists():
        log_tail = "\n".join(
            FIT_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-12:])
    return _tpl(request).TemplateResponse(
        request, "fit_index.html",
        {"assets": assets, "running": running, "log_tail": log_tail})


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


def register_module() -> Module:
    return register(Module(
        name="fit", label="拟合", router=router,
        template_dir=TEMPLATES, nav=[("拟合", "/fit")]))
