"""Runtime configuration.

Every setting has a working default so the container starts with no .env file.
Host names for sibling containers are injected by docker-compose, not read here.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"

    # Named `postgres`/`redis` inside the compose network.
    database_url: str = "postgresql+psycopg://eam:eam_dev_password@postgres:5432/eam"
    redis_url: str = "redis://redis:6379/0"

    # The browser talks to the API directly, so origins must be host-facing.
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
