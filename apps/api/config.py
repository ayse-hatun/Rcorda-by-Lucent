import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlsplit

from dotenv import load_dotenv


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    app_env: str
    api_host: str
    api_port: int
    cors_allowed_origins: Tuple[str, ...]
    database_url: Optional[str]
    storage_backend: str = "filesystem"
    storage_dir: Path = REPOSITORY_ROOT / "data" / "storage"
    s3_endpoint_url: Optional[str] = None
    s3_bucket: Optional[str] = None
    s3_access_key: Optional[str] = None
    s3_secret_key: Optional[str] = None
    s3_region: str = "us-east-1"

    def require_postgres_url(self) -> str:
        if not self.database_url or not self.database_url.strip():
            raise ConfigurationError(
                "DATABASE_URL is required. Set it in the repository .env file "
                "or in the process environment."
            )

        parsed = urlsplit(self.database_url)
        if (
            parsed.scheme not in ("postgresql", "postgres")
            or not parsed.hostname
            or not parsed.path.strip("/")
        ):
            raise ConfigurationError(
                "DATABASE_URL must be a PostgreSQL URL with a host and database name."
            )
        return self.database_url


def load_settings() -> Settings:
    load_dotenv(REPOSITORY_ROOT / ".env")

    raw_port = os.getenv("API_PORT", "8000")
    try:
        api_port = int(raw_port)
    except ValueError as error:
        raise ConfigurationError("API_PORT must be an integer from 1 to 65535.") from error
    if not 1 <= api_port <= 65535:
        raise ConfigurationError("API_PORT must be an integer from 1 to 65535.")

    raw_origins = os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:3000")
    origins = tuple(origin.strip() for origin in raw_origins.split(",") if origin.strip())
    if not origins:
        raise ConfigurationError("CORS_ALLOWED_ORIGINS must contain at least one origin.")

    database_url = os.getenv("DATABASE_URL")
    if database_url is not None:
        database_url = database_url.strip() or None

    storage_backend = os.getenv("STORAGE_BACKEND", "").strip().lower()
    s3_bucket = os.getenv("S3_BUCKET", "").strip() or None
    s3_endpoint_url = os.getenv("S3_ENDPOINT_URL", "").strip() or None
    s3_access_key = os.getenv("S3_ACCESS_KEY", "").strip() or None
    s3_secret_key = os.getenv("S3_SECRET_KEY", "").strip() or None
    s3_region = os.getenv("S3_REGION", "us-east-1").strip() or "us-east-1"

    if not storage_backend:
        if s3_bucket:
            storage_backend = "s3"
        else:
            storage_backend = "filesystem"

    raw_storage_dir = os.getenv("STORAGE_DIR", "").strip()
    storage_dir = (
        Path(raw_storage_dir).resolve()
        if raw_storage_dir
        else (REPOSITORY_ROOT / "data" / "storage")
    )

    return Settings(
        app_env=os.getenv("APP_ENV", "development").strip() or "development",
        api_host=os.getenv("API_HOST", "127.0.0.1").strip() or "127.0.0.1",
        api_port=api_port,
        cors_allowed_origins=origins,
        database_url=database_url,
        storage_backend=storage_backend,
        storage_dir=storage_dir,
        s3_endpoint_url=s3_endpoint_url,
        s3_bucket=s3_bucket,
        s3_access_key=s3_access_key,
        s3_secret_key=s3_secret_key,
        s3_region=s3_region,
    )
