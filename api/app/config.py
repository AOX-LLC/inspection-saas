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

    db_worker_user: str = "inspection_worker"
    db_worker_password_file: Path = Path("/run/inspection-secrets/worker/worker_password")

    db_owner_user: str = "inspection_owner"
    db_owner_password_file: Path = Path("/run/inspection-secrets/owner/owner_password")

    # Sessions: the idle timer resets on use, the absolute limit never does.
    session_idle_seconds: int = 12 * 60 * 60
    session_absolute_seconds: int = 7 * 24 * 60 * 60
    # None means Secure outside demo and test, where plain HTTP on localhost is normal.
    cookie_secure: bool | None = None
    # Browser origins allowed to make state-changing requests. Comma-separated.
    allowed_origins: str = "http://127.0.0.1:4700,http://localhost:4700"

    # Object store. Operations use the internal endpoint; presigned URLs are
    # signed for the public one, which is what the browser can reach.
    s3_endpoint: str = "http://objectstore:3900"
    s3_public_endpoint: str = "http://127.0.0.1:4703"
    s3_region: str = "garage"
    s3_bucket: str = "inspection-uploads"
    s3_access_key_id_file: Path = Path("/run/inspection-secrets/storage/s3_access_key_id")
    s3_secret_access_key_file: Path = Path("/run/inspection-secrets/storage/s3_secret_access_key")
    presign_ttl_seconds: int = 300
    max_upload_bytes: int = 50 * 1024 * 1024

    @property
    def origins(self) -> frozenset[str]:
        return frozenset(o.strip() for o in self.allowed_origins.split(",") if o.strip())

    @property
    def cookie_is_secure(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.app_env is AppEnv.PRODUCTION

    def app_database_url(self) -> URL:
        return self._database_url(self.db_app_user, self.db_app_password_file)

    def worker_database_url(self) -> URL:
        return self._database_url(self.db_worker_user, self.db_worker_password_file)

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
