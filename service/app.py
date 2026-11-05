"""服务层应用：发现 + 挂载 + 统一中间件。

    uvicorn service.app:app --host 127.0.0.1 --port 8000

单进程模型：模块在 import 时注册路由，这里只负责拼装。
鉴权用环境变量 `SERVICE_TOKEN`（不设就不鉴权，仅限本机）。
"""

from __future__ import annotations

import os
import secrets
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from datahub import DB_PATH
from service.registry import Module, all_modules, nav_items, register, template_dirs

SERVICE_DIR = Path(__file__).resolve().parent
PUBLIC_PATHS = {"/health"}

app = FastAPI(title="因果状态卡", docs_url=None, redoc_url=None)

# 一次只允许一个"会改变状态"的操作（防连点、防并发写状态机）
RUN_LOCK = threading.Lock()

# ---------------- 中间件：鉴权 + 日志 + 错误
TOKEN = os.environ.get("SERVICE_TOKEN", "")


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not TOKEN or request.url.path in PUBLIC_PATHS:
            return await call_next(request)
        # 支持 ?token=xxx（手机书签用）与 Authorization 头
        supplied = request.query_params.get("token") or \
            request.headers.get("authorization", "").removeprefix("Bearer ")
        if not secrets.compare_digest(str(supplied), TOKEN):
            raise HTTPException(401, "需要 token：/?token=...")
        return await call_next(request)


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        import logging
        import time

        t0 = time.time()
        resp = await call_next(request)
        logging.getLogger("service.web").info(
            "%s %s → %s (%.0f ms)", request.method, request.url.path,
            resp.status_code, (time.time() - t0) * 1000)
        return resp


app.add_middleware(AuthMiddleware)
app.add_middleware(RequestLogMiddleware)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    tpl = getattr(app.state, "templates", None)
    if tpl is None:
        return HTMLResponse(f"<h1>{exc.status_code}</h1><p>{exc.detail}</p>",
                            status_code=exc.status_code)
    return tpl.TemplateResponse(
        request, "error.html",
        {"code": exc.status_code, "detail": exc.detail},
        status_code=exc.status_code)


# ---------------- 模板：searchpath 含服务层与各模块
templates = Jinja2Templates(directory=[str(SERVICE_DIR / "templates")])
app.state.templates = templates


def _refresh_template_paths() -> None:
    templates.env.loader.searchpath = template_dirs(SERVICE_DIR)


# ---------------- 运维页
def _manager():
    from service.card_service import CardService
    from service.data_service import get_data_service
    from service.ops.manager import Manager
    from service.ops.states import STATES

    mgr = Manager(STATES, get_data_service(), DB_PATH)
    mgr.extra = {"cards": CardService()}
    return mgr


@app.get("/ops", response_class=HTMLResponse)
def ops_page(request: Request):
    mgr = _manager()
    status = mgr.status()
    msgs = mgr.store.messages(unacked_only=True)
    defaults = {s.name: s.default_snooze for s in mgr.states.values()}
    for m in msgs:
        m["default_snooze"] = defaults.get(m.get("state", ""), "once")

    # 展示层按 (state, level) 汇总：一个状态一堆同类告警折叠成一组。
    # 不动 Message 结构——一条消息仍对应一个 (key, period)，那是静音的基础。
    groups: dict[tuple[str, str], list[dict]] = {}
    for m in msgs:
        groups.setdefault((m.get("state", ""), m["level"]), []).append(m)
    msg_groups = [{"state": k[0], "level": k[1], "items": v,
                   "first": v[0], "count": len(v),
                   "keys": [m.get("key") for m in v if m.get("key")]}
                  for k, v in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0]))]
    recent = mgr.store.messages()[-20:][::-1]
    stale = mgr.store.stale_snoozes(90)

    all_settled = mgr.all_settled()
    has_unacked = mgr.has_unacked()

    last = mgr.store.last_run()
    hours = None
    if last and last.get("started_at"):
        delta = datetime.now() - datetime.fromisoformat(last["started_at"])
        hours = round(delta.total_seconds() / 3600, 1)
    return templates.TemplateResponse(
        request, "ops.html",
        {"status": status, "messages": msgs, "msg_groups": msg_groups,
         "recent": recent, "stale": stale,
         "last_run": last, "hours_since": hours,
         "stale_watchdog": bool(hours is not None and hours > 26),
         "all_settled": all_settled, "has_unacked": has_unacked,
         "just_ran": request.query_params.get("ran"),
         "running": bool(last and not last.get("finished_at")),
         "started": request.query_params.get("started")})


@app.post("/task/confirm")
async def confirm(msg_id: str = Form(...), action: str = Form("retry"),
                  remind_at: str = Form("")):
    if not RUN_LOCK.acquire(blocking=False):
        raise HTTPException(409, "状态机正在运行，稍后确认")
    try:
        mgr = _manager()
        res = mgr.ack(msg_id, action=action, remind_at=remind_at or None)
    finally:
        RUN_LOCK.release()
    if not res.get("ok"):
        raise HTTPException(400, res.get("reason", "确认失败"))
    return RedirectResponse("/ops", status_code=303)


def _run_job(mode: str) -> None:
    """后台跑一轮。**不占 HTTP 请求**——fetch 可能跑好几分钟。"""
    import logging

    log = logging.getLogger("service.web")
    try:
        mgr = _manager()
        if mode == "rerun":
            r = mgr.reset_all()
            if not r.get("ok"):
                log.warning("重跑被拒：%s", r.get("reason"))
                return
        mgr.run()
    except Exception:  # noqa: BLE001
        log.exception("后台运行失败")
    finally:
        RUN_LOCK.release()


@app.post("/task/run")
async def task_run(mode: str = Form("run")):
    if not RUN_LOCK.acquire(blocking=False):
        raise HTTPException(409, "已经在运行了，稍等一下")
    threading.Thread(target=_run_job, args=(mode,), daemon=True).start()
    return RedirectResponse("/ops?started=1", status_code=303)


@app.get("/health")
def health():
    """自检：除了模块，还回报数据库的两个关键 PRAGMA。

    journal_mode 不是 wal → WAL 没生效（并发会撞锁）
    busy_timeout 是 5000 → 用的是裸 connect，不是 core.conn.connect
    """
    from datahub.core.conn import connect

    try:
        with connect() as con:
            mode = con.execute("PRAGMA journal_mode").fetchone()[0]
            busy = con.execute("PRAGMA busy_timeout").fetchone()[0]
    except Exception:  # noqa: BLE001
        mode, busy = "unknown", 0
    return {"ok": True, "modules": [m.name for m in all_modules()],
            "journal_mode": mode, "busy_timeout": busy}


@app.get("/", response_class=HTMLResponse)
def home():
    return RedirectResponse("/cards", status_code=303)


# ---------------- 挂载模块
def _load_modules():
    from causual import web as causual_web
    from service import data_web

    causual_web.register_module()      # 它内部已经调用 register()
    data_web.register_module()


_load_modules()
_refresh_template_paths()


def _startup_recover() -> None:
    """进程启动时把上次崩在 running 的状态折算成 blocked（只做一次）。"""
    try:
        _manager().recover()
    except Exception:  # noqa: BLE001
        pass


_startup_recover()

for mod in all_modules():
    app.include_router(mod.router)
    if mod.static_dir and mod.static_dir.exists():
        from fastapi.staticfiles import StaticFiles

        app.mount(f"/static/{mod.name}", StaticFiles(directory=str(mod.static_dir)),
                  name=f"static-{mod.name}")

# 导航：模板里直接调 nav_items()
templates.env.globals["nav_items"] = nav_items
