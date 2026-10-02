"""Login, logout and the current user."""

import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import store
from app.auth.deps import SESSION_COOKIE, PrincipalDep
from app.auth.passwords import verify_password
from app.config import get_settings
from app.db.tenant import user_transaction
from app.deps import EngineDep, LoginLimiterDep, SessionFactoryDep

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

# One message for every way a login can fail, so it reveals nothing about accounts.
INVALID_LOGIN = "Invalid email or password"

_USER = text("SELECT id, email, display_name FROM users WHERE id = :user_id")
_USER_ORGS = text(
    """
    SELECT o.id, o.name, m.role
    FROM memberships m JOIN orgs o ON o.id = m.org_id
    WHERE m.user_id = :user_id
    ORDER BY o.name, o.id
    """
)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    # Capped so an oversized body cannot be used to burn hashing time.
    password: str = Field(min_length=1, max_length=1024)


class OrgSummary(BaseModel):
    id: UUID
    name: str
    role: str


class Me(BaseModel):
    id: UUID
    email: str
    display_name: str
    orgs: list[OrgSummary]


async def _load_me(factory: async_sessionmaker[AsyncSession], user_id: UUID) -> Me:
    async with user_transaction(factory, user_id=user_id) as session:
        user = (await session.execute(_USER, {"user_id": user_id})).one()
        orgs = (await session.execute(_USER_ORGS, {"user_id": user_id})).all()
    return Me(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        orgs=[OrgSummary(id=o.id, name=o.name, role=o.role) for o in orgs],
    )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    engine: EngineDep,
    factory: SessionFactoryDep,
    limiter: LoginLimiterDep,
) -> Me:
    ip = _client_ip(request)
    email = body.email.strip().lower()

    wait = limiter.retry_after(ip, email)
    if wait:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts. Try again later.",
            headers={"Retry-After": str(wait)},
        )

    record = await store.find_login(engine, email)
    # Runs a full verification even when there is no such account.
    verified = await verify_password(record.password_hash if record else None, body.password)
    if record is None or not verified:
        limiter.record_failure(ip, email)
        logger.warning("login failed", extra={"client_ip": ip})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_LOGIN)

    settings = get_settings()
    token = store.new_token()
    await store.create_session(engine, record.user_id, token, settings.session_absolute_seconds)
    limiter.record_success(email)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_absolute_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_is_secure,
        path="/",
    )
    return await _load_me(factory, record.user_id)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, engine: EngineDep) -> Response:
    """Revokes the session behind the cookie. Safe to call with none."""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await store.revoke_session(engine, token)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/me")
async def me(principal: PrincipalDep, factory: SessionFactoryDep) -> Me:
    return await _load_me(factory, principal.user_id)
