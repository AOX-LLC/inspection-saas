"""The object store answers cross-origin requests from the web origin only."""

from uuid import uuid4

import httpx
import pytest

from app.config import get_settings

WEB_ORIGIN = "http://127.0.0.1:4700"


def preflight(origin: str, method: str, headers: str = "") -> httpx.Response:
    settings = get_settings()
    request_headers = {"Origin": origin, "Access-Control-Request-Method": method}
    if headers:
        request_headers["Access-Control-Request-Headers"] = headers
    with httpx.Client(timeout=10) as client:
        return client.options(
            f"{settings.s3_public_endpoint}/{settings.s3_bucket}/orgs/{uuid4()}/x",
            headers=request_headers,
        )


@pytest.mark.parametrize("method", ["POST", "GET"])
def test_the_web_origin_may_upload_and_show_thumbnails(method):
    response = preflight(WEB_ORIGIN, method)

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == WEB_ORIGIN


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.example",
        "http://localhost:4700",
        "http://127.0.0.1:4701",
        "https://127.0.0.1:4700",
        "null",
    ],
)
def test_no_other_origin_is_allowed(origin):
    response = preflight(origin, "POST")

    # The store refuses the preflight. (It labels its error responses with a wildcard
    # origin, which a browser ignores: a preflight must be a success to count.)
    assert response.status_code == 403


def test_the_web_origin_may_send_only_a_content_type_header():
    allowed = preflight(WEB_ORIGIN, "POST", "content-type")
    refused = preflight(WEB_ORIGIN, "POST", "x-amz-acl")

    assert allowed.status_code == 200
    assert refused.status_code == 403


@pytest.mark.parametrize("method", ["PUT", "DELETE", "HEAD"])
def test_the_web_origin_may_not_use_other_methods(method):
    response = preflight(WEB_ORIGIN, method)

    assert response.status_code == 403


def test_the_allowed_origin_is_never_a_wildcard():
    response = preflight(WEB_ORIGIN, "POST")

    assert response.headers.get("access-control-allow-origin") != "*"
