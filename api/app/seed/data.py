"""Synthetic demo data.

Fixed ids keep the seed idempotent and give later tests and docs stable
references. All names and emails are synthetic.
"""

from dataclasses import dataclass
from uuid import UUID

ALPHA = UUID("8e35f604-8a87-4fba-b70e-e87a8e22efd1")
BETA = UUID("d16b9455-89e1-4a14-8535-6f581ac38d7b")

ALPHA_OWNER = UUID("f792de8a-588a-4c65-985a-319946b41f70")
ALPHA_INSPECTOR = UUID("75b20b44-1c28-4db7-a6bd-a4544c5dbd59")
ALPHA_VIEWER = UUID("cb088c71-e791-4fe0-953a-679fa594a761")
BETA_OWNER = UUID("432e92b6-15bb-4654-b34c-0ab756553567")
SHARED_CONSULTANT = UUID("b85d1d87-d365-410b-9a59-082ce685c460")


@dataclass(frozen=True)
class SeedOrg:
    id: UUID
    name: str


@dataclass(frozen=True)
class SeedUser:
    id: UUID
    email: str
    display_name: str


@dataclass(frozen=True)
class SeedMembership:
    org_id: UUID
    user_id: UUID
    role: str


@dataclass(frozen=True)
class SeedProject:
    id: UUID
    org_id: UUID
    name: str


ORGS = (
    SeedOrg(ALPHA, "Demo Org Alpha"),
    SeedOrg(BETA, "Demo Org Beta"),
)

USERS = (
    SeedUser(ALPHA_OWNER, "alpha.owner@alpha.example", "Alpha Owner"),
    SeedUser(ALPHA_INSPECTOR, "alpha.inspector@alpha.example", "Alpha Inspector"),
    SeedUser(ALPHA_VIEWER, "alpha.viewer@alpha.example", "Alpha Viewer"),
    SeedUser(BETA_OWNER, "beta.owner@beta.example", "Beta Owner"),
    SeedUser(SHARED_CONSULTANT, "consultant@shared.example", "Shared Consultant"),
)

MEMBERSHIPS = (
    SeedMembership(ALPHA, ALPHA_OWNER, "owner"),
    SeedMembership(ALPHA, ALPHA_INSPECTOR, "inspector"),
    SeedMembership(ALPHA, ALPHA_VIEWER, "viewer"),
    SeedMembership(BETA, BETA_OWNER, "owner"),
    SeedMembership(ALPHA, SHARED_CONSULTANT, "inspector"),
    SeedMembership(BETA, SHARED_CONSULTANT, "inspector"),
)

PROJECTS = (
    SeedProject(UUID("022a4671-687f-475b-a4b4-21ea059836ab"), ALPHA, "Synthetic Car Park Level 2"),
    SeedProject(UUID("3422f8b6-cf2a-44de-b3cc-ac2a9fbe8a39"), ALPHA, "Synthetic Bridge Pier 4"),
    SeedProject(
        UUID("b70e9fc6-b294-4ea6-abce-a3ba75ff66e6"), BETA, "Synthetic Retaining Wall East"
    ),
    SeedProject(UUID("648e0f72-478a-40ef-aeef-3112555e4643"), BETA, "Synthetic Water Tank Roof"),
)
