"""The key builder accepts UUIDs and nothing else."""

from uuid import UUID, uuid4

import pytest

from app.storage.content_types import detect_content_type
from app.storage.keys import assert_key_in_org, org_prefix, original_key

ORG, PROJECT, FILE = uuid4(), uuid4(), uuid4()


def test_a_key_is_built_from_the_three_ids():
    assert (
        original_key(ORG, PROJECT, FILE) == f"orgs/{ORG}/projects/{PROJECT}/files/{FILE}/original"
    )
    assert org_prefix(ORG) == f"orgs/{ORG}/"


@pytest.mark.parametrize(
    "value",
    [
        str(uuid4()),  # right shape, wrong type: strings are never accepted
        "../../etc/passwd",
        "..",
        "a/b",
        "",
        None,
        42,
        b"bytes",
        str(uuid4()).upper(),
        f"{uuid4()}/../{uuid4()}",
        "%2e%2e%2f",
        "${filename}",
    ],
)
@pytest.mark.parametrize("position", range(3))
def test_anything_that_is_not_a_uuid_object_is_refused(value, position):
    ids: list = [ORG, PROJECT, FILE]
    ids[position] = value

    with pytest.raises(TypeError):
        original_key(*ids)


def test_a_org_prefix_needs_a_uuid_too():
    with pytest.raises(TypeError):
        org_prefix("not-a-uuid")


def test_keys_are_always_canonical_lower_case():
    shouty = UUID(str(ORG).upper())
    assert original_key(shouty, PROJECT, FILE) == original_key(ORG, PROJECT, FILE)


def test_a_key_belongs_to_its_own_org_only():
    key = original_key(ORG, PROJECT, FILE)

    assert_key_in_org(key, ORG)
    with pytest.raises(ValueError, match="does not belong"):
        assert_key_in_org(key, uuid4())


@pytest.mark.parametrize(
    "suffix",
    [
        "/../orgs/x",
        "/extra",
        "//",
        "\n",
        "?versionId=1",
    ],
)
def test_malformed_keys_are_refused(suffix):
    with pytest.raises(ValueError):
        assert_key_in_org(original_key(ORG, PROJECT, FILE) + suffix, ORG)


@pytest.mark.parametrize(
    "key",
    [
        f"orgs/{ORG}/projects/{PROJECT}/files/{FILE}",
        f"orgs/{ORG}/../orgs/{ORG}/projects/{PROJECT}/files/{FILE}/original",
        f"/orgs/{ORG}/projects/{PROJECT}/files/{FILE}/original",
        f"orgs/{str(ORG).upper()}/projects/{PROJECT}/files/{FILE}/original",
        f"orgs/{ORG}/projects/{PROJECT}/files/not-a-uuid/original",
        f"prefix/orgs/{ORG}/projects/{PROJECT}/files/{FILE}/original",
        "",
    ],
)
def test_keys_outside_the_layout_are_refused(key):
    with pytest.raises(ValueError):
        assert_key_in_org(key, ORG)


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"\xff\xd8\xff\xe0" + b"0" * 8, "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n" + b"0" * 4, "image/png"),
        (b"RIFF\x24\x00\x00\x00WEBP", "image/webp"),
        (b"RIFF\x24\x00\x00\x00WAVE", None),
        (b"GIF89a" + b"0" * 6, None),
        (b"<html>", None),
        (b"%PDF-1.7", None),
        (b"", None),
    ],
)
def test_content_is_recognised_by_its_leading_bytes(head, expected):
    assert detect_content_type(head) == expected
