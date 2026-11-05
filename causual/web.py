"""因果模块：卡片浏览。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from causual import causual_cards as cards
from service.registry import Module, register

router = APIRouter()
TEMPLATES = Path(__file__).parent / "templates"


def _tpl(request: Request):
    return request.app.state.templates


@router.get("/cards", response_class=HTMLResponse)
def index(request: Request, asset: str = "ashare", limit: int = 40,
          type: str = "daily"):
    kinds = ("macro",) if type == "macro" else ("card",)
    items = [c for c in cards.list_cards(asset) if c.kind in kinds][:limit]
    return _tpl(request).TemplateResponse(
        request, "causual_index.html",
        {"asset": asset, "assets": cards.ASSET_NAME, "items": items,
         "type": type})


@router.get("/card/{asset}/{date}", response_class=HTMLResponse)
def view(request: Request, asset: str, date: str, kind: str = "card"):
    if asset not in cards.ASSET_NAME:
        raise HTTPException(404, f"未知资产：{asset}")
    try:
        text = cards.get_card(asset, date, kind)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    body = cards.to_html(text)
    return _tpl(request).TemplateResponse(
        request, "causual_card.html",
        {"asset": asset, "date": date, "kind": kind,
         "body": body, "asset_name": cards.ASSET_NAME[asset]})


@router.post("/card/{asset}/{date}/regenerate")
def regenerate(asset: str, date: str):
    """重新生成某天的卡片（数据补上了、或改了先验之后用）。"""
    from fastapi.responses import RedirectResponse

    from service.card_service import CardService

    if asset not in cards.ASSET_NAME:
        raise HTTPException(404, f"未知资产：{asset}")
    try:
        CardService().regenerate(asset, date)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"重生成失败：{type(e).__name__}: {e}") from e
    return RedirectResponse(f"/card/{asset}/{date}", status_code=303)


def register_module() -> Module:
    return register(Module(
        name="causual", label="卡片", router=router,
        template_dir=TEMPLATES, nav=[("卡片", "/cards")]))
