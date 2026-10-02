"""The login page may list the seeded demo accounts in demo mode, and never otherwise."""

import httpx
import pytest
from fastapi import FastAPI

import app.main as main
from app.config import AppEnv, get_settings
from app.seed.data import DEMO_PASSWORD, USERS

pytestmark = pytest.mark.asyncio


def app_in(monkeypatch: pytest.MonkeyPatch, env: AppEnv | None) -> FastAPI:
    base = get_settings()
    settings = base if env is None else base.model_copy(update={"app_env": env})
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    return main.create_app()


async def get_accounts(application: FastAPI) -> httpx.Response:
    # No lifespan: the route reads no database and needs no object store.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        return await client.get("/auth/demo-accounts")


async def test_demo_mode_lists_the_seeded_accounts(monkeypatch):
    response = await get_accounts(app_in(monkeypatch, AppEnv.DEMO))

    assert response.status_code == 200
    body = response.json()
    assert [a["email"] for a in body["accounts"]] == [u.email for u in USERS]
    assert body["password"] == DEMO_PASSWORD
    owner = next(a for a in body["accounts"] if a["email"].startswith("alpha.owner"))
    assert owner["description"] == "owner of Demo Org Alpha"
    consultant = next(a for a in body["accounts"] if a["email"].startswith("consultant"))
    assert consultant["description"] == "inspector of Demo Org Alpha, inspector of Demo Org Beta"


@pytest.mark.parametrize("env", [AppEnv.PRODUCTION, AppEnv.TEST])
async def test_any_other_mode_has_no_such_route(monkeypatch, env):
    response = await get_accounts(app_in(monkeypatch, env))

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


async def test_an_unset_mode_counts_as_production(monkeypatch):
    # The setting's default is production, so a missing APP_ENV never exposes the list.
    monkeypatch.delenv("APP_ENV", raising=False)
    get_settings.cache_clear()
    try:
        response = await get_accounts(main.create_app())
    finally:
        get_settings.cache_clear()

    assert response.status_code == 404
