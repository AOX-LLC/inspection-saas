"""Shared FastAPI dependencies: the objects the lifespan puts on app.state."""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.auth.ratelimit import LoginRateLimiter
from app.storage.s3 import ObjectStore


def get_engine(request: Request) -> AsyncEngine:
    return request.app.state.engine


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.session_factory


def get_object_store(request: Request) -> ObjectStore:
    return request.app.state.object_store


def get_login_limiter(request: Request) -> LoginRateLimiter:
    return request.app.state.login_limiter


EngineDep = Annotated[AsyncEngine, Depends(get_engine)]
SessionFactoryDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
ObjectStoreDep = Annotated[ObjectStore, Depends(get_object_store)]
LoginLimiterDep = Annotated[LoginRateLimiter, Depends(get_login_limiter)]
