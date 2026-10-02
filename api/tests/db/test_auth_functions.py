"""The auth.* SECURITY DEFINER functions, called as the app role.

The app role cannot read `sessions` or `credentials`, yet login and session
lookup must work with no tenant context. These tests check that the functions
do exactly that and nothing more: lifetimes are enforced and clamped, a token
is only ever stored hashed, and the owner cannot call them.
"""

import hashlib
from collections.abc import Iterator
from uuid import UUID, uuid4

import psycopg
import pytest

from tests.db.conftest import TENANT_A, TENANT_B, app_conninfo, owner_conninfo, set_context

HASH = "$argon2id$v=19$m=19456,t=2,p=1$c29tZXNhbHQ$c29tZWhhc2hfZm9yX3Rlc3Rpbmc"
ALPHA_EMAIL = f"user-{TENANT_A.user_id}@test.example"


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


@pytest.fixture(scope="module", autouse=True)
def credential_for_tenant_a(tenants) -> None:
    """A password hash for tenant A's user only. Tenant B's user has none."""
    with psycopg.connect(owner_conninfo(), autocommit=True) as connection, connection.transaction():
        set_context(connection, user_id=TENANT_A.user_id)
        connection.execute("SET LOCAL ROLE inspection_auth")
        connection.execute(
            "INSERT INTO credentials (user_id, password_hash) VALUES (%s, %s)"
            " ON CONFLICT DO NOTHING",
            (TENANT_A.user_id, HASH),
        )


@pytest.fixture
def autocommit_app() -> Iterator[psycopg.Connection]:
    """The app role with each call committed, so another connection can see its effects."""
    with psycopg.connect(app_conninfo(), autocommit=True) as connection:
        yield connection


def as_auth_role(sql: str, params: tuple = ()) -> list[tuple]:
    """Runs SQL as the auth role: the one thing that can see sessions, for assertions."""
    with psycopg.connect(owner_conninfo(), autocommit=True) as connection, connection.transaction():
        connection.execute("SET LOCAL ROLE inspection_auth")
        cursor = connection.execute(sql, params)
        return cursor.fetchall() if cursor.description else []


def create_session(connection: psycopg.Connection, token: str, seconds: int = 3600) -> UUID:
    return connection.execute(
        "SELECT auth.create_session(%s, %s, %s)", (TENANT_A.user_id, token_hash(token), seconds)
    ).fetchone()[0]


def resolve(connection: psycopg.Connection, token: str, idle: int = 3600) -> tuple | None:
    return connection.execute(
        "SELECT session_id, user_id FROM auth.resolve_session(%s, %s)", (token_hash(token), idle)
    ).fetchone()


def test_verify_login_returns_the_hash_for_a_known_email(app_conn):
    row = app_conn.execute("SELECT * FROM auth.verify_login(%s)", (ALPHA_EMAIL,)).fetchone()
    assert row == (TENANT_A.user_id, HASH)


def test_verify_login_ignores_email_case(app_conn):
    rows = app_conn.execute("SELECT * FROM auth.verify_login(%s)", (ALPHA_EMAIL.upper(),))
    assert rows.fetchall() == [(TENANT_A.user_id, HASH)]


def test_verify_login_finds_nothing_for_an_unknown_email(app_conn):
    assert (
        app_conn.execute("SELECT * FROM auth.verify_login('nobody@test.example')").fetchall() == []
    )


def test_verify_login_finds_nothing_for_a_user_without_credentials(app_conn):
    email = f"user-{TENANT_B.user_id}@test.example"
    assert app_conn.execute("SELECT * FROM auth.verify_login(%s)", (email,)).fetchall() == []


def test_functions_work_with_no_tenant_context(app_conn):
    # A fresh connection, no set_config: this is the state at login.
    assert app_conn.execute("SELECT current_setting('app.org_id', true)").fetchone()[0] in (
        None,
        "",
    )
    assert app_conn.execute("SELECT * FROM auth.verify_login(%s)", (ALPHA_EMAIL,)).fetchall()


def test_a_session_resolves_until_it_is_revoked(autocommit_app):
    token = uuid4().hex
    session_id = create_session(autocommit_app, token)

    assert resolve(autocommit_app, token) == (session_id, TENANT_A.user_id)

    autocommit_app.execute("SELECT auth.revoke_session(%s)", (token_hash(token),))
    assert resolve(autocommit_app, token) is None


def test_an_unknown_token_resolves_to_nothing(autocommit_app):
    assert resolve(autocommit_app, uuid4().hex) is None


def test_only_the_hash_of_a_token_is_stored(autocommit_app):
    token = uuid4().hex
    create_session(autocommit_app, token)

    rows = as_auth_role(
        "SELECT token_sha256 FROM sessions WHERE token_sha256 = %s", (token_hash(token),)
    )
    assert rows == [(token_hash(token),)]
    stored_text = as_auth_role(
        "SELECT sessions::text FROM sessions WHERE token_sha256 = %s", (token_hash(token),)
    )
    assert token not in stored_text[0][0]


def test_an_idle_session_stops_resolving(autocommit_app):
    token = uuid4().hex
    create_session(autocommit_app, token)
    as_auth_role(
        "UPDATE sessions SET last_seen_at = now() - interval '2 hours' WHERE token_sha256 = %s",
        (token_hash(token),),
    )

    assert resolve(autocommit_app, token, idle=3600) is None
    assert resolve(autocommit_app, token, idle=3 * 3600) is not None


def test_a_session_past_its_absolute_limit_stops_resolving(autocommit_app):
    token = uuid4().hex
    create_session(autocommit_app, token)
    as_auth_role(
        "UPDATE sessions SET expires_at = now() - interval '1 second' WHERE token_sha256 = %s",
        (token_hash(token),),
    )

    assert resolve(autocommit_app, token) is None


def test_the_absolute_lifetime_is_capped_at_seven_days(autocommit_app):
    token = uuid4().hex
    create_session(autocommit_app, token, seconds=10**9)

    (within_cap,) = as_auth_role(
        "SELECT expires_at <= now() + interval '7 days 1 minute' FROM sessions"
        " WHERE token_sha256 = %s",
        (token_hash(token),),
    )[0]
    assert within_cap


def test_the_idle_window_is_capped_at_twelve_hours(autocommit_app):
    token = uuid4().hex
    create_session(autocommit_app, token)
    as_auth_role(
        "UPDATE sessions SET last_seen_at = now() - interval '13 hours' WHERE token_sha256 = %s",
        (token_hash(token),),
    )

    assert resolve(autocommit_app, token, idle=10**9) is None


def test_last_seen_moves_at_most_once_a_minute(autocommit_app):
    token = uuid4().hex
    create_session(autocommit_app, token)
    seen = "SELECT last_seen_at FROM sessions WHERE token_sha256 = %s"
    first = as_auth_role(seen, (token_hash(token),))[0][0]

    resolve(autocommit_app, token)
    assert as_auth_role(seen, (token_hash(token),))[0][0] == first

    as_auth_role(
        "UPDATE sessions SET last_seen_at = now() - interval '5 minutes' WHERE token_sha256 = %s",
        (token_hash(token),),
    )
    resolve(autocommit_app, token)
    refreshed = as_auth_role(
        "SELECT last_seen_at > now() - interval '1 minute' FROM sessions WHERE token_sha256 = %s",
        (token_hash(token),),
    )
    assert refreshed == [(True,)]


def test_a_session_cannot_be_created_for_a_missing_user(autocommit_app):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        autocommit_app.execute(
            "SELECT auth.create_session(%s, %s, 60)", (uuid4(), token_hash(uuid4().hex))
        )


def test_the_owner_cannot_call_the_functions(owner_conn):
    with pytest.raises(
        psycopg.errors.InsufficientPrivilege, match="permission denied for function"
    ):
        owner_conn.execute("SELECT auth.create_session(%s, %s, 60)", (TENANT_A.user_id, b"x" * 32))
