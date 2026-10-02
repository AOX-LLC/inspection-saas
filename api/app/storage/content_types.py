"""The only content types an upload may have, and how to recognise them by content.

The declared type is a claim. On completion the first bytes of the object must
match it, so a client cannot upload HTML or script under an image type.
"""

ALLOWED_CONTENT_TYPES: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}

# Enough to cover every signature below.
SNIFF_BYTES = 12


def detect_content_type(head: bytes) -> str | None:
    """The allowed content type the leading bytes match, or None."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None
