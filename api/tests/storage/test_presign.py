"""Presigned URLs against the real object store.

Each test follows what an attacker or a mistaken client could do with a URL:
edit it, wait it out, strip its signature, or bend the POST policy.
"""

import re
import time
from collections.abc import Iterator
from uuid import uuid4

import httpx
import pytest

from app.config import get_settings
from app.storage.keys import original_key
from app.storage.s3 import MAX_PRESIGN_TTL_SECONDS, ObjectStore

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture(scope="module")
def store() -> ObjectStore:
    return ObjectStore(get_settings())


def new_key() -> str:
    return original_key(uuid4(), uuid4(), uuid4())


@pytest.fixture
def keys(store: ObjectStore) -> Iterator[list[str]]:
    created: list[str] = []
    yield created
    for key in created:
        store.delete(key)


def post_to_store(post, body: bytes, **overrides: str) -> httpx.Response:
    fields = {**post.fields, **overrides}
    return httpx.post(post.url, data=fields, files={"file": ("upload", body)}, timeout=10)


def test_a_valid_presigned_post_then_get_round_trips(store, keys):
    key = new_key()
    keys.append(key)

    post = store.presign_upload(key, "image/png", len(PNG), 60)
    stored = post_to_store(post, PNG)
    url = store.presign_download(key, "image/png", "attachment", 60)
    fetched = httpx.get(url, timeout=10)

    assert stored.status_code in (200, 201, 204), stored.text
    assert fetched.status_code == 200
    assert fetched.content == PNG


def test_the_signature_covers_one_key_only(store, keys):
    mine, other = new_key(), new_key()
    keys.extend([mine, other])
    store.put(mine, PNG, "image/png")
    store.put(other, PNG, "image/png")
    url = store.presign_download(mine, "image/png", "attachment", 60)

    edited = url.replace(mine, other)

    assert edited != url
    assert httpx.get(url, timeout=10).status_code == 200
    assert httpx.get(edited, timeout=10).status_code == 403


def test_an_expired_url_stops_working(store, keys):
    key = new_key()
    keys.append(key)
    store.put(key, PNG, "image/png")
    url = store.presign_download(key, "image/png", "attachment", 1)
    assert httpx.get(url, timeout=10).status_code == 200

    time.sleep(2.5)

    # Garage answers an expired signature with 400; other stores use 403.
    assert httpx.get(url, timeout=10).status_code in (400, 403)


def test_an_unsigned_get_is_refused(store, keys):
    key = new_key()
    keys.append(key)
    store.put(key, PNG, "image/png")
    signed = store.presign_download(key, "image/png", "attachment", 60)

    unsigned = signed.split("?")[0]

    assert httpx.get(unsigned, timeout=10).status_code == 403


def test_the_bucket_cannot_be_listed_anonymously(store):
    bucket_url = get_settings().s3_public_endpoint + "/" + get_settings().s3_bucket

    assert httpx.get(bucket_url + "?list-type=2", timeout=10).status_code == 403
    assert httpx.get(bucket_url + "/", timeout=10).status_code == 403


def test_a_changed_response_header_breaks_the_signature(store, keys):
    key = new_key()
    keys.append(key)
    store.put(key, PNG, "image/png")
    url = store.presign_download(key, "image/png", "attachment", 60)

    swapped = re.sub(r"response-content-type=[^&]+", "response-content-type=text%2Fhtml", url)

    assert swapped != url
    assert httpx.get(swapped, timeout=10).status_code == 403


def test_downloads_force_type_and_disposition(store, keys):
    key = new_key()
    keys.append(key)
    store.put(key, PNG, "text/html")

    response = httpx.get(
        store.presign_download(key, "image/png", 'attachment; filename="x.png"', 60), timeout=10
    )

    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"] == 'attachment; filename="x.png"'


def test_a_post_larger_than_the_policy_is_rejected(store, keys):
    key = new_key()
    keys.append(key)
    post = store.presign_upload(key, "image/png", 100, 60)

    response = post_to_store(post, PNG + b"0" * 500)

    assert response.status_code >= 400
    assert store.inspect(key) is None


def test_a_post_cannot_name_a_different_key(store, keys):
    signed_key, other_key = new_key(), new_key()
    keys.extend([signed_key, other_key])
    post = store.presign_upload(signed_key, "image/png", len(PNG), 60)

    response = post_to_store(post, PNG, key=other_key)

    assert response.status_code >= 400
    assert store.inspect(other_key) is None
    assert store.inspect(signed_key) is None


@pytest.mark.parametrize("claimed", ["text/html", "image/svg+xml", "image/jpeg"])
def test_a_post_cannot_change_the_content_type(store, keys, claimed):
    key = new_key()
    keys.append(key)
    post = store.presign_upload(key, "image/png", len(PNG), 60)

    response = post_to_store(post, PNG, **{"Content-Type": claimed})

    assert response.status_code >= 400
    assert store.inspect(key) is None


def test_a_post_with_a_stripped_policy_is_rejected(store, keys):
    key = new_key()
    keys.append(key)
    post = store.presign_upload(key, "image/png", len(PNG), 60)
    without_policy = {k: v for k, v in post.fields.items() if k.lower() != "policy"}

    response = httpx.post(
        post.url, data=without_policy, files={"file": ("upload", PNG)}, timeout=10
    )

    assert response.status_code >= 400
    assert store.inspect(key) is None


def test_an_expired_post_is_rejected(store, keys):
    key = new_key()
    keys.append(key)
    post = store.presign_upload(key, "image/png", len(PNG), 1)
    time.sleep(2.5)

    assert post_to_store(post, PNG).status_code >= 400
    assert store.inspect(key) is None


@pytest.mark.parametrize("content_type", ["text/html", "image/svg+xml", "image/gif", ""])
def test_the_policy_only_ever_allows_the_three_image_types(store, content_type):
    with pytest.raises(ValueError, match="not allowed"):
        store.presign_upload(new_key(), content_type, 10, 60)


@pytest.mark.parametrize("size", [0, -1, 50 * 1024 * 1024 + 1])
def test_the_policy_size_range_is_bounded(store, size):
    with pytest.raises(ValueError, match="size"):
        store.presign_upload(new_key(), "image/png", size, 60)


def test_no_url_lives_longer_than_the_cap(store):
    url = store.presign_download(new_key(), "image/png", "attachment", 10**9)
    expires = int(re.search(r"X-Amz-Expires=(\d+)", url).group(1))
    post = store.presign_upload(new_key(), "image/png", 10, 10**9)

    assert expires == MAX_PRESIGN_TTL_SECONDS
    assert post.fields  # a policy was produced even with an absurd request


def test_urls_are_signed_for_the_public_endpoint(store):
    url = store.presign_download(new_key(), "image/png", "attachment", 60)

    assert url.startswith(get_settings().s3_public_endpoint + "/" + get_settings().s3_bucket + "/")
