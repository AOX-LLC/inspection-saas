"""Password hashing with argon2id.

Parameters are the OWASP minimum (19 MiB, 2 passes, 1 lane). They are modest
on purpose: hashing runs in worker threads on a small shared host, and a
semaphore caps concurrent hashes so a burst of logins cannot exhaust memory.
"""

import asyncio
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_HASHER = PasswordHasher(memory_cost=19_456, time_cost=2, parallelism=1)
_CONCURRENT_HASHES = asyncio.Semaphore(2)
# Logins waiting for a hashing slot beyond this are turned away, so a flood
# cannot queue unbounded work or starve the connection pool.
MAX_WAITING_LOGINS = 20
_waiting = 0


class LoginBusyError(Exception):
    """Too many logins are already waiting to be verified."""


# Verified against when an email is unknown, so that path costs the same as a
# real one. Derived from a random value at import, so it matches no password.
_DUMMY_HASH = _HASHER.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def _verify(stored_hash: str, password: str) -> bool:
    try:
        return _HASHER.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False


async def verify_password(stored_hash: str | None, password: str) -> bool:
    """True only when `stored_hash` is real and matches.

    Passing None (unknown email) still runs a full argon2 verification, against
    a throwaway hash, and always returns False.
    """
    global _waiting
    if _waiting >= MAX_WAITING_LOGINS:
        raise LoginBusyError
    _waiting += 1
    try:
        async with _CONCURRENT_HASHES:
            if stored_hash is None:
                await asyncio.to_thread(_verify, _DUMMY_HASH, password)
                return False
            return await asyncio.to_thread(_verify, stored_hash, password)
    finally:
        _waiting -= 1
