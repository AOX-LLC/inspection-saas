"""The app's only route to credentials and sessions: the `auth.*` SQL functions.

The app role cannot read either table. These calls run with no tenant context
because none exists yet at login. They are plain function calls on a short
connection, never a tenant session.
"""

import hashlib
import secrets
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_VERIFY_LOGIN = text("SELECT user_id, password_hash FROM auth.verify_login(:email)")
_CREATE_SESSION = text("SELECT auth.create_session(:user_id, :token_sha256, :absolute_seconds)")
_RESOLVE_SESSION = text(
    "SELECT session_id, user_id FROM auth.resolve_session(:token_sha256, :idle)"
)
_REVOKE_SESSION = text("SELECT auth.revoke_session(:token_sha256)")


@dataclass(frozen=True)
class LoginRecord:
    user_id: UUID
    password_hash: str


@dataclass(frozen=True)
class SessionPrincipal:
    session_id: UUID
    user_id: UUID


def new_token() -> str:
    """256 random bits, URL-safe. Shown to the client once; only its hash is stored."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def find_login(engine: AsyncEngine, email: str) -> LoginRecord | None:
    async with engine.begin() as connection:
        row = (await connection.execute(_VERIFY_LOGIN, {"email": email})).first()
    return (
        None if row is None else LoginRecord(user_id=row.user_id, password_hash=row.password_hash)
    )


async def create_session(
    engine: AsyncEngine, user_id: UUID, token: str, absolute_seconds: int
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            _CREATE_SESSION,
            {
                "user_id": user_id,
                "token_sha256": hash_token(token),
                "absolute_seconds": absolute_seconds,
            },
        )


async def resolve_session(
    engine: AsyncEngine, token: str, idle_seconds: int
) -> SessionPrincipal | None:
    async with engine.begin() as connection:
        row = (
            await connection.execute(
                _RESOLVE_SESSION, {"token_sha256": hash_token(token), "idle": idle_seconds}
            )
        ).first()
    return None if row is None else SessionPrincipal(session_id=row.session_id, user_id=row.user_id)


async def revoke_session(engine: AsyncEngine, token: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(_REVOKE_SESSION, {"token_sha256": hash_token(token)})
