"""Object keys, built only from database UUIDs.

A key is a path into another system, so nothing a client sends may reach one.
Every segment is a `UUID` instance rendered in canonical form; strings are
refused, which rules out traversal, separators and encoded tricks by type
instead of by filtering. Client filenames are kept as metadata and never used.

    orgs/{org_id}/projects/{project_id}/files/{file_id}/original
    orgs/{org_id}/projects/{project_id}/files/{file_id}/upload      (staging)
    orgs/{org_id}/projects/{project_id}/photos/{photo_id}/tiles/{level}_{x}_{y}.jpg
    orgs/{org_id}/projects/{project_id}/photos/{photo_id}/thumb.jpg

Uploads are written to the staging key and copied to the original key by the
server once their content has been checked, so a presigned POST never targets
a key that anything reads from.
"""

import re
from uuid import UUID

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ORIGINAL_KEY = re.compile(
    rf"orgs/(?P<org>{_UUID})/projects/(?P<project>{_UUID})/files/(?P<file>{_UUID})/original"
)
_THUMBNAIL_KEY = re.compile(rf"orgs/(?P<org>{_UUID})/projects/{_UUID}/photos/{_UUID}/thumb\.jpg")


def _segment(value: UUID) -> str:
    if not isinstance(value, UUID):
        raise TypeError(f"object key segments must be UUIDs, got {type(value).__name__}")
    return str(value)


def org_prefix(org_id: UUID) -> str:
    return f"orgs/{_segment(org_id)}/"


def original_key(org_id: UUID, project_id: UUID, file_id: UUID) -> str:
    return f"{org_prefix(org_id)}projects/{_segment(project_id)}/files/{_segment(file_id)}/original"


def staging_key(org_id: UUID, project_id: UUID, file_id: UUID) -> str:
    """Where a client's presigned POST writes. Nothing reads from here except `complete`."""
    return f"{org_prefix(org_id)}projects/{_segment(project_id)}/files/{_segment(file_id)}/upload"


def photo_prefix(org_id: UUID, project_id: UUID, photo_id: UUID) -> str:
    return f"{org_prefix(org_id)}projects/{_segment(project_id)}/photos/{_segment(photo_id)}/"


def tile_key(org_id: UUID, project_id: UUID, photo_id: UUID, level: int, x: int, y: int) -> str:
    for name, value in (("level", level), ("x", x), ("y", y)):
        # bool is an int subclass; a tile coordinate is never a flag.
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"tile {name} must be a non-negative integer")
    return f"{photo_prefix(org_id, project_id, photo_id)}tiles/{level}_{x}_{y}.jpg"


def thumbnail_key(org_id: UUID, project_id: UUID, photo_id: UUID) -> str:
    """The one small preview of a photo, which is all a grid ever loads."""
    return f"{photo_prefix(org_id, project_id, photo_id)}thumb.jpg"


def assert_thumbnail_key_in_org(key: str, org_id: UUID) -> None:
    """Raises ValueError unless `key` is a well-formed thumbnail key inside `org_id`."""
    match = _THUMBNAIL_KEY.fullmatch(key)
    if match is None or match["org"] != _segment(org_id):
        raise ValueError("thumbnail key does not belong to this org")


def assert_key_in_org(key: str, org_id: UUID) -> None:
    """Raises ValueError unless `key` is a well-formed original key inside `org_id`."""
    match = _ORIGINAL_KEY.fullmatch(key)
    if match is None or match["org"] != _segment(org_id):
        raise ValueError("object key does not belong to this org")
