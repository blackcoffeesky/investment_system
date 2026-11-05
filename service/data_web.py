"""数据层页面：表清单 + 手工数据录入。

原则：**日常维护不该碰命令行**。
- 看：所有表的频率、来源、起止、是否过期
- 改：手工源上传文件，或直接录一个 (日期, 数值)
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from service.registry import Module, register

router = APIRouter()

# 允许上传的人工数据源（与 datahub.fin.intake.registry 对应）
MANUAL_SOURCES = {
    "wgc": "WGC 央行黄金储备（季报，xlsx）",
    "wgc_etf": "WGC 全球黄金 ETF 持仓（月报，xlsx）",
}

MAX_UPLOAD_MB = 20
RAW_DIR = Path(__file__).resolve().parents[1] / "datahub" / "fin" / "intake" / "raw"


def _tpl(request: Request):
    return request.app.state.templates


@router.get("/data", response_class=HTMLResponse)
def index(request: Request, only: str = "manual"):
    from datahub.fin import metadata

    meta = metadata()
    rows = []
    for _, r in meta.iterrows():
        name = str(r["name"])
        rows.append({
            "name": name,
            "freq": str(r.get("freq") or ""),
            "market": str(r.get("market") or ""),
            "kind": str(r.get("kind") or "single"),
            "start": str(r.get("start_date") or ""),
            "end": str(r.get("end_date") or ""),
            "note": str(r.get("note") or ""),
        })
    if only == "manual":
        keep = {"cb_holdings", "gold_etf_holdings", "insider_net",
                "equity_fin_monthly", "dividend_annual"}
        rows = [r for r in rows if r["name"] in keep]

    return _tpl(request).TemplateResponse(
        request, "data_index.html",
        {"rows": rows, "only": only, "sources": MANUAL_SOURCES,
         "max_mb": MAX_UPLOAD_MB})


@router.post("/data/upload")
async def upload(request: Request, source: str = Form(...),
                 file: UploadFile = File(...), load: str = Form("1")):
    if source not in MANUAL_SOURCES:
        raise HTTPException(400, f"未知数据源：{source}")

    raw = (file.filename or "upload.xlsx").replace("\\", "_").replace("/", "_")
    if not raw.lower().endswith((".xlsx", ".xls", ".csv")):
        raise HTTPException(400, "只接受 .xlsx / .xls / .csv")

    blob = await file.read()
    if len(blob) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"文件超过 {MAX_UPLOAD_MB} MB")

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    target = RAW_DIR / raw
    target.write_bytes(blob)

    from datahub.fin.intake.cli import main as intake_main

    argv = [source, str(target)]
    if load == "1":
        argv.append("--load")
    try:
        code = intake_main(argv)
    except SystemExit as e:              # argparse 出错时
        raise HTTPException(400, f"解析失败：{e}") from e
    if code:
        raise HTTPException(400, f"入库失败（退出码 {code}），到服务器日志看原因")

    return RedirectResponse("/data?only=manual", status_code=303)


@router.post("/data/manual")
async def manual(request: Request, table: str = Form(...),
                 date: str = Form(...), value: str = Form(...),
                 confirm: str = Form("")):
    from datahub.fin import write

    if not table or not date:
        raise HTTPException(400, "表和日期都要填")
    try:
        res = write.upsert_single(table, date, value, overwrite=True)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"写入失败：{type(e).__name__}: {e}") from e

    return _tpl(request).TemplateResponse(
        request, "data_index.html",
        {"rows": [], "only": "manual", "sources": MANUAL_SOURCES,
         "max_mb": MAX_UPLOAD_MB, "result": res})


def register_module() -> Module:
    return register(Module(
        name="data", label="数据", router=router,
        template_dir=Path(__file__).resolve().parents[1] / "service" / "templates",
        nav=[("数据", "/data")]))
