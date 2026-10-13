"""OmniAgent 2.0 - single FastAPI server: JSON API under /api and the static frontend at /."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import admin, auth, chat, documents
from .config import FRONTEND_DIR, get_settings
from .db import session_factory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("omniagent")

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        session_factory()  # lazily creates the application engine (tables come from `scripts.setup`)
        log.info("OmniAgent started (env=%s, rag=%s)", settings.app_env, settings.rag_enabled)
        yield

    app = FastAPI(
        title="OmniAgent 2.0", lifespan=lifespan,
        docs_url=None if settings.is_production else "/api/docs", redoc_url=None,
        openapi_url=None if settings.is_production else "/api/openapi.json",
    )

    @app.middleware("http")
    async def security(request: Request, call_next):
        # Browser cross-site protection for state-changing API calls (in addition to CSRF tokens).
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") not in settings.origins:
                return JSONResponse({"detail": "Origin not allowed."}, status_code=403)
        response = await call_next(request)
        response.headers.update({
            "Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Referrer-Policy": "same-origin", "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        })
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError):
        msgs = [f"{'.'.join(str(p) for p in e['loc'][1:])}: {e['msg']}" for e in exc.errors()[:5]]
        return JSONResponse({"detail": "Invalid request. " + "; ".join(msgs)}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception):
        log.exception("unhandled error")  # details stay in server logs only
        return JSONResponse({"detail": "Something went wrong on the server."}, status_code=500)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    for r in (auth.router, chat.router, documents.router, admin.router):
        app.include_router(r)

    if FRONTEND_DIR.exists():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app


app = create_app()
