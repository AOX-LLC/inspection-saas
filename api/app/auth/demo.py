"""The seeded demo accounts, listed for the login page in demo mode only.

The route exists only when `APP_ENV=demo`: in any other mode it is not
registered, so it is a plain 404 and not a response that says "disabled".
The accounts and the shared password are the synthetic ones the seed creates
and the README publishes; the list is built from the seed's own data and
never reads the database.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.seed.data import DEMO_PASSWORD, MEMBERSHIPS, ORGS, USERS

router = APIRouter(prefix="/auth", tags=["auth"])

_ORG_NAMES = {org.id: org.name for org in ORGS}


class DemoAccount(BaseModel):
    email: str
    display_name: str
    # Who they are in the demo, e.g. "owner of Demo Org Alpha".
    description: str


class DemoAccounts(BaseModel):
    accounts: list[DemoAccount]
    # One password for every demo account; it protects nothing.
    password: str


def _describe(user_id) -> str:
    return ", ".join(
        f"{m.role} of {_ORG_NAMES[m.org_id]}" for m in MEMBERSHIPS if m.user_id == user_id
    )


@router.get("/demo-accounts")
async def demo_accounts() -> DemoAccounts:
    return DemoAccounts(
        accounts=[
            DemoAccount(email=u.email, display_name=u.display_name, description=_describe(u.id))
            for u in USERS
        ],
        password=DEMO_PASSWORD,
    )
