"""Accounts and the audit log.

Revision ID: 0004
Revises: 0003
Created: 2026-09-26

Two tables, and one constraint worth explaining:

* `users.employee_id` is UNIQUE, which is what makes "one account per employee"
  true independently of application behaviour — two administrators acting at the
  same moment cannot both win.
* `users.password_hash` has a CHECK requiring the Argon2id prefix. The originating
  answer for this ticket mentioned SHA-256; storing a password under a fast
  digest is unsafe, and the constraint means a plaintext value or a foreign
  format cannot be written here even by mistake.

`audit_log` is append-only by intent. It is not yet enforced at the database
level because the application still connects as the table owner; ticket 13
introduces a restricted role with INSERT and SELECT only, which is the point at
which the guarantee stops depending on application code.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("must_change_password", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("session_epoch", sa.Integer(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.Column("password_changed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("length(username) >= 3", name="ck_users_username_length"),
        sa.CheckConstraint(
            "password_hash LIKE '$argon2id$%'", name="ck_users_password_hash_is_argon2id"
        ),
        sa.CheckConstraint("session_epoch >= 1", name="ck_users_session_epoch"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # One account per employee, enforced by the database rather than by a
        # check that two concurrent requests could both pass.
        sa.UniqueConstraint("employee_id", name="uq_users_employee_id"),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column(
            "actor_roles",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("entity_type", sa.String(length=60), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=True),
        sa.Column("before", postgresql.JSONB(), nullable=True),
        sa.Column("after", postgresql.JSONB(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "initiated_by", sa.String(length=16), server_default=sa.text("'user'"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_entity", "audit_log", ["entity_type", "entity_id"])
    op.create_index("ix_audit_log_actor", "audit_log", ["actor_user_id"])
    op.create_index("ix_audit_log_occurred_at", "audit_log", ["occurred_at"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_index("ix_audit_log_occurred_at", table_name="audit_log")
    op.drop_index("ix_audit_log_actor", table_name="audit_log")
    op.drop_index("ix_audit_log_entity", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("users")
