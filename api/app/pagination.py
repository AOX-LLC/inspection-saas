"""Keyset pagination: an opaque cursor that says where the previous page ended.

Lists are ordered by `(created_at, id)`, which is unique, and a page starts
strictly after (or, for newest-first lists, before) the row the cursor names.
Unlike an offset, the cost of a page does not grow with its depth, and rows
added meanwhile never shift a page's contents.

A cursor holds only a timestamp and an id. It grants nothing: every query that
uses one still runs under the caller's tenant context.
"""

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException, status

MAX_CURSOR_LENGTH = 200


@dataclass(frozen=True)
class Cursor:
    created_at: datetime
    id: UUID

    def encode(self) -> str:
        raw = f"{self.created_at.isoformat()}|{self.id}".encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str | None) -> Cursor | None:
    """None for no cursor; a 422 for one that is not ours."""
    if value is None:
        return None
    try:
        if len(value) > MAX_CURSOR_LENGTH:
            raise ValueError("cursor too long")
        padded = value + "=" * (-len(value) % 4)
        stamp, _, row_id = base64.urlsafe_b64decode(padded).decode().partition("|")
        created_at = datetime.fromisoformat(stamp)
        if created_at.tzinfo is None:
            raise ValueError("cursor timestamp has no zone")
        return Cursor(created_at=created_at, id=UUID(row_id))
    except (ValueError, binascii.Error):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid cursor"
        ) from None


def next_cursor(rows: list, limit: int) -> str | None:
    """The cursor for the page after `rows[:limit]`, or None on the last page.

    Callers fetch `limit + 1` rows; the extra one says another page exists.
    """
    if len(rows) <= limit:
        return None
    last = rows[limit - 1]
    return Cursor(created_at=last.created_at, id=last.id).encode()
