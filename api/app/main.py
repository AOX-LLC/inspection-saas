"""FastAPI application: health, auth, projects and file transfer."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.auth.ratelimit import LoginRateLimiter
from app.auth.router import router as auth_router
from app.config import AppEnv, get_settings
from app.db.engine import create_engine, create_session_factory
from app.files.router import router as files_router
from app.http_security import protect
from app.projects.router import router as projects_router
from app.storage.s3 import ObjectStore

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    engine = create_engine(settings.app_database_url())
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.object_store = ObjectStore(settings)
    # In memory: correct for one API process. See app/auth/ratelimit.py.
    app.state.login_limiter = LoginRateLimiter()
    try:
        yield
    finally:
        await engine.dispose()


def create_app() -> FastAPI:
    is_production = get_settings().app_env is AppEnv.PRODUCTION
    app = FastAPI(
        title="Inspection API",
        lifespan=lifespan,
        docs_url=None if is_production else "/docs",
        redoc_url=None,
        openapi_url=None if is_production else "/openapi.json",
    )
    app.middleware("http")(protect)
    app.add_api_route("/health", health, methods=["GET"], include_in_schema=False)
    app.include_router(auth_router)
    app.include_router(projects_router)
    app.include_router(files_router)
    return app


async def health(request: Request) -> JSONResponse:
    """Liveness plus a database round trip. Reports status only, never detail."""
    try:
        async with request.app.state.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError):
        logger.exception("health check: database unreachable")
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok"})


app = create_app()
