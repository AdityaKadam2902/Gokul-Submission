"""FastAPI application factory + normalised error payloads.

Responsibility: app assembly only — CORS (explicit localhost origins),
correlation-id middleware, exception handlers that convert typed
exceptions to a consistent JSON error shape, and router mounting.

Run (localhost-only per ARCHITECTURE §8):
    python -m uvicorn backend.app.api.main:app --host 127.0.0.1 --port 8000

OpenAPI docs are auto-generated at /docs and /openapi.json; the frontend
generates its TypeScript types from /openapi.json (D19).
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.app import __version__
from backend.app.core.config import get_settings
from backend.app.core.exceptions import PracticeSignalError
from backend.app.core.logging import configure_logging, correlation_id, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    from backend.app.db.base import get_engine, init_db

    init_db(get_engine())  # reproducible create-on-boot (D9); idempotent
    logger.info("startup_complete app_version=%s", __version__)
    yield
    logger.info("shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="PracticeSignal API",
        version=__version__,
        description=(
            "PracticeSignal — Healthcare Practice & Workforce Intelligence "
            "(M1 vertical slice: sources, fixtures, parsing, snapshots). "
            "Synthetic fixtures only; no live collection in this milestone."
        ),
        lifespan=lifespan,
    )

    # Explicit localhost-only CORS (M1-AC 7): the two dev origins, nothing wild.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-Correlation-Id"],
        expose_headers=["X-Correlation-Id"],
    )

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):
        token = correlation_id.set(uuid.uuid4().hex[:12])
        try:
            response = await call_next(request)
        finally:
            correlation_id.reset(token)
        response.headers["X-Correlation-Id"] = correlation_id.get()
        return response

    @app.exception_handler(PracticeSignalError)
    async def typed_error_handler(_request: Request, exc: PracticeSignalError):
        logger.warning(
            "api_typed_error code=%s status=%d details=%s",
            exc.error_code, exc.http_status, exc.details,
        )
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.error_code, "message": exc.message, "details": exc.details}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "validation_error", "message": "request payload failed schema validation",
                               "details": exc.errors()}},
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_request: Request, exc: Exception):
        # 5xx: full stack in application logs only, never in the HTTP response.
        logger.error("api_unexpected_error type=%s", type(exc).__name__, exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "unexpected internal error; logged for review",
                               "details": {}}},
        )

    from backend.app.api.routers import (
        changes,
        discrepancies,
        facts,
        freshness,
        health,
        lifecycle,
        opportunities,
        organisations,
        prompt_runs,
        reviews,
        snapshots,
        sources,
    )

    app.include_router(health.router)
    app.include_router(snapshots.router)
    app.include_router(sources.router)
    app.include_router(facts.router)
    app.include_router(prompt_runs.router)
    app.include_router(organisations.router)
    app.include_router(discrepancies.router)
    app.include_router(changes.router)
    app.include_router(freshness.router)
    app.include_router(opportunities.router)
    app.include_router(reviews.router)
    app.include_router(lifecycle.router)

    @app.get("/", tags=["meta"])
    def root() -> dict:
        return {"app": "PracticeSignal", "version": __version__, "docs": "/docs"}

    return app


app = create_app()
