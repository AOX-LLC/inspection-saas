"""Runtime settings, read from the environment.

Database passwords are never passed as values. Each role's password is read
from a file that the `secrets` service writes into a shared volume.
"""

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class AppEnv(StrEnum):
    DEMO = "demo"
    TEST = "test"
    PRODUCTION = "production"


class ModelMode(StrEnum):
    MOCK = "mock"
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(frozen=True)

    # Fails closed: demo-only behaviour (seeding, API docs) needs an explicit opt-in.
    app_env: AppEnv = AppEnv.PRODUCTION
    model_mode: ModelMode = ModelMode.MOCK

    db_host: str = "db"
    db_port: int = 5432
    db_name: str = "inspection"

    db_app_user: str = "inspection_app"
    db_app_password_file: Path = Path("/run/inspection-secrets/app/app_password")

    db_owner_user: str = "inspection_owner"
    db_owner_password_file: Path = Path("/run/inspection-secrets/owner/owner_password")

    def app_database_url(self) -> URL:
        return self._database_url(self.db_app_user, self.db_app_password_file)

    def owner_database_url(self) -> URL:
        return self._database_url(self.db_owner_user, self.db_owner_password_file)

    def _database_url(self, user: str, password_file: Path) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=user,
            password=password_file.read_text().strip(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
