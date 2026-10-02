"""Server-side copy and bounded reads against the real object store."""

from collections.abc import Iterator
from uuid import uuid4

import pytest

from app.config import get_settings
from app.storage.keys import original_key, staging_key
from app.storage.s3 import ObjectChanged, ObjectStore

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture(scope="module")
def store() -> ObjectStore:
    return ObjectStore(get_settings())


@pytest.fixture
def pair(store: ObjectStore) -> Iterator[tuple[str, str]]:
    org, project, file = uuid4(), uuid4(), uuid4()
    keys = (staging_key(org, project, file), original_key(org, project, file))
    yield keys
    for key in keys:
        store.delete(key)


def test_copy_moves_the_bytes_and_sets_the_content_type(store, pair):
    source, target = pair
    store.put(source, PNG, "application/octet-stream")
    etag = store.inspect(source).etag

    store.copy(source, target, content_type="image/png", if_match=etag)

    assert store.read(target, max_bytes=1000) == PNG
    head = store._operations.head_object(Bucket=get_settings().s3_bucket, Key=target)
    assert head["ContentType"] == "image/png"


def test_copy_refuses_a_source_that_changed_after_it_was_checked(store, pair):
    source, target = pair
    store.put(source, PNG, "image/png")
    etag = store.inspect(source).etag
    store.put(source, b"<html>swapped</html>", "image/png")

    with pytest.raises(ObjectChanged):
        store.copy(source, target, content_type="image/png", if_match=etag)

    assert store.inspect(target) is None


def test_read_refuses_an_object_over_the_limit(store, pair):
    source, _ = pair
    store.put(source, PNG, "image/png")

    with pytest.raises(ValueError, match="larger"):
        store.read(source, max_bytes=len(PNG) - 1)


def test_inspect_reports_size_head_and_etag(store, pair):
    source, _ = pair
    store.put(source, PNG, "image/png")

    info = store.inspect(source)

    assert (info.size_bytes, info.head) == (len(PNG), PNG[:12])
    assert info.etag
