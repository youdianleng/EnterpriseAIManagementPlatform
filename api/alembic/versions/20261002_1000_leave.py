"""Leave types, the year's balance, its ledger, and the requests that spend it.

Revision ID: 0019
Revises: 0018
Created: 2026-10-02

**Chain position:** 0018 (`20261001_1100_daily_digests.py`) → 0019 (this, ticket
25). The directory was read immediately before this file was written, because the
ids have collided three times in this project; ticket 26 (overtime) is being
written in the same window, and whichever of us lands second chains onto the other.

Four tables (DESIGN §3.2, D7, Q24, §8) and one seed. What is worth reading:

* **`leave_types` is seeded, because a system nobody can use without `psql` is not
  finished.** The four rows are the Spanish statutory shapes: `annual` (vacaciones,
  paid, spends the allowance), `sick` (baja por IT, paid, needs the parte de baja,
  spends nothing), `personal` (asuntos propios, unpaid, spends nothing) and
  `parental` (nacimiento y cuidado de menor, paid, spends nothing). One type for
  birth and childcare rather than a maternity/paternity pair, because since 2021
  the Estatuto de los Trabajadores gives both parents the same 16 weeks under that
  one name — encoding the older split would put a sex on a leave the law no longer
  distinguishes. HR maintains the catalogue afterwards; the codes are what a
  request, a report and an import name a type by, so they are unique for good.
* **The allowance is a parameter and the balance row is where it lands.** There is
  deliberately no `leave_settings` table: D7 names a single company-wide figure
  (`annual_leave_days=30`), a setting changes it with no code and no migration, and
  the per-person answer — `leave_balances.entitled_days`, written from that figure
  when the year is first needed — is what a request is actually checked against.
  `carried_over_days` is the half only a person can write.
* **`used_days + pending_days <= entitled_days + carried_over_days` is a CHECK, not
  a service rule.** Two submissions racing each other cannot pass a check the
  database performs on the row it is updating, which is what makes "额度不足时无法
  提交" true rather than likely.
* **`leave_balance_entries` is append-only** (`REVOKE UPDATE, DELETE`), like the
  expected-hours snapshot and for the same reason: it is the history of how a
  balance was reached, and evidence that can be edited is not evidence. Each row
  carries the four totals after its movement, so it reads on its own.
* **The sick-note rule is in the schema in three places.** There is no `reason`, no
  `note` and no free-text column on `leave_requests` at all — a free-text field on a
  sick leave is an invitation to write a diagnosis into the database, which §8's
  AEPD reading forbids. The one text column is `attachment_reference`, and
  `ck_leave_requests_attachment_reference` constrains it to the shape of a storage
  key, so a sentence cannot be stored in it either. The bytes are not here: ticket
  31's document store is where a file goes, and until it exists the reference is an
  opaque string that only HR may be shown.
* **No `DELETE` for the runtime role on `leave_requests`**, for the reason the
  corrections table gives: a rejected or withdrawn request is the record of a
  decision two people made, and removing it would rewrite an approval history.

No `GRANT` statement: migration 0007 set default privileges, so tables added later
are reachable by the runtime role without one. No row-level security either, and
that is a decision rather than an omission: `employee_private` carries policies
because the columns there are the ones an administrative query must not read, while
leave is read through the kernel's actions — `leave.read_own`, `leave.read_report`,
`leave.read_all` — exactly as attendance is.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "eam_app"

#: Matches `app/models/leave.py`. Literals rather than imports: a migration
#: describes the schema at one moment, and an import would let a later edit to the
#: model rewrite what this revision did.
BALANCE_ENTRY_TYPES = (
    "('grant', 'carry_over', 'adjustment', 'reserve', 'release', 'consume', 'refund')"
)
ATTACHMENT_REFERENCE = r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$"
MAX_BALANCE_DAYS = 366

#: The starter catalogue. Fixed uuids so a later migration, a fixture or an
#: installation's own report can name a type without a lookup by code.
LEAVE_TYPES: tuple[dict[str, object], ...] = (
    {
        "id": "1f0f5d5e-0001-4000-8000-000000000001",
        "code": "annual",
        "name_es": "Vacaciones anuales",
        "name_en": "Annual leave",
        "is_paid": True,
        "requires_attachment": False,
        "counts_against_annual": True,
    },
    {
        "id": "1f0f5d5e-0001-4000-8000-000000000002",
        "code": "sick",
        "name_es": "Baja por incapacidad temporal",
        "name_en": "Sick leave",
        "is_paid": True,
        "requires_attachment": True,
        "counts_against_annual": False,
    },
    {
        "id": "1f0f5d5e-0001-4000-8000-000000000003",
        "code": "personal",
        "name_es": "Permiso por asuntos propios",
        "name_en": "Personal leave",
        "is_paid": False,
        "requires_attachment": False,
        "counts_against_annual": False,
    },
    {
        "id": "1f0f5d5e-0001-4000-8000-000000000004",
        "code": "parental",
        "name_es": "Nacimiento y cuidado de menor",
        "name_en": "Birth and childcare leave",
        "is_paid": True,
        "requires_attachment": False,
        "counts_against_annual": False,
    },
)


def upgrade() -> None:
    op.create_table(
        "leave_types",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name_es", sa.String(length=160), nullable=False),
        sa.Column("name_en", sa.String(length=160), nullable=False),
        sa.Column("is_paid", sa.Boolean(), nullable=False),
        sa.Column("requires_attachment", sa.Boolean(), nullable=False),
        sa.Column("counts_against_annual", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("length(btrim(code)) > 0", name="ck_leave_types_code"),
        sa.CheckConstraint(
            "length(btrim(name_es)) > 0 AND length(btrim(name_en)) > 0",
            name="ck_leave_types_names",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_leave_types_code"),
    )
    op.bulk_insert(
        sa.table(
            "leave_types",
            sa.column("id", sa.UUID()),
            sa.column("code", sa.String()),
            sa.column("name_es", sa.String()),
            sa.column("name_en", sa.String()),
            sa.column("is_paid", sa.Boolean()),
            sa.column("requires_attachment", sa.Boolean()),
            sa.column("counts_against_annual", sa.Boolean()),
            sa.column("is_active", sa.Boolean()),
        ),
        [{**row, "is_active": True} for row in LEAVE_TYPES],
    )

    op.create_table(
        "leave_balances",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("year", sa.SmallInteger(), nullable=False),
        sa.Column("leave_type_id", sa.UUID(), nullable=False),
        sa.Column("entitled_days", sa.Integer(), nullable=False),
        sa.Column("carried_over_days", sa.Integer(), nullable=False),
        sa.Column("used_days", sa.Integer(), nullable=False),
        sa.Column("pending_days", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("year BETWEEN 2000 AND 2200", name="ck_leave_balances_year"),
        sa.CheckConstraint(
            f"entitled_days BETWEEN 0 AND {MAX_BALANCE_DAYS}", name="ck_leave_balances_entitled"
        ),
        sa.CheckConstraint(
            f"carried_over_days BETWEEN 0 AND {MAX_BALANCE_DAYS}",
            name="ck_leave_balances_carried",
        ),
        sa.CheckConstraint("used_days >= 0", name="ck_leave_balances_used"),
        sa.CheckConstraint("pending_days >= 0", name="ck_leave_balances_pending"),
        # The allowance, enforced where a race cannot pass it.
        sa.CheckConstraint(
            "used_days + pending_days <= entitled_days + carried_over_days",
            name="ck_leave_balances_within_allowance",
        ),
        # RESTRICT both ways: a balance is the record of what somebody was granted,
        # and it outlives the employee row's history and the type's retirement.
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["leave_type_id"], ["leave_types.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "employee_id", "year", "leave_type_id", name="uq_leave_balances_employee_year_type"
        ),
    )
    op.create_index("ix_leave_balances_employee_year", "leave_balances", ["employee_id", "year"])

    op.create_table(
        "leave_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("leave_type_id", sa.UUID(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("business_days_count", sa.Integer(), nullable=False),
        sa.Column("approval_request_id", sa.UUID(), nullable=True),
        sa.Column("attachment_reference", sa.String(length=200), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("end_date >= start_date", name="ck_leave_requests_window"),
        sa.CheckConstraint("business_days_count > 0", name="ck_leave_requests_days"),
        # The one text column holds a storage key, and the constraint says so: a
        # diagnosis typed into it has a space or an accent and is refused.
        sa.CheckConstraint(
            f"attachment_reference IS NULL OR attachment_reference ~ '{ATTACHMENT_REFERENCE}'",
            name="ck_leave_requests_attachment_reference",
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["leave_type_id"], ["leave_types.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_leave_requests_employee_start", "leave_requests", ["employee_id", "start_date"]
    )
    op.create_index("ix_leave_requests_covering", "leave_requests", ["start_date", "end_date"])
    op.create_index(
        "ix_leave_requests_unsettled",
        "leave_requests",
        ["submitted_at"],
        postgresql_where=sa.text("approval_request_id IS NOT NULL AND settled_at IS NULL"),
    )

    op.create_table(
        "leave_balance_entries",
        sa.Column("id", sa.UUID(), nullable=False),
        # The reading order. `created_at` is the transaction's start time, so every
        # entry one request writes shares it and ordering by it shuffles the ledger —
        # the same reason the audit trail numbers its rows.
        sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("balance_id", sa.UUID(), nullable=False),
        sa.Column("entry_type", sa.String(length=16), nullable=False),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("entitled_days", sa.Integer(), nullable=False),
        sa.Column("carried_over_days", sa.Integer(), nullable=False),
        sa.Column("used_days", sa.Integer(), nullable=False),
        sa.Column("pending_days", sa.Integer(), nullable=False),
        sa.Column("leave_request_id", sa.UUID(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by_employee_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            f"entry_type IN {BALANCE_ENTRY_TYPES}", name="ck_leave_balance_entries_type"
        ),
        sa.CheckConstraint("days <> 0", name="ck_leave_balance_entries_days"),
        sa.CheckConstraint("entitled_days >= 0", name="ck_leave_balance_entries_entitled"),
        sa.CheckConstraint("carried_over_days >= 0", name="ck_leave_balance_entries_carried"),
        sa.CheckConstraint("used_days >= 0", name="ck_leave_balance_entries_used"),
        sa.CheckConstraint("pending_days >= 0", name="ck_leave_balance_entries_pending"),
        sa.ForeignKeyConstraint(["balance_id"], ["leave_balances.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("seq", name="uq_leave_balance_entries_seq"),
    )
    op.create_index(
        "ix_leave_balance_entries_balance", "leave_balance_entries", ["balance_id", "seq"]
    )
    op.create_index(
        "ix_leave_balance_entries_request", "leave_balance_entries", ["leave_request_id"]
    )
    # The history is evidence: it cannot be rewritten or removed by the role that
    # serves requests, which is what makes "how was this computed" answerable.
    op.execute(f"REVOKE UPDATE, DELETE ON leave_balance_entries FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON leave_balance_entries TO {APP_ROLE}")
    # A request that was rejected, withdrawn or approved is the record of a decision
    # two people took; the same reasoning as `attendance_corrections`.
    op.execute(f"REVOKE DELETE ON leave_requests FROM {APP_ROLE}")


def downgrade() -> None:
    op.drop_index("ix_leave_balance_entries_request", table_name="leave_balance_entries")
    op.drop_index("ix_leave_balance_entries_balance", table_name="leave_balance_entries")
    op.drop_table("leave_balance_entries")
    op.drop_index("ix_leave_requests_unsettled", table_name="leave_requests")
    op.drop_index("ix_leave_requests_covering", table_name="leave_requests")
    op.drop_index("ix_leave_requests_employee_start", table_name="leave_requests")
    op.drop_table("leave_requests")
    op.drop_index("ix_leave_balances_employee_year", table_name="leave_balances")
    op.drop_table("leave_balances")
    op.drop_table("leave_types")
