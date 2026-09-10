"""Application assembly: middleware, routers, lifespan.

Run with ``python scripts/dev.py api`` (uvicorn ``app.main:app``). The process owns both
surface and the in-process run workers; see ``docs/DEMO.md`` for the deployment
shape and its known limitations.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.context import AppContext
from app.core.config import Settings, get_settings

# Configure logging from Settings, not from os.environ directly. Reading the environment
# here bypassed the single configuration path, so LOG_LEVEL was accepted, documented, and
# then ignored — the setting had no effect at all.
logging.basicConfig(
    level=get_settings().log_level.upper(),
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)r}',
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    app.state.ctx = AppContext.create(settings)
    _warn_about_auth_mode(settings)
    logger.info("service started (auth_mode=%s)", settings.auth_mode)
    try:
        yield
    finally:
        # Cancelling in-flight runs on the way out means a restart leaves clean
        # "failed" records instead of runs stuck in "running" forever.
        await app.state.ctx.shutdown()
        logger.info("service stopped")


def _warn_about_auth_mode(settings: Settings) -> None:
    """Say out loud when the running configuration is only safe behind a proxy.

    Both modes below authenticate nobody unless something else does it first. That is a
    legitimate deployment, but it is indistinguishable from a mistake at runtime — so it is
    logged at warning level rather than left for a reader of the config to notice.
    """
    if settings.auth_mode == "disabled":
        logger.warning(
            "AUTH_MODE=disabled: every caller is treated as an admin. Local development only."
        )
    elif settings.auth_mode == "trusted_header":
        logger.warning(
            "AUTH_MODE=trusted_header: identity is taken from the %r header without "
            "verification, and every such caller gets role %r. This is safe only if the "
            "service's port is unreachable except through the proxy that sets that header.",
            settings.trusted_actor_header,
            settings.trusted_default_role,
        )


app = FastAPI(
    title="COBOL Analysis Platform",
    version="0.1.0",
    description=(
        "Agent pipeline that analyses COBOL legacy systems and produces a Functional "
        "Design Document, a test case workbook and a data mapping workbook, then serves "
        "the results through a knowledge base."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Actor", "Last-Event-ID"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Attach a trace id to every request and echo it back.

    Error messages reference this id, which is what makes "what happened, what to do
    next, trace id" (CODESTYLE.md §3.2) actionable rather than decorative.
    """
    trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex[:16]
    request.state.trace_id = trace_id
    response = await call_next(request)
    response.headers["X-Trace-Id"] = trace_id
    if response.status_code >= 500:
        logger.error("request failed", extra={"trace_id": trace_id, "path": request.url.path})
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Never leak a stack trace to the client; always give a traceable response."""
    trace_id = getattr(request.state, "trace_id", "unknown")
    logger.exception("unhandled error (trace_id=%s)", trace_id)
    return JSONResponse(
        status_code=500,
        content={
            "detail": ("The server hit an unexpected error. Quote the trace id when reporting it."),
            "trace_id": trace_id,
        },
    )


app.include_router(router)


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {
        "service": "cobol-analysis",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "health": "/api/v1/health",
    }
