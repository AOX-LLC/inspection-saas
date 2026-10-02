"""Walks every route and tries to reach another org's data through it.

The routes are discovered from the app, so a new route is covered the moment it
exists. A route that needs a request body must have a sample here, or the walk
fails and says so: nobody can add a write route that escapes this test.
"""

from uuid import uuid4

import pytest
from fastapi.routing import APIRoute
from starlette.routing import Route

from app.main import create_app
from tests.api.conftest import World

pytestmark = pytest.mark.asyncio


DOCUMENTATION_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect"}


def api_routes(routes, prefix: str = ""):
    """Every APIRoute with its full path, looking inside included routers.

    Anything that is neither a route nor an included router (a mounted app, say)
    could hide routes from this walk, so it fails loudly instead of being skipped.
    """
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from api_routes(included.routes, prefix + route.include_context.prefix)
        elif isinstance(route, APIRoute):
            yield prefix + route.path, route
        elif isinstance(route, Route) and route.path in DOCUMENTATION_PATHS:
            continue  # the schema and its viewer, served outside production only
        else:
            raise AssertionError(f"the route walker cannot see inside {type(route).__name__}")


_APP = create_app()
ROUTES = [
    (method, path) for path, route in api_routes(_APP.routes) for method in sorted(route.methods)
]
ORG_ROUTES = [(method, path) for method, path in ROUTES if "{org_id}" in path]
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}

# Routes that work without a session. Everything else must answer 401.
PUBLIC_ROUTES = {
    ("GET", "/health"),
    ("POST", "/auth/login"),
    ("POST", "/auth/logout"),
}

# A valid body for each org-scoped write route, so a 404 means "not yours", not "malformed".
# None says the route takes no body. A write route missing from here fails the walk.
SAMPLE_BODIES = {
    ("POST", "/orgs/{org_id}/projects/{project_id}/files/{file_id}/complete"): None,
    ("POST", "/orgs/{org_id}/projects/{project_id}/files"): {
        "filename": "walker.jpg",
        "content_type": "image/jpeg",
        "size_bytes": 1000,
    },
}

# Signup and invite create accounts, and the unique email constraint ignores
# row-level security, so an "email already taken" answer would reveal accounts
# in other orgs. Any such route must return one generic answer and be tested for
# it; until then, adding one fails this suite.
ACCOUNT_CREATION_WORDS = ("signup", "sign-up", "register", "invite")


def fill(path: str, *, org, project, file) -> str:
    return path.format(org_id=org, project_id=project, file_id=file)


def body_for(method: str, path: str) -> dict | None:
    if method not in UNSAFE:
        return None
    if (method, path) not in SAMPLE_BODIES:
        pytest.fail(f"{method} {path} needs a SAMPLE_BODIES entry in {__file__}")
    return SAMPLE_BODIES[(method, path)]


async def test_the_walker_found_the_org_routes():
    # Guards against the walk passing because it walked nothing.
    assert len(ORG_ROUTES) >= 5
    assert any(method == "POST" for method, _ in ORG_ROUTES)


async def test_the_walker_sees_every_documented_operation():
    """Cross-check against the OpenAPI document, which is built a different way."""
    documented = {
        (method.upper(), path)
        for path, operations in _APP.openapi()["paths"].items()
        for method in operations
    }
    assert documented <= set(ROUTES)


async def test_no_account_creation_route_exists_yet():
    offenders = [
        path for _, path in ROUTES if any(w in path.lower() for w in ACCOUNT_CREATION_WORDS)
    ]
    assert offenders == [], "add a generic-error test before adding account creation routes"


@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_every_route_needs_a_session(anonymous, world: World, method, path):
    if (method, path) in PUBLIC_ROUTES:
        pytest.skip("public by design")
    url = fill(path, org=world.alpha.id, project=world.alpha.project_id, file=world.alpha.file_id)

    response = await anonymous.request(method, url, json=body_for(method, path))

    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path"), ORG_ROUTES)
async def test_another_orgs_ids_under_its_own_path_are_not_found(
    signed_in, world: World, method, path
):
    client = await signed_in(world.alpha.inspector)
    url = fill(path, org=world.beta.id, project=world.beta.project_id, file=world.beta.file_id)

    response = await client.request(method, url, json=body_for(method, path))

    assert response.status_code == 404


@pytest.mark.parametrize(("method", "path"), ORG_ROUTES)
async def test_another_orgs_ids_under_my_path_are_not_found(signed_in, world: World, method, path):
    client = await signed_in(world.alpha.inspector)
    url = fill(path, org=world.alpha.id, project=world.beta.project_id, file=world.beta.file_id)

    response = await client.request(method, url, json=body_for(method, path))

    if "{project_id}" in path:
        assert response.status_code == 404
    else:
        # The route lists my own org's data; none of Beta's may appear in it.
        assert response.status_code == 200
        assert str(world.beta.project_id) not in response.text


@pytest.mark.parametrize(("method", "path"), ORG_ROUTES)
async def test_another_orgs_file_under_my_project_is_not_found(
    signed_in, world: World, method, path
):
    client = await signed_in(world.alpha.inspector)
    url = fill(path, org=world.alpha.id, project=world.alpha.project_id, file=world.beta.file_id)

    response = await client.request(method, url, json=body_for(method, path))

    # Routes with no file id in the path read my own project and succeed; the rest must 404.
    if "{file_id}" in path:
        assert response.status_code == 404
    else:
        assert response.status_code != 404 or method in UNSAFE


@pytest.mark.parametrize(("method", "path"), ORG_ROUTES)
async def test_an_unknown_org_is_not_found(signed_in, world: World, method, path):
    client = await signed_in(world.alpha.inspector)
    url = fill(path, org=uuid4(), project=world.alpha.project_id, file=world.alpha.file_id)

    response = await client.request(method, url, json=body_for(method, path))

    assert response.status_code == 404


@pytest.mark.parametrize(("method", "path"), ORG_ROUTES)
async def test_a_member_of_both_orgs_is_still_walled_off_per_org(
    signed_in, world: World, method, path
):
    """The consultant belongs to both orgs, so Beta's file under Alpha's path must still 404."""
    client = await signed_in(world.consultant)
    url = fill(path, org=world.alpha.id, project=world.alpha.project_id, file=world.beta.file_id)

    response = await client.request(method, url, json=body_for(method, path))

    if "{file_id}" in path:
        assert response.status_code == 404
