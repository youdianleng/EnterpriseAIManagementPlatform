"""The notification centre's two tables.

Revision ID: 0011
Revises: 0010
Created: 2026-09-27

Four decisions are worth reading before the DDL:

* **`(recipient_employee_id, dedupe_key)` is unique.** That index *is* the ticket's
  "the same event never produces two in-app notifications" — a retry, a double
  submission or a worker that ran twice lands on the same row instead of a second
  one. Checking in application code first would be the race the index exists to
  close.
* **`expires_at` is a read filter, not a deletion.** The list and the badge exclude
  expired rows; the rows stay, because "was this person told, and when" outlives
  the notification being worth showing.
* **The database refuses a sentence.** `title_key` has to match a dotted lowercase
  key and `payload` has to be a JSON *object*, so "store the wording in the row"
  and "store the payload as text" are both refused by PostgreSQL rather than
  caught in review.
* **No row-level policy here, deliberately.** The row belongs to one person, and
  the application-side filter is structural — every repository read takes the
  recipient and there is no read-by-id — but a recipient-scoped SELECT policy
  would break the writer: a notifier legitimately inserts rows *for other people*
  (the approver tells the requester), so it cannot see the very row its dedupe
  check has to collide with, and `ON CONFLICT DO NOTHING` raises a unique
  violation instead of doing nothing when the conflicting row is invisible to the
  policy. The 403 is enforced in one statement with both conditions, and the
  refusal is recorded.

No `GRANT` statement: migration 0007 set default privileges so tables added later
are reachable by the runtime role without one, "because the one that forgets is
the bug this avoids".
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.UUID(), nullable=False),
        # A plain UUID, no foreign key to `employees`: what somebody was told stays
        # readable after they leave, and a notification needs no account to exist.
        sa.Column("recipient_employee_id", sa.UUID(), nullable=False),
        # The event, not the wording. Free-form rather than a CHECK constraint: this
        # vocabulary grows with every module that learns to notify somebody, and a
        # constraint would make each of those a migration of a table they do not own.
        sa.Column("type", sa.String(length=60), nullable=False),
        # The bilingual dictionary key the client renders (DESIGN §3.7, D2).
        sa.Column("title_key", sa.String(length=120), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("entity_type", sa.String(length=60), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        # What makes two raises one event. Unique per recipient, below.
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        # A timestamp rather than a flag: "when did they see it" is the same column.
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # NULL means it never expires, not that it already has.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            r"title_key ~ '^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$'", name="ck_notifications_title_key"
        ),
        sa.CheckConstraint("length(btrim(type)) > 0", name="ck_notifications_type"),
        sa.CheckConstraint("length(btrim(dedupe_key)) > 0", name="ck_notifications_dedupe_key"),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_notifications_payload"),
        sa.CheckConstraint("length(btrim(entity_type)) > 0", name="ck_notifications_entity_type"),
        sa.UniqueConstraint(
            "recipient_employee_id", "dedupe_key", name="uq_notifications_recipient_dedupe"
        ),
    )
    # The list: one person's notifications, newest first.
    op.create_index(
        "ix_notifications_recipient_created", "notifications", ["recipient_employee_id", "created_at"]
    )
    # The badge. Partial, because unread rows are the ones that accumulate.
    op.create_index(
        "ix_notifications_recipient_unread",
        "notifications",
        ["recipient_employee_id"],
        postgresql_where=sa.text("read_at IS NULL"),
    )
    op.create_index("ix_notifications_entity", "notifications", ["entity_type", "entity_id"])

    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("notification_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        # Why it failed, as the sender reported it — never a rendered message.
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["notification_id"], ["notifications.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # The two channels, and their three states: closed vocabularies, unlike
        # `notifications.type` above.
        sa.CheckConstraint("channel IN ('inapp', 'email')", name="ck_notification_deliveries_channel"),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed')", name="ck_notification_deliveries_status"
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_notification_deliveries_attempts"),
        # One row per channel per notification: a retry updates this row or it
        # appends nothing at all.
        sa.UniqueConstraint(
            "notification_id", "channel", name="uq_notification_deliveries_channel"
        ),
    )
    op.create_index(
        "ix_notification_deliveries_notification", "notification_deliveries", ["notification_id"]
    )
    # What still has to go out, which is what a sending worker reads.
    op.create_index(
        "ix_notification_deliveries_pending",
        "notification_deliveries",
        ["channel", "status"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    # Deliveries first: they carry the foreign key.
    op.drop_index("ix_notification_deliveries_pending", table_name="notification_deliveries")
    op.drop_index(
        "ix_notification_deliveries_notification", table_name="notification_deliveries"
    )
    op.drop_table("notification_deliveries")
    op.drop_index("ix_notifications_entity", table_name="notifications")
    op.drop_index("ix_notifications_recipient_unread", table_name="notifications")
    op.drop_index("ix_notifications_recipient_created", table_name="notifications")
    op.drop_table("notifications")
