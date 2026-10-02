"""Resolving the caller from the session cookie."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.auth import store
from app.config import get_settings
from app.deps import EngineDep

SESSION_COOKIE = "session"
# A real token is 43 characters; anything far longer is not worth hashing.
MAX_TOKEN_LENGTH = 128


def _unauthenticated() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


async def current_principal(request: Request, engine: EngineDep) -> store.SessionPrincipal:
    """The live session behind the cookie, or 401. Revoked and expired look the same."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token or len(token) > MAX_TOKEN_LENGTH:
        raise _unauthenticated()
    settings = get_settings()
    principal = await store.resolve_session(engine, token, settings.session_idle_seconds)
    if principal is None:
        raise _unauthenticated()
    return principal


PrincipalDep = Annotated[store.SessionPrincipal, Depends(current_principal)]
