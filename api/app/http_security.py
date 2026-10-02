"""Cross-cutting HTTP protections: the Origin check and response headers."""

from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from app.config import get_settings

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # Responses carry session state and short-lived signed URLs.
    "Cache-Control": "no-store",
}


async def protect(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Rejects state-changing requests from origins we do not serve, then adds headers.

    SameSite=Lax already withholds the cookie on cross-site POSTs; the Origin
    check is the second, independent layer. A missing Origin is rejected too:
    browsers send it on every unsafe request, so only non-browser clients omit it.
    """
    if request.method in UNSAFE_METHODS and request.headers.get("origin") not in (
        get_settings().origins
    ):
        response: Response = JSONResponse({"detail": "Origin not allowed"}, status_code=403)
    else:
        response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response
