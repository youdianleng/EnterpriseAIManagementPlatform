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

    # --- answers (ticket 34) ------------------------------------------------
    # Which adapter generates an answer, derived from the environment exactly as
    # `embeddings_provider` is and for the same reason:
    #
    #   development, test → `fake`   a deterministic, offline adapter that answers
    #                                from the retrieved passages alone. `docker compose
    #                                up` must work with no OpenAI account, and a test
    #                                that asks a question must get a reproducible
    #                                answer with citations rather than a 503.
    #   anything else      → `openai` the real implementation, so a deployment that
    #                                forgot the key fails loudly with `ERR_ANS_001`
    #                                instead of answering from a fake.
    #
    # There is deliberately **no `none`**, unlike embeddings. A corpus with no vectors
    # still answers by full text (§5.3's degradation), but an answer with no model is
    # not an answer at all: D20 forbids falling back to the model's own knowledge, and
    # there is nothing else to fall back to. So the adapter is always built, and a
    # deployment that names a provider this repository does not have is refused where
    # the adapter is built rather than degraded into an ungrounded answer.
    chat_provider: str | None = None
    #: **The ordered chain** (§5.3's `CHAT_CHAIN`, ticket 42), as a comma-separated list:
    #: `CHAT_PROVIDERS=openai,deepseek`. **Unset means one provider** — the one
    #: `chat_provider_name` derives — which is deliberate: a chain nobody configured must not
    #: grow a second provider behind an operator's back, and a deployment that wants §5.3's
    #: full list says so. The first entry is the primary and the rest are tried in order, on
    #: a technical failure only (`domain/answer/chat.py` owns that rule).
    #:
    #: `fake` is accepted here and means what it means everywhere else: an adapter for
    #: development and tests, never selected silently outside them.
    chat_providers: str | None = None
    #: The generation model, and unlike the embedding model this one *is* a setting:
    #: §5.3's `CHAT_CHAIN` lists several interchangeable providers, so the model name is a
    #: deployment's choice rather than a schema decision — `rag_messages.model_used`
    #: records which one answered, which is what keeps that choice auditable. The
    #: embedding model is not a setting for the opposite reason: vectors from two models
    #: are not comparable and the rows would go stale.
    #:
    #: This is the model for the *default* provider. A chain entry's model can be named
    #: separately (`DEEPSEEK_CHAT_MODEL`), because the two providers do not share model names
    #: and one variable cannot honestly mean `gpt-4o` and `deepseek-chat` at once.
    chat_model: str = "gpt-4o"
    #: Seconds before a generation call is abandoned. Its expiry is the ticket's
    #: 「模型调用失败或超时」, and the answer stream closes with `ERR_ANS_001` rather than
    #: with a half-written answer presented as complete. **One budget for the whole chain**:
    #: see `chat.build_chat_chain`.
    chat_timeout_seconds: float = 60.0

    # --- the rest of the chain (ticket 42) ----------------------------------
    # One key and one model per provider, so a deployment can run OpenAI primary with
    # DeepSeek as its fallback without either variable meaning two things. Names follow the
    # providers `domain/answer/chat.py::PROVIDERS` catalogs; a provider with no key is
    # *present and failing* rather than absent, which is what makes the fallback record say
    # why it moved on.
    deepseek_api_key: str | None = None
    #: Where DeepSeek is posted. A setting for the reason `openai_base_url` is one: an
    #: installation may route through a gateway or a regional endpoint.
    deepseek_base_url: str = "https://api.deepseek.com"
    #: DeepSeek's model, separate from `chat_model` because the names are not interchangeable.
    deepseek_chat_model: str = "deepseek-chat"
    anthropic_api_key: str | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_chat_model: str = "claude-3-5-sonnet-latest"
    #: The LangSmith (option (A), §10.1) sink. **Unset means no trace leaves at all**, which
    #: is the right default: the filter exists to make an export safe, not to make one
    #: happen, and a deployment that has not chosen a backend should not acquire one.
    trace_sink: str | None = None
    #: The run id every trace of this process carries. Named so an operator can find the
    #: traces of one deployment; deliberately not the user id, which is not ours to send.
    trace_project: str = "eam-agent"

    @property
    def chat_provider_names(self) -> tuple[str, ...]:
        """The configured chain. See `chat_providers` and `parse_provider_chain`."""
        from app.domain.answer.chat import parse_provider_chain

        return parse_provider_chain(self.chat_providers, fallback=self.chat_provider_name)

    @property
    def chat_provider_name(self) -> str:
        """Which chat adapter this deployment uses. See `chat_provider`."""
        if self.chat_provider is not None:
            return self.chat_provider
        return "fake" if self.is_development or self.app_env == "test" else "openai"

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

    # --- the agent's drafts (ticket 40) -------------------------------------
    # How long a draft stands before it is `expired` and the employee is told to
    # generate it again. DESIGN §6.3 names 24 hours and calls it a default, so it is
    # a setting rather than a constant: `AGENT_DRAFT_TTL_HOURS=8` makes a draft last a
    # working day in a deployment that wants that, with no code change. It is *not* a
    # maintenance window — the clock that decides is the database's, and a draft that
    # lapsed is evidence of an offer that was never taken up (§3.6's trail).
    agent_draft_ttl_hours: int = 24

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
