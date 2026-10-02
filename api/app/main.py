"""FastAPI application. Phase 1a serves only the health check."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import AppEnv, get_settings
from app.db.engine import create_engine

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    engine = create_engine(get_settings().app_database_url())
    app.state.engine = engine
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
    app.add_api_route("/health", health, methods=["GET"], include_in_schema=False)
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
