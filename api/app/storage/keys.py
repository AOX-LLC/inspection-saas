"""Object keys, built only from database UUIDs.

A key is a path into another system, so nothing a client sends may reach one.
Every segment is a `UUID` instance rendered in canonical form; strings are
refused, which rules out traversal, separators and encoded tricks by type
instead of by filtering. Client filenames are kept as metadata and never used.

    orgs/{org_id}/projects/{project_id}/files/{file_id}/original
"""

import re
from uuid import UUID

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ORIGINAL_KEY = re.compile(
    rf"orgs/(?P<org>{_UUID})/projects/(?P<project>{_UUID})/files/(?P<file>{_UUID})/original"
)


def _segment(value: UUID) -> str:
    if not isinstance(value, UUID):
        raise TypeError(f"object key segments must be UUIDs, got {type(value).__name__}")
    return str(value)


def org_prefix(org_id: UUID) -> str:
    return f"orgs/{_segment(org_id)}/"


def original_key(org_id: UUID, project_id: UUID, file_id: UUID) -> str:
    return f"{org_prefix(org_id)}projects/{_segment(project_id)}/files/{_segment(file_id)}/original"


def assert_key_in_org(key: str, org_id: UUID) -> None:
    """Raises ValueError unless `key` is a well-formed original key inside `org_id`."""
    match = _ORIGINAL_KEY.fullmatch(key)
    if match is None or match["org"] != _segment(org_id):
        raise ValueError("object key does not belong to this org")
