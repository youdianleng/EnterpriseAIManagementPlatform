"""The daily digest's own table, and the language a mail is written in.

Revision ID: 0018
Revises: 0017
Created: 2026-10-01

**Chain position:** 0017 (`20261001_1000_attendance_corrections.py`, ticket 24) →
0018 (this, ticket 20). This file first claimed 0017 and yielded: ticket 24's
migration was written in the same window and onto the same parent, exactly as
0015/0016 collided before — and yielding here costs nothing, because the two
tickets touch disjoint tables and only one of them has to move.

Two changes (DESIGN §3.7, §10.4), and the decisions worth reading:

* **`(recipient_employee_id, digest_date)` is unique, and that index *is* ticket
  20's "one mail per recipient per day".** A second run of the job, a restarted
  container, a cron entry that fired twice: all three collide here instead of
  producing a second message, and the collision is a no-op rather than an error.
  The design's column name is `manager_employee_id`; it is `recipient_employee_id`
  here for the reason ticket 19 renamed `recipient_user_id` — the mail is addressed
  to a *person*, and that person is a manager in the ordinary case and an employee
  receiving their own outstanding-punch reminder in the other one. One row shape
  answers both, so there is one key rather than two mechanisms.
* **`anomaly_count > 0` is the "no daily noise" rule as a constraint.** A row is
  only written for a recipient with something to report, so a digest about a clean
  day cannot be expressed — not by this job, not by the next one, not by a
  hand-written INSERT.
* **`attempts` and `error` are on this table, not only on the deliveries.** The
  digest is the unit that is retried: composed once, sent once, and a failure is
  retried by the next run while `attempts < DIGEST_MAX_ATTEMPTS`. The delivery rows
  carry the same count because "was this notification emailed" is asked of them, but
  the retry decision is about this row.
* **`sent_at` is the idempotency's other half.** Written on success only, so
  "already mailed" is a timestamp rather than a status somebody has to interpret,
  and a crash between the send and the write leaves an unsent row that is retried —
  a duplicate is the acceptable failure direction for a mail, a lost one is not.
* **`users.locale`, nullable, checked to the two languages the interface ships
  in.** DESIGN §10.4 puts the persisted language preference here; until a profile
  screen writes it, the digest is its only reader and NULL means "write to them in
  the default language". Defaulting to `'es'` would have made "nobody has chosen"
  indistinguishable from "chose Spanish", which is the distinction the first
  profile screen will need.

No `GRANT` statement: migration 0007 set default privileges, so tables added later
are reachable by the runtime role without one. `users` is an existing table, so its
new column is covered by the grant the table already has.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "daily_digests",
        sa.Column("id", sa.UUID(), nullable=False),
        # A plain UUID, no foreign key to `employees`: what somebody was mailed
        # stays readable after they leave, the reasoning the notifications table
        # records for its own recipient column.
        sa.Column("recipient_employee_id", sa.UUID(), nullable=False),
        sa.Column("digest_date", sa.Date(), nullable=False),
        # The structured content that was composed. Never the rendered sentences: a
        # mail that has left the building has to stay reconstructible in either
        # language, and a stored sentence cannot be.
        sa.Column(
            "payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("anomaly_count", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        # The "avoid daily noise" rule: no row without something to report.
        sa.CheckConstraint("anomaly_count > 0", name="ck_daily_digests_anomaly_count"),
        sa.CheckConstraint("attempts >= 0", name="ck_daily_digests_attempts"),
        # The idempotency, and the conflict target of the job's write.
        sa.UniqueConstraint(
            "recipient_employee_id", "digest_date", name="uq_daily_digests_recipient_date"
        ),
    )
    # One day across all recipients, which is how the job reads the table.
    op.create_index("ix_daily_digests_date", "daily_digests", ["digest_date"])
    # What a run still owes somebody. Partial: a sent row is never asked about
    # again, and those are the ones that accumulate.
    op.create_index(
        "ix_daily_digests_unsent",
        "daily_digests",
        ["digest_date"],
        postgresql_where=sa.text("sent_at IS NULL"),
    )

    # Nullable with no default: "nobody has chosen a language for this account" is
    # a state the reader has to be able to see.
    op.add_column("users", sa.Column("locale", sa.String(length=5), nullable=True))
    op.create_check_constraint("ck_users_locale", "users", "locale IS NULL OR locale IN ('es', 'en')")


def downgrade() -> None:
    op.drop_constraint("ck_users_locale", "users", type_="check")
    op.drop_column("users", "locale")
    op.drop_index("ix_daily_digests_unsent", table_name="daily_digests")
    op.drop_index("ix_daily_digests_date", table_name="daily_digests")
    op.drop_table("daily_digests")
