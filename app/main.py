"""Web application for the institutional-repository harvester.

Pages
-----
``/``                          catalogue browser, watermark, run controls
``/runs``                      round history (windows, attempts, restarts, errors)
``/rules``                     subject rules editor with preview / confirm
``/status``                    statically rendered status JSON
``GET  /api/status``           polling endpoint used by the page
``GET  /api/catalog``          catalogue JSON (search + pagination, with subject)
``POST /api/harvest``          start a round
``POST /api/harvest/cancel``   cancel the active round without waiting for it
``POST /api/harvest/restart``  cancel (if any) and immediately start a new round
``GET  /api/rules``            subject rules + a dry-run preview on the catalogue
``POST /api/rules``            create or update a rule
``DELETE /api/rules/{id}``     delete a rule
``POST /api/rules/confirm``    publish the subjects implied by the current rules
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .harvest import HarvestConfig, Harvester
from .store import Store

BASE_DIR = Path(__file__).resolve().parent


def _data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "./data"))


def _oai_url() -> str:
    return os.environ.get("OAI_URL", "http://demo-oai:8000/oai")


def create_app(
    *,
    db_path: str | Path | None = None,
    oai_url: str | None = None,
    read_timeout: float | None = None,
    max_attempts: int | None = None,
    client_factory: Any = None,
) -> FastAPI:
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(db_path or (data_dir / "harvester.sqlite3"))
    config = HarvestConfig(
        oai_url=oai_url or _oai_url(),
        read_timeout=float(
            read_timeout
            if read_timeout is not None
            else os.environ.get("OAI_READ_TIMEOUT", "20")
        ),
        connect_timeout=float(os.environ.get("OAI_CONNECT_TIMEOUT", "10")),
        max_attempts=int(
            max_attempts
            if max_attempts is not None
            else os.environ.get("HARVEST_MAX_ATTEMPTS", "3")
        ),
        retry_backoff_ms=int(os.environ.get("HARVEST_RETRY_BACKOFF_MS", "300")),
    )
    harvester = Harvester(store, config, client_factory=client_factory)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # A fresh process owns no in-flight rounds; Store already marked any
        # 'running' rows from a previous process as failed without publishing.
        yield
        await harvester.shutdown()
        store.close()

    app = FastAPI(title="机构库采集控制台", lifespan=lifespan)
    app.state.store = store
    app.state.harvester = harvester
    app.state.config = config
    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    static_dir = BASE_DIR / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def status_payload() -> dict[str, Any]:
        active = store.get_active_run()
        published = store.published_state()
        last = store.list_runs(limit=1)
        return {
            "active": active,
            "published": published,
            "last_run": last[0] if last else None,
            "config": {
                "oai_url": config.oai_url,
                "max_attempts": config.max_attempts,
                "read_timeout": config.read_timeout,
            },
        }

    @app.get("/", response_class=HTMLResponse)
    async def index(
        request: Request,
        q: str = "",
        page: int = 1,
        page_size: int = 20,
        message: str | None = None,
    ) -> HTMLResponse:
        catalogue = store.catalog_page(q, page, page_size)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "catalogue": catalogue,
                "published": store.published_state(),
                "status": status_payload(),
                "runs": store.list_runs(limit=10),
                "message": message,
            },
        )

    @app.get("/runs", response_class=HTMLResponse)
    async def runs_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "runs.html",
            {
                "runs": store.list_runs(limit=50),
                "published": store.published_state(),
                "status": status_payload(),
            },
        )

    @app.get("/api/status")
    async def api_status() -> JSONResponse:
        return JSONResponse(status_payload())

    @app.get("/status")
    async def status_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "status.html", {"status": status_payload()}
        )

    @app.post("/api/harvest")
    async def start_harvest() -> JSONResponse:
        try:
            control = await harvester.start_run()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(
            {"run_id": control.run_id, "status": "running"}, status_code=202
        )

    @app.post("/api/harvest/cancel")
    async def cancel_harvest() -> JSONResponse:
        active = store.get_active_run()
        if active is None:
            raise HTTPException(status_code=409, detail="当前没有正在运行的采集轮次")
        await harvester.cancel_run(active["id"], "用户在网页取消", "user")
        return JSONResponse({"run_id": active["id"], "status": "cancelled"})

    @app.post("/api/harvest/restart")
    async def restart_harvest() -> JSONResponse:
        """Cancel the active round (if any) and immediately start a new one.

        ``cancel_run`` returns as soon as SQLite has recorded the cancellation
        and the old asyncio task has been asked to stop; it never waits for a
        slow HTTP page.  The new run gets a new ``run_id`` and its own window,
        and the commit ownership check makes the old round unable to publish.
        """
        active = store.get_active_run()
        if active is not None:
            await harvester.cancel_run(
                active["id"], "用户取消了本轮以开始新一轮", "superseded"
            )
        try:
            control = await harvester.start_run()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(
            {"run_id": control.run_id, "status": "running"}, status_code=202
        )

    @app.get("/api/catalog")
    async def api_catalog(q: str = "", page: int = 1, page_size: int = 20) -> JSONResponse:
        return JSONResponse(store.catalog_page(q, page, page_size))

    # ----------------------------------------------------------- subject rules
    @app.get("/rules", response_class=HTMLResponse)
    async def rules_page(request: Request, message: str | None = None) -> HTMLResponse:
        preview = store.preview_subject_rules()
        return templates.TemplateResponse(
            request,
            "rules.html",
            {
                "rules": store.list_subject_rules(),
                "preview": preview,
                "published": store.published_state(),
                "status": status_payload(),
                "message": message,
            },
        )

    @app.get("/api/rules")
    async def api_rules_preview() -> JSONResponse:
        return JSONResponse(store.preview_subject_rules())

    @app.post("/api/rules")
    async def api_upsert_rule(
        keyword: str = Form(...),
        subject: str = Form(...),
        priority: int = Form(...),
        id: int | None = Form(None),
    ) -> JSONResponse:
        try:
            rule = store.upsert_subject_rule(keyword, subject, priority, id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"规则 {exc.args[0]} 不存在") from exc
        return JSONResponse(
            {"rule": rule, "preview": store.preview_subject_rules()}, status_code=201
        )

    @app.delete("/api/rules/{rule_id}")
    async def api_delete_rule(rule_id: int) -> JSONResponse:
        if not store.delete_subject_rule(rule_id):
            raise HTTPException(status_code=404, detail=f"规则 {rule_id} 不存在")
        return JSONResponse({"deleted": rule_id, "preview": store.preview_subject_rules()})

    @app.post("/api/rules/confirm")
    async def api_confirm_rules(
        rules_version: int = Form(...), catalog_version: int = Form(...)
    ) -> JSONResponse:
        result = store.confirm_subject_rules(
            rules_version=rules_version, catalog_version=catalog_version
        )
        if result.get("confirmed"):
            return JSONResponse(result)
        if result.get("reason") == "stale":
            # Refuse the stale confirmation and hand back the current picture
            # so the page can show what would happen now.
            return JSONResponse(
                {
                    "confirmed": False,
                    "reason": "stale",
                    "detail": "预览后规则或目录已变化，旧确认已拒绝，请查看当前预览后重新确认",
                    "current": store.preview_subject_rules(),
                },
                status_code=409,
            )
        # Conflicts detected at commit time.
        return JSONResponse(
            {
                "confirmed": False,
                "reason": "conflict",
                "detail": "存在最高优先级主题冲突，拒绝改变已发布结果，请先调整规则",
                "conflicts": result.get("conflicts", []),
                "current": store.preview_subject_rules(),
            },
            status_code=409,
        )

    # No-JavaScript fallbacks: plain form posts that end with a redirect.
    @app.post("/rules/save")
    async def form_save_rule(
        keyword: str = Form(...),
        subject: str = Form(...),
        priority: str = Form(...),
        id: str = Form(""),
    ) -> RedirectResponse:
        try:
            rule_id = int(id) if id.strip() else None
            store.upsert_subject_rule(keyword, subject, priority, rule_id)
            message = "规则已保存（未确认前不改变已发布主题），请查看预览"
        except ValueError as exc:
            message = f"规则保存失败：{exc}"
        except KeyError:
            message = "规则不存在，可能已被删除"
        return RedirectResponse(f"/rules?message={quote(message)}", status_code=303)

    @app.post("/rules/{rule_id}/delete")
    async def form_delete_rule(rule_id: int) -> RedirectResponse:
        if store.delete_subject_rule(rule_id):
            message = "规则已删除（未确认前不改变已发布主题），请查看预览"
        else:
            message = "规则不存在，可能已被删除"
        return RedirectResponse(f"/rules?message={quote(message)}", status_code=303)

    return app


app = create_app()
