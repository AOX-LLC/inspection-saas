"""X-Forwarded-For is believed only from a configured proxy; anyone else keeps their own address."""

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from app.auth.clientip import TrustedProxies, client_ip
from app.auth.ratelimit import LoginRateLimiter
from tests.api.conftest import ORIGIN, PASSWORD, World

pytestmark = pytest.mark.asyncio

WEB = "172.20.0.5"  # the web container, as the API sees its socket
OTHER = "172.20.0.99"  # anything else on the network, or straight at the published port


def make_request(peer: str | None, *forwarded: str) -> Request:
    headers = [(b"x-forwarded-for", value.encode()) for value in forwarded]
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth/login",
            "headers": headers,
            "client": (peer, 40000) if peer else None,
        }
    )


# The rule, on its own --------------------------------------------------------------


async def test_a_trusted_proxys_report_of_the_client_is_used():
    proxies = TrustedProxies.parse(WEB)

    assert await client_ip(make_request(WEB, "198.51.100.7"), proxies) == "198.51.100.7"


async def test_a_request_from_anyone_else_keeps_its_socket_address():
    proxies = TrustedProxies.parse(WEB)

    assert await client_ip(make_request(OTHER, "198.51.100.7"), proxies) == OTHER


async def test_nothing_is_trusted_by_default():
    assert await client_ip(make_request(WEB, "198.51.100.7"), TrustedProxies.parse("")) == WEB


async def test_a_spoofed_left_entry_is_ignored_because_the_proxy_appends_on_the_right():
    proxies = TrustedProxies.parse(WEB)

    # The client claimed 10.0.0.1; the proxy appended the address it really saw.
    assert await client_ip(make_request(WEB, "10.0.0.1, 198.51.100.7"), proxies) == "198.51.100.7"


async def test_trusted_hops_are_skipped_from_the_right():
    proxies = TrustedProxies.parse("172.20.0.0/24")

    request = make_request("172.20.0.5", "198.51.100.7, 172.20.0.9")

    assert await client_ip(request, proxies) == "198.51.100.7"


async def test_several_header_lines_are_read_as_one_list():
    proxies = TrustedProxies.parse(WEB)

    assert await client_ip(make_request(WEB, "10.0.0.1", "198.51.100.7"), proxies) == "198.51.100.7"


@pytest.mark.parametrize(
    "value",
    [
        "not-an-address",
        "198.51.100.7, garbage",
        "",
        ", ,",
        "198.51.100.7:8080",
        "1.1.1.1" + ",1.1.1.1" * 25,
    ],
)
async def test_a_malformed_or_oversized_header_falls_back_to_the_socket_address(value):
    assert await client_ip(make_request(WEB, value), TrustedProxies.parse(WEB)) == WEB


async def test_a_missing_header_falls_back_to_the_socket_address():
    assert await client_ip(make_request(WEB), TrustedProxies.parse(WEB)) == WEB


async def test_ipv6_clients_are_read():
    proxies = TrustedProxies.parse(WEB)

    assert await client_ip(make_request(WEB, "2001:db8::7"), proxies) == "2001:db8::7"


async def test_a_request_with_no_socket_address_is_unknown():
    assert (
        await client_ip(make_request(None, "198.51.100.7"), TrustedProxies.parse(WEB)) == "unknown"
    )


async def test_a_host_name_is_resolved_and_remembered_briefly():
    now = [0.0]
    proxies = TrustedProxies.parse("localhost", clock=lambda: now[0])

    assert await client_ip(make_request("127.0.0.1", "198.51.100.7"), proxies) == "198.51.100.7"
    first = proxies._resolved
    now[0] = 10
    await client_ip(make_request("127.0.0.1", "198.51.100.7"), proxies)
    assert proxies._resolved is first  # still the cached answer
    now[0] = 31
    await client_ip(make_request("127.0.0.1", "198.51.100.7"), proxies)
    assert proxies._resolved is not first


async def test_a_host_name_that_does_not_resolve_trusts_nobody():
    proxies = TrustedProxies.parse("no-such-proxy.invalid")

    assert await client_ip(make_request("127.0.0.1", "198.51.100.7"), proxies) == "127.0.0.1"


# Through the login limit ------------------------------------------------------------


def client_from(app: FastAPI, peer: str, forwarded: str | None = None) -> httpx.AsyncClient:
    headers = {"Origin": ORIGIN}
    if forwarded:
        headers["X-Forwarded-For"] = forwarded
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(peer, 40000)),
        base_url="http://test",
        headers=headers,
    )


async def fail_login(client: httpx.AsyncClient, n: int) -> httpx.Response:
    return await client.post(
        "/auth/login", json={"email": f"nobody{n}@test.example", "password": "x"}
    )


async def test_through_the_web_container_each_real_client_has_its_own_limit(app, world: World):
    app.state.login_limiter = LoginRateLimiter(per_ip_limit=2, per_email_limit=100)
    app.state.trusted_proxies = TrustedProxies.parse(WEB)

    async with client_from(app, WEB, "198.51.100.1") as first:
        assert [(await fail_login(first, n)).status_code for n in range(3)] == [401, 401, 429]
    async with client_from(app, WEB, "198.51.100.2") as second:
        # A different real client is not held back by the first one's failures.
        assert (await fail_login(second, 9)).status_code == 401


async def test_straight_to_the_api_a_forged_header_cannot_dodge_the_limit(app, world: World):
    app.state.login_limiter = LoginRateLimiter(per_ip_limit=2, per_email_limit=100)
    app.state.trusted_proxies = TrustedProxies.parse(WEB)

    async with client_from(app, OTHER) as direct:
        statuses = []
        for n in range(4):
            direct.headers["X-Forwarded-For"] = f"198.51.100.{n}"  # a new "client" each time
            statuses.append((await fail_login(direct, n)).status_code)

    assert statuses == [401, 401, 429, 429]


async def test_straight_to_the_api_the_forged_address_does_not_lock_out_its_owner(app, world):
    app.state.login_limiter = LoginRateLimiter(per_ip_limit=2, per_email_limit=100)
    app.state.trusted_proxies = TrustedProxies.parse(WEB)

    async with client_from(app, OTHER, "198.51.100.1") as attacker:
        for n in range(3):
            await fail_login(attacker, n)
    async with client_from(app, WEB, "198.51.100.1") as real_owner_of_that_address:
        # The attacker's failures were counted against the attacker's socket, not 198.51.100.1.
        response = await real_owner_of_that_address.post(
            "/auth/login", json={"email": world.alpha.owner.email, "password": PASSWORD}
        )

    assert response.status_code == 200


async def test_with_no_trusted_proxy_the_header_is_ignored_even_from_the_web_container(
    app, world: World
):
    app.state.login_limiter = LoginRateLimiter(per_ip_limit=2, per_email_limit=100)
    app.state.trusted_proxies = TrustedProxies.parse("")

    async with client_from(app, WEB) as web:
        statuses = []
        for n in range(3):
            web.headers["X-Forwarded-For"] = f"198.51.100.{n}"
            statuses.append((await fail_login(web, n)).status_code)

    assert statuses == [401, 401, 429]
