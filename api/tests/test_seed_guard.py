"""The seed refuses to run outside demo, and its data is internally consistent."""

from collections.abc import Iterator

import pytest

from app.config import get_settings
from app.seed import __main__ as seed_main
from app.seed.data import MEMBERSHIPS, ORGS, PROJECTS, USERS


@pytest.fixture(params=["test", "production", None], ids=["test", "production", "unset"])
def non_demo_env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Unset matters most: a missing APP_ENV must not count as demo.
    if request.param is None:
        monkeypatch.delenv("APP_ENV", raising=False)
    else:
        monkeypatch.setenv("APP_ENV", request.param)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_seed_refuses_outside_demo(non_demo_env: None, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        seed_main.main()

    assert exit_info.value.code == 2
    assert "refusing to run unless APP_ENV=demo" in capsys.readouterr().err


def test_seed_data_is_consistent() -> None:
    org_ids = {org.id for org in ORGS}
    user_ids = {user.id for user in USERS}

    assert all(m.org_id in org_ids and m.user_id in user_ids for m in MEMBERSHIPS)
    assert all(p.org_id in org_ids for p in PROJECTS)

    all_ids = [*org_ids, *user_ids, *(p.id for p in PROJECTS)]
    assert len(all_ids) == len(set(all_ids))
    assert len({(m.org_id, m.user_id) for m in MEMBERSHIPS}) == len(MEMBERSHIPS)
    assert len({user.email for user in USERS}) == len(USERS)
    assert all(user.email.endswith(".example") for user in USERS)
    assert all(sum(p.org_id == org.id for p in PROJECTS) == 2 for org in ORGS)
