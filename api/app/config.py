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

    # Personnel changes take effect on their effective date, and something has to
    # notice. The supported way is the command
    # (`python -m app.jobs.apply_personnel_changes`) from cron or a systemd timer.
    # This flag adds an in-process runner for a deployment that has no scheduler
    # yet — a thin loop over the same function, and **off by default**, because a
    # scheduler nobody can turn off is worse than a command somebody runs.
    personnel_apply_runner_enabled: bool = False
    personnel_apply_interval_seconds: int = 900

    # --- documents (ticket 31) ----------------------------------------------
    # Where an uploaded original is kept, inside the API container. A volume is
    # mounted here by compose; a directory that does not exist yet is created on the
    # first upload rather than at startup, so a deployment that never receives a
    # document does not need one.
    #
    # Not a `res://`-style path and deliberately not part of what a client sees: the
    # stored path is a *key* relative to this root, so moving the root between
    # environments does not change a single row.
    document_storage_path: str = "/data/documents"
    # The per-file ceiling, refused rather than truncated. 50 MB is what the ticket
    # names, and a setting rather than a literal so an installation that stores
    # scanned manuals can raise it without a code change.
    document_max_upload_bytes: int = 50 * 1024 * 1024
    # The in-process parsing loop, **on only in development**, and off in production
    # for the reason `personnel_apply_runner_enabled` is off everywhere: a loop that
    # lives inside the API dies whenever the API is redeployed, and a scheduler nobody
    # can turn off is worse than none. Production runs the command
    # (`python -m app.jobs.parse_documents`) from cron or the worker container.
    #
    # Development is the case that needs it: `docker compose up` has no cron and no
    # worker, so without this an upload sits in `processing` for ever and the screen
    # honestly says so — which reads as a broken pipeline rather than as an
    # unconfigured one. Deriving it from `app_env` rather than defaulting it on means
    # the behaviour is a property of the environment and not something a deployment has
    # to remember to turn off.
    document_parse_runner_enabled: bool | None = None
    document_parse_interval_seconds: int = 2

    # --- embeddings (ticket 32) ---------------------------------------------
    # Which adapter produces the vectors. Three values, and the default is *derived*
    # from the environment for the reason the parsing loop above is:
    #
    #   development, test → `fake`   reproducible bag-of-words vectors, no key, no
    #                                network. `docker compose up` must work without an
    #                                OpenAI account, and a test suite must be able to
    #                                assert that a re-embed produced the *same*
    #                                vectors.
    #   anything else      → `openai` the real implementation, so a deployment that
    #                                forgot the key gets a document that failed to
    #                                embed with `ERR_DOC_009` naming the key — rather
    #                                than a corpus of fake vectors that looks indexed
    #                                and answers every question badly.
    #
    # `none` is a supported setting and not a degenerate one: chunks are written, the
    # full-text index works, and `WHERE embedding IS NULL` is the worklist for the
    # re-embed. The fake is never selected silently outside development, which is the
    # property that makes the other two honest.
    embedding_provider: str | None = None
    # Where the real adapter posts. A setting because an installation may route
    # through a gateway or a regional endpoint; the model and the dimension are
    # deliberately *not* settings — vectors from two models are not comparable, so a
    # model change is a re-embedding migration rather than an environment variable.
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"

    @property
    def parses_documents_in_process(self) -> bool:
        """Whether this process runs the parsing loop. Development, unless overridden."""
        if self.document_parse_runner_enabled is not None:
            return self.document_parse_runner_enabled
        return self.is_development

    @property
    def embeddings_provider(self) -> str:
        """Which embedding adapter this deployment uses. See `embedding_provider`."""
        if self.embedding_provider is not None:
            return self.embedding_provider
        return "fake" if self.is_development or self.app_env == "test" else "openai"

    # --- retrieval (ticket 33) ----------------------------------------------
    # The reciprocal-rank fusion constant. 60 is the literature's value (Cormack et
    # al., 2009) and the default; it damps the top ranks so that a candidate both legs
    # put in their top five beats one leg's single first place. Lowering it (10 is the
    # usual alternative) trusts each leg's own top hits more, which is the right choice
    # for a corpus where one leg is known to be much the stronger — a decision that has
    # to be measured, which is what the evaluation script is for.
    retrieval_fusion_k: int = 60
    # How many candidates each leg contributes before the fusion. §5.2 says twenty for
    # each half: it is a *recall* budget rather than a result size, because a document
    # the fusion never saw cannot be reranked into the five that are returned.
    retrieval_leg_limit: int = 20
    # Which reranker adapter to build. `None` (and `lexical`) is the one this repository
    # ships; a deployment with a cross-encoder endpoint names its adapter here, which is
    # what makes `domain/retrieval/rerank.py` a seam rather than a class.
    retrieval_reranker: str | None = None
    # The threshold below which the answer is "the knowledge base holds no basis for
    # this" (§5.2/D20) rather than a weak top five. It is on the *reranked* score's
    # `[0, 1]` scale — see `domain/retrieval/service.py` for why that score and not the
    # fusion's. A setting because the number a corpus needs is a measurement: run
    # `tests/tools/eval_retrieval.py` and move it deliberately rather than inheriting it.
    retrieval_min_score: float = 0.35

    # --- leave (ticket 25) --------------------------------------------------
    # The annual allowance, in natural days, and the whole of `docs/DESIGN.md`'s
    # D7: "30 自然日 ... 额度可配置 (`annual_leave_days=30`)". A setting rather than
    # a table because it is one company-wide policy number: `ANNUAL_LEAVE_DAYS=25`
    # in the environment changes it with no code and no migration, while a table
    # would need an endpoint, a permission, an audit trail and a UI before the
    # first installation could change a number the design already names. What a
    # *person* is granted is `leave_balances.entitled_days` — materialised from
    # this figure when their year's row is first needed, and adjustable per person
    # by HR — so the parameter is the default, not the only source.
    annual_leave_days: int = 30

    # --- overtime (ticket 26) -----------------------------------------------
    # How far the approved minutes and the day's actual worked minutes may differ
    # before the record is marked for HR instead of being settled quietly. Half an
    # hour, because below it the gap is the minute somebody spent walking to the lift
    # and the rounding of a punch, and at or above it somebody worked materially more
    # or less than was agreed — which is a conversation rather than a number the
    # system may pick silently. A setting rather than a constant for the same reason
    # `annual_leave_days` is one: it is a company's tolerance, and
    # `OVERTIME_CONFIRMATION_THRESHOLD_MINUTES=15` changes it with no code change.
    overtime_confirmation_threshold_minutes: int = 30

    # The browser talks to the API directly, so origins must be host-facing.
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # --- mail (ticket 20) ---------------------------------------------------
    # **Off by default**, like the personnel runner above and for a blunter
    # reason: an unconfigured sender that tries anyway bounces a company's worth
    # of mail off a mail server that never agreed to take it. Compose turns it on
    # and points it at Mailpit, which accepts everything and delivers nothing.
    mail_enabled: bool = False
    # `mailpit` is the sibling container's name on the compose network, the same
    # convention `postgres` and `redis` follow above.
    smtp_host: str = "mailpit"
    smtp_port: int = 1025
    # Both optional: Mailpit wants neither, a real relay usually wants both. A
    # username without a password (or the reverse) is a half-configured relay and
    # is refused at send time rather than guessed at.
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "no-reply@empresa.es"
    # Plain SMTP inside the compose network; a real relay terminates TLS itself,
    # and STARTTLS is what a submission port needs.
    smtp_starttls: bool = False
    smtp_timeout_seconds: int = 10

    # Where the link in a digest points. The host is a setting because the mail
    # is read outside the compose network, where `web` does not resolve.
    web_base_url: str = "http://localhost:3000"
    # How many times one day's digest for one recipient is attempted before it is
    # left `failed` with its reason. Three, because a transient refusal is worth
    # two more tries and a permanent one is worth nobody's morning.
    digest_max_attempts: int = 3
    # The language a digest is written in when the recipient has stored no
    # preference (DESIGN §10.4: the choice lives on the account, and the great
    # majority of accounts have never been asked).
    digest_default_language: str = "es"

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
