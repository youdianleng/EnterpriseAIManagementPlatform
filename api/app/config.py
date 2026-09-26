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

    # What the application connects as. A different role from the one that owns
    # the tables, because that is what makes the database's own defences real:
    # the owner may rewrite the audit trail, and Postgres exempts a table's owner
    # from its row-level policies. Migrations and fixtures use `database_url`;
    # requests use this. Left unset, both are the same connection — which is how
    # a developer runs it without the extra role, and why `enforces_database_
    # security` exists to say which mode is in force.
    app_database_url: str | None = None

    # Integration tests run against this database on the same server. Keeping it
    # separate means a test run can never truncate development data.
    test_database_name: str = "eam_test"

    # Two different lifetimes, because they answer two different questions. The
    # audit trail is evidence: Spanish labour law requires four years of working
    # time records, and an audit record that expires sooner than the data it
    # describes cannot explain that data. Runtime logs are diagnostics, they live
    # on stdout rather than in this database at all, and two weeks is enough to
    # investigate an incident.
    audit_retention_days: int = 1460
    log_retention_days: int = 14

    # Per-worker connection ceilings; 100 staff with a handful of AI requests
    # never needs more, and a low ceiling surfaces leaks instead of hiding them.
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_recycle_seconds: int = 1800

    # The browser talks to the API directly, so origins must be host-facing.
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"

    @property
    def test_database_url(self) -> str:
        """The configured database URL pointed at the test database."""
        base, _, _ = self.database_url.rpartition("/")
        return f"{base}/{self.test_database_name}"

    @property
    def runtime_database_url(self) -> str:
        """The connection the application serves requests with."""
        return self.app_database_url or self.database_url

    @property
    def runtime_test_database_url(self) -> str:
        """The runtime connection pointed at the test database.

        The same swap as `test_database_url`, applied to the application's role:
        a test run has to exercise the role production uses, or the row-level
        policies it relies on are never executed.
        """
        base, _, _ = self.runtime_database_url.rpartition("/")
        return f"{base}/{self.test_database_name}"

    @property
    def enforces_database_security(self) -> bool:
        """True when requests connect as a role other than the table owner."""
        return self.runtime_database_url != self.database_url

    @property
    def admin_database_url(self) -> str:
        """Same server, `postgres` database: used to create the test database."""
        base, _, _ = self.database_url.rpartition("/")
        return f"{base}/postgres"

    @property
    def sync_database_url(self) -> str:
        return to_libpq_dsn(self.database_url)

    @property
    def sync_test_database_url(self) -> str:
        return to_libpq_dsn(self.test_database_url)

    @property
    def sync_admin_database_url(self) -> str:
        return to_libpq_dsn(self.admin_database_url)


def to_libpq_dsn(sqlalchemy_url: str) -> str:
    """Strip the SQLAlchemy driver suffix so libpq (psycopg direct, Alembic) accepts it.

    Readiness probes and the test-database bootstrap use a raw psycopg
    connection, which rejects the `+psycopg` part of the URL.
    """
    prefix = "postgresql+psycopg://"
    if sqlalchemy_url.startswith(prefix):
        return "postgresql://" + sqlalchemy_url[len(prefix) :]
    return sqlalchemy_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
