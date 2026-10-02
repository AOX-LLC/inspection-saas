"""Login, sessions, logout, the Origin check and the login rate limit."""

import hashlib

import httpx
import pytest

from app.auth import passwords
from app.auth.ratelimit import LoginRateLimiter, SlidingWindowCounter
from tests.api.conftest import ORIGIN, PASSWORD, World, as_auth_role, login, new_client

pytestmark = pytest.mark.asyncio


async def test_login_sets_a_hardened_cookie_and_returns_the_user(anonymous, world: World):
    response = await anonymous.post(
        "/auth/login", json={"email": world.alpha.owner.email, "password": PASSWORD}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == world.alpha.owner.email
    assert [(o["id"], o["role"]) for o in body["orgs"]] == [(str(world.alpha.id), "owner")]
    assert "password" not in response.text

    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie
    assert "max-age=604800" in cookie


async def test_cookie_is_marked_secure_in_production(world: World, monkeypatch):
    from app.config import get_settings
    from app.main import create_app

    monkeypatch.setenv("COOKIE_SECURE", "true")
    get_settings.cache_clear()
    try:
        app = create_app()
        async with app.router.lifespan_context(app), new_client(app) as client:
            response = await client.post(
                "/auth/login", json={"email": world.alpha.owner.email, "password": PASSWORD}
            )
        assert "secure" in response.headers["set-cookie"].lower().split("; ")
    finally:
        monkeypatch.delenv("COOKIE_SECURE")
        get_settings.cache_clear()


async def test_responses_carry_security_headers(anonymous):
    response = await anonymous.get("/auth/me")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


async def test_the_token_is_stored_only_as_a_hash(anonymous, world: World):
    response = await anonymous.post(
        "/auth/login", json={"email": world.alpha.viewer.email, "password": PASSWORD}
    )
    token = response.cookies["session"]

    rows = as_auth_role(
        "SELECT token_sha256, sessions::text FROM sessions WHERE user_id = %s",
        (world.alpha.viewer.id,),
    )
    assert hashlib.sha256(token.encode()).digest() in {row[0] for row in rows}
    assert all(token not in row[1] for row in rows)


async def test_me_requires_a_session_and_returns_the_user(anonymous, signed_in, world: World):
    assert (await anonymous.get("/auth/me")).status_code == 401

    client = await signed_in(world.consultant)
    body = (await client.get("/auth/me")).json()
    assert {o["id"]: o["role"] for o in body["orgs"]} == {
        str(world.alpha.id): "inspector",
        str(world.beta.id): "inspector",
    }


async def test_wrong_password_and_unknown_email_look_identical(anonymous, world: World):
    wrong_password = await anonymous.post(
        "/auth/login", json={"email": world.alpha.owner.email, "password": "not the password"}
    )
    unknown_email = await anonymous.post(
        "/auth/login", json={"email": "nobody@test.example", "password": "not the password"}
    )

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()
    assert "set-cookie" not in wrong_password.headers
    assert "set-cookie" not in unknown_email.headers


async def test_an_unknown_email_still_runs_a_full_password_verification(
    anonymous, world: World, monkeypatch
):
    calls: list[str] = []
    real_verify = passwords._verify

    def counting_verify(stored_hash: str, password: str) -> bool:
        calls.append(stored_hash)
        return real_verify(stored_hash, password)

    monkeypatch.setattr(passwords, "_verify", counting_verify)

    await anonymous.post("/auth/login", json={"email": "nobody@test.example", "password": "x"})
    assert len(calls) == 1
    assert calls[0].startswith("$argon2id$")

    await anonymous.post(
        "/auth/login", json={"email": world.alpha.owner.email, "password": "wrong"}
    )
    assert len(calls) == 2


async def test_login_normalises_the_email(anonymous, world: World):
    response = await anonymous.post(
        "/auth/login",
        json={"email": f"  {world.alpha.owner.email.upper()} ", "password": PASSWORD},
    )
    assert response.status_code == 200


async def test_logout_revokes_the_session(signed_in, app, world: World):
    client = await signed_in(world.alpha.owner)
    token = client.cookies["session"]
    assert (await client.get("/auth/me")).status_code == 200

    response = await client.post("/auth/logout")
    assert response.status_code == 204

    # The old cookie, replayed from a fresh client, is dead server-side.
    async with new_client(app) as replay:
        replay.cookies.set("session", token)
        assert (await replay.get("/auth/me")).status_code == 401


async def test_logout_without_a_session_is_harmless(anonymous):
    assert (await anonymous.post("/auth/logout")).status_code == 204


async def test_an_idle_session_is_rejected(signed_in, world: World):
    client = await signed_in(world.alpha.inspector)
    as_auth_role(
        "UPDATE sessions SET last_seen_at = now() - interval '13 hours' WHERE user_id = %s",
        (world.alpha.inspector.id,),
    )

    assert (await client.get("/auth/me")).status_code == 401


async def test_a_session_past_its_absolute_limit_is_rejected(signed_in, world: World):
    client = await signed_in(world.beta.inspector)
    as_auth_role(
        "UPDATE sessions SET expires_at = now() - interval '1 second' WHERE user_id = %s",
        (world.beta.inspector.id,),
    )

    assert (await client.get("/auth/me")).status_code == 401


@pytest.mark.parametrize("cookie", ["", "garbage", "x" * 500, "a" * 43])
async def test_bad_cookies_are_rejected(anonymous, cookie):
    anonymous.cookies.set("session", cookie)
    assert (await anonymous.get("/auth/me")).status_code == 401


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_unsafe_methods_need_an_allowed_origin(app, fresh_limiter, method):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as bare:
        missing = await bare.request(method, "/auth/login")
        foreign = await bare.request(method, "/auth/login", headers={"Origin": "https://evil.test"})
        allowed = await bare.request(method, "/auth/login", headers={"Origin": ORIGIN})

    assert missing.status_code == 403
    assert foreign.status_code == 403
    # An allowed origin gets past the check; the route then answers on its own terms.
    assert allowed.status_code != 403


async def test_safe_methods_do_not_need_an_origin(app, fresh_limiter):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as bare:
        assert (await bare.get("/health")).status_code == 200


async def test_login_accepts_json_only(anonymous, world: World):
    response = await anonymous.post(
        "/auth/login",
        content=f"email={world.alpha.owner.email}&password={PASSWORD}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 422
    plain = await anonymous.post(
        "/auth/login",
        content='{"email": "a@b.example", "password": "x"}',
        headers={"Content-Type": "text/plain"},
    )
    assert plain.status_code == 422


async def test_login_rejects_unknown_fields_and_oversized_passwords(anonymous, world: World):
    extra = await anonymous.post(
        "/auth/login",
        json={"email": world.alpha.owner.email, "password": PASSWORD, "admin": True},
    )
    huge = await anonymous.post(
        "/auth/login", json={"email": world.alpha.owner.email, "password": "x" * 5000}
    )
    assert extra.status_code == huge.status_code == 422


async def test_health_stays_status_only(anonymous):
    response = await anonymous.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# Rate limiting ------------------------------------------------------------------


async def test_failed_logins_for_one_email_are_throttled(app, world: World):
    app.state.login_limiter = LoginRateLimiter(per_ip_limit=100, per_email_limit=3)
    async with new_client(app) as client:
        for _ in range(3):
            response = await client.post(
                "/auth/login", json={"email": world.alpha.owner.email, "password": "wrong"}
            )
            assert response.status_code == 401

        blocked = await client.post(
            "/auth/login", json={"email": world.alpha.owner.email, "password": PASSWORD}
        )
        other_email = await client.post(
            "/auth/login", json={"email": world.alpha.viewer.email, "password": PASSWORD}
        )

    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0
    assert other_email.status_code == 200


async def test_failed_logins_from_one_ip_are_throttled(app, world: World):
    app.state.login_limiter = LoginRateLimiter(per_ip_limit=3, per_email_limit=100)
    async with new_client(app) as client:
        for index in range(3):
            response = await client.post(
                "/auth/login", json={"email": f"nobody{index}@test.example", "password": "x"}
            )
            assert response.status_code == 401

        blocked = await client.post(
            "/auth/login", json={"email": world.alpha.owner.email, "password": PASSWORD}
        )
    assert blocked.status_code == 429


async def test_unknown_emails_are_throttled_like_real_ones(app):
    app.state.login_limiter = LoginRateLimiter(per_ip_limit=100, per_email_limit=2)
    async with new_client(app) as client:
        statuses = [
            (
                await client.post(
                    "/auth/login", json={"email": "ghost@test.example", "password": "x"}
                )
            ).status_code
            for _ in range(3)
        ]
    assert statuses == [401, 401, 429]


async def test_a_successful_login_clears_the_email_counter(app, world: World):
    app.state.login_limiter = LoginRateLimiter(per_ip_limit=100, per_email_limit=3)
    async with new_client(app) as client:
        for _ in range(2):
            await client.post(
                "/auth/login", json={"email": world.alpha.owner.email, "password": "wrong"}
            )
        await login(client, world.alpha.owner)
        for _ in range(2):
            response = await client.post(
                "/auth/login", json={"email": world.alpha.owner.email, "password": "wrong"}
            )
            assert response.status_code == 401


async def test_the_window_slides_and_old_failures_expire():
    now = [0.0]
    counter = SlidingWindowCounter(limit=2, window_seconds=10, clock=lambda: now[0])

    counter.record("k")
    now[0] = 5
    counter.record("k")
    assert counter.retry_after("k") == 6  # the first failure leaves the window at t=10

    now[0] = 10.5
    assert counter.retry_after("k") == 0


async def test_tracked_keys_are_bounded(monkeypatch):
    import app.auth.ratelimit as ratelimit

    monkeypatch.setattr(ratelimit, "MAX_TRACKED_KEYS", 5)
    now = [0.0]
    counter = SlidingWindowCounter(limit=1, window_seconds=1000, clock=lambda: now[0])
    for index in range(50):
        now[0] = float(index)
        counter.record(f"key-{index}")

    assert len(counter._events) <= 5
    assert counter.retry_after("key-49") > 0  # the newest are kept
