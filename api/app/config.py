"""Runtime settings, read from the environment.

Database passwords are never passed as values. Each role's password is read
from a file that the `secrets` service writes into a shared volume.
"""

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
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
    # Proxies whose X-Forwarded-For is believed: IP addresses, CIDR ranges or host names,
    # comma-separated. Empty trusts none, and every request is rate-limited by its socket
    # address. In Compose this is the web service, which forwards its clients' addresses.
    trusted_proxies: str = ""
    # The web app's own origin(s), comma-separated: the only browser origins the API accepts
    # state-changing requests from, and the only ones the object store's CORS allows.
    allowed_origins: str = "http://127.0.0.1:4700"

    # Object store. Operations use the internal endpoint; presigned URLs are
    # signed for the public one, which is what the browser can reach.
    s3_endpoint: str = "http://objectstore:3900"
    s3_public_endpoint: str = "http://127.0.0.1:4703"
    s3_region: str = "garage"
    s3_bucket: str = "inspection-uploads"
    s3_access_key_id_file: Path = Path("/run/inspection-secrets/storage/s3_access_key_id")
    s3_secret_access_key_file: Path = Path("/run/inspection-secrets/storage/s3_secret_access_key")
    presign_ttl_seconds: int = 300
    # Thumbnails are many and small, and a grid stays open, so they live longer than an upload.
    thumbnail_ttl_seconds: int = Field(default=900, ge=60, le=900)
    max_upload_bytes: int = 50 * 1024 * 1024

    # Tiling. A tile is cut at the detector's input size, so a tile of
    # `tile_size` pixels needs no resizing before inference. Neighbouring tiles
    # overlap so a defect cut by one tile's edge is whole in the next.
    tile_size: int = Field(default=640, ge=64, le=4096)
    tile_overlap: int = Field(default=128, ge=0)
    tile_jpeg_quality: int = Field(default=85, ge=1, le=100)
    # The longest side of the one small preview the grid shows, so it never loads a photo.
    thumbnail_size: int = Field(default=320, ge=32, le=1024)
    # The largest decoded image the worker will open, in pixels, and the most
    # tiles it will cut from one photo. Together they bound memory and time for
    # a hostile or pathological file.
    max_image_pixels: int = Field(default=50_000_000, ge=1)
    max_tiles_per_photo: int = Field(default=512, ge=1)

    # The worker. One job at a time by default: decoding a large photo is the
    # memory-heavy step, and the container has a memory limit.
    worker_concurrency: int = Field(default=1, ge=1, le=8)
    # How long a claimed job stays locked before another worker may take it.
    job_lock_seconds: int = Field(default=300, ge=10, le=3600)
    # The wait before a failed job's second attempt; it doubles each attempt.
    job_backoff_seconds: int = Field(default=5, ge=1)
    # The fallback when no NOTIFY arrives.
    worker_poll_seconds: float = Field(default=5.0, gt=0)
    worker_heartbeat_file: Path = Path("/tmp/worker-alive")  # noqa: S108 (a tmpfs in the container)
    cleanup_interval_seconds: int = Field(default=300, ge=1)
    # An upload started this long ago and never completed is abandoned.
    abandoned_upload_seconds: int = Field(default=3600, ge=900)
    session_purge_grace_seconds: int = Field(default=24 * 60 * 60, ge=60)

    @model_validator(mode="after")
    def _overlap_leaves_a_stride(self) -> "Settings":
        if self.tile_overlap >= self.tile_size:
            raise ValueError("TILE_OVERLAP must be smaller than TILE_SIZE")
        return self

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
