"""`salary_records`: an effective-dated archive nobody computes from (ticket 43).

Revision ID: 0028
Revises: 0027
Created: 2026-10-10

**Chain position:** 0027 (`20261009_1000_agent_actions.py`, ticket 40) → 0028 (this,
ticket 43). The directory *and* `alembic heads` were read immediately before this file
was written — `heads` reported `0027` and nothing else — because the ids have collided
repeatedly in this project.

**One table, and it is DESIGN §3.5's `salary_records` with three readings recorded
here.**

* **`base_salary` is `NUMERIC(14, 2)`, not a float.** §3.5 names the column and D9 makes
  this table an archive; an archive whose figures change by a cent when they are read
  back is not one. PostgreSQL's `numeric` is exact decimal arithmetic, the paired
  Python value is `decimal.Decimal`, and the API serialises it as a string — so
  `123456789.01` is the same eleven digits at rest, in the row, in the response and in
  the test. A `double precision` column would round it at the first hop, and no
  constraint would say so. The scale is fixed at two because the system's money is
  euros and cents; a currency with three decimal places would be a widening of this
  column, which the ticket notes rather than guesses at.

* **`components` is a JSONB *array* of objects, one per allowance line**, which is what
  §3.5's 「津贴明细」 means by a structured breakdown: `code`, `label`, `amount`. The CHECK
  below refuses a string, an object or an array of scalars, because a prose blob in that
  column is exactly the shape the design rejects ("its own column, not prose") — and a
  shape rule PostgreSQL enforces cannot be forgotten by the next writer the way a
  service-side validator can. The amounts inside are decimal *strings*, the same
  decision as `base_salary` one level down: JSON has one number type and it is a float.

* **`ck_salary_records_effective_range` and the exclusion constraint below are the
  ticket's two structural claims.** The first says a half-open range is not a range
  (`effective_to` is either absent or on/after `effective_from`), and the second says
  two records for one employee may not cover the same day. `btree_gist` was enabled by
  migration 0001; with it, `EXCLUDE USING gist (employee_id WITH =, daterange(...) WITH
  &&)` makes an overlap *unrepresentable* rather than merely refused by a service, which
  is what the ticket asks for and what `docs/DESIGN.md` §3.2 already does for
  `employee_schedule_overrides`. An unbounded upper end is representable on purpose:
  the record in force today has no end until somebody says when it stops.

**Append-only, in the two places it matters.** There is no UPDATE path in the module at
all — a correction is a new record — and the runtime role is denied `UPDATE` and
`DELETE` outright, so a future write path cannot quietly gain one. That is the same
narrowing tickets 24, 26 and 34 applied to their own ledgers.

**The row-level policy is the *narrower* half of the rule, deliberately.** The
application's `salary.read_all` admits HR and finance; the policy here admits the
person the row is about, or a caller whose published roles intersect
`ARRAY['hr', 'finance']`. It is written from the roles rather than from
`app.is_privileged` because that flag is also true for `compliance`, and §4.1 gives
compliance the audit trail and not the payroll record — a policy *wider* than the rule
it backs up is the direction ticket 36 found and fixed on documents, so this one states
its own two roles and makes the audit reader unreadable to the reader who only reads
the audit. An administrator is in neither clause: §4.1 separates the duties, and the
refusal is proved over the restricted role in `tests/test_salary_records.py`.

**No `GRANT` statement**: migration 0007's `ALTER DEFAULT PRIVILEGES` gives the runtime
role DML on tables created later in `public`. Only the one *narrowing* — `REVOKE UPDATE,
DELETE` — is written here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Spelled here as migration 0007 spells it: a migration describes the schema it
#: applied, and the application's constant for the same name may move afterwards.
APP_ROLE = "eam_app"

#: The policy names, written out so `downgrade` drops exactly what `upgrade` created.
READ = "salary_records_read"
INSERT = "salary_records_insert"

#: The settings the policy reads, through the accessors migration 0007 wrote.
#: `app_setting` folds a written-then-abandoned empty string into NULL, so a request
#: with no context reads as NULL, NULL never equals a uuid, and the row is refused —
#: the failure mode of a missing context is silence rather than disclosure.
ME = "app_setting('app.current_employee_id')::uuid"
ROLES = "app_setting_array('app.current_roles')"

#: The two roles §4.1 gives the payroll record: 人力资源 keeps 薪酬档案 and 财务 owns the
#: payslips. Written as a literal array rather than read from the catalogue, because a
#: policy cannot import Python and a migration describes its own moment — and because
#: the *narrowness* is the point: `admin` and `compliance` are absent by name.
PAYROLL_ROLES = "ARRAY['hr', 'finance']"

#: The vocabularies, written as the literals they were applied with rather than imported
#: from `app.domain.payroll.models`: a migration must describe the schema of its own
#: moment.
PAY_PERIODS = ("monthly", "biweekly", "weekly")
REASON_TYPES = ("initial", "adjustment", "correction")
CURRENCY = r"^[A-Z]{3}$"


def upgrade() -> None:
    _create_component_validator()
    op.create_table(
        "salary_records",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        # Open-ended while the record is the one in force: NULL is "no end stated",
        # which is a state rather than a missing value.
        sa.Column("effective_to", sa.Date(), nullable=True),
        # Exact, and see the module docstring: this is the column a float would ruin.
        sa.Column("base_salary", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("pay_period", sa.String(length=12), nullable=False),
        # The structured allowance breakdown (§3.5's 「津贴明细」). `'[]'` is the
        # ordinary case: most records carry a base and nothing else.
        sa.Column(
            "components",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("change_reason_type", sa.String(length=12), nullable=False),
        sa.Column("change_reason", sa.Text(), nullable=False),
        # Who entered it, as the *account* rather than the employee: the archive's
        # question is "which login wrote this", and an employee with no account cannot
        # have written one.
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "pay_period IN ("
            + ", ".join(f"'{value}'" for value in PAY_PERIODS)
            + ")",
            name="ck_salary_records_pay_period",
        ),
        sa.CheckConstraint(
            "change_reason_type IN ("
            + ", ".join(f"'{value}'" for value in REASON_TYPES)
            + ")",
            name="ck_salary_records_reason_type",
        ),
        sa.CheckConstraint(f"currency ~ '{CURRENCY}'", name="ck_salary_records_currency"),
        # A half-open range is not a range: an end before the start would make the
        # record cover no day at all while still excluding that window from every other
        # record, which is a hole in the archive nobody would see.
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_salary_records_effective_range",
        ),
        sa.CheckConstraint("base_salary > 0", name="ck_salary_records_base_positive"),
        # The shape of the breakdown, not its contents: an array, every element of it an
        # object. A string, a bare object or a scalar inside the array is refused by
        # PostgreSQL rather than by a validator somebody has to remember to call.
        #
        # Through a function because PostgreSQL refuses a subquery inside a CHECK
        # ("cannot use subquery in check constraint"), and the rule here *is* "no element
        # of the array is anything but an object". `salary_components_are_lines` is
        # created by `_create_component_validator` below, is `IMMUTABLE` because a CHECK
        # may only call something whose answer cannot change, and never raises:
        # `jsonb_array_elements` throws on an object, so the `CASE` makes it total and
        # the constraint answers for every value rather than only for the arrays.
        sa.CheckConstraint(
            "salary_components_are_lines(components)",
            name="ck_salary_records_components_are_lines",
        ),
        sa.CheckConstraint(
            "length(btrim(change_reason)) > 0", name="ck_salary_records_change_reason"
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )

    # The chain read: one employee's records, oldest first, which is also the order the
    # effective-date query walks.
    op.create_index(
        "ix_salary_records_employee_from",
        "salary_records",
        ["employee_id", "effective_from"],
    )
    # "Which record was in force on this day", as an index the range query can use.
    op.create_index(
        "ix_salary_records_effective",
        "salary_records",
        ["effective_from", "effective_to"],
    )
    # Only one opening record per employee, enforced rather than assumed. A second
    # `initial` is a double entry of the same hire, and the entry that would lose the
    # race is the one a reviewer never sees.
    op.create_index(
        "uq_salary_records_initial",
        "salary_records",
        ["employee_id"],
        unique=True,
        postgresql_where=sa.text("change_reason_type = 'initial'"),
    )

    # The ticket's non-overlap guarantee, as a database fact. `daterange(from, to, '[]')`
    # is inclusive at both ends; `daterange(from, NULL, '[]')` is unbounded above, and an
    # unbounded range overlaps everything from `from` onwards — which is what "the record
    # in force has no end yet" has to mean for the constraint to be worth having.
    op.execute(
        """
        ALTER TABLE salary_records
        ADD CONSTRAINT ex_salary_records_no_overlap
        EXCLUDE USING gist (
            employee_id WITH =,
            daterange(effective_from, effective_to, '[]') WITH &&
        )
        """
    )

    _attach_policies()

    # Append-only: the archive's rows are evidence, and a correction is a *new* record
    # rather than an edit of the old one. The role that serves requests cannot rewrite
    # or remove one, so "the old row keeps its dates" is a property of the database and
    # not of the module's good manners.
    op.execute(f"REVOKE UPDATE, DELETE ON salary_records FROM {APP_ROLE}")


def _create_component_validator() -> None:
    """The `components` shape rule, as a function the CHECK constraint calls.

    A CHECK may not contain a subquery, and "every element of this JSON array is an
    object" is a subquery. So the rule is a function instead, `IMMUTABLE` because a
    check may only call something whose answer cannot change, and total because
    `jsonb_array_elements` raises on the inputs the first clause exists to refuse.

    `STRICT` is deliberately *not* used: a NULL `components` would then make the
    function NULL, and a NULL check passes. The column is NOT NULL anyway, and this
    way the rule answers `false` for a value it cannot read rather than abstaining.
    """
    op.execute(
        """
        CREATE OR REPLACE FUNCTION salary_components_are_lines(value jsonb)
        RETURNS boolean AS $$
            SELECT jsonb_typeof(value) = 'array'
               AND NOT EXISTS (
                   SELECT 1
                   FROM jsonb_array_elements(
                       CASE WHEN jsonb_typeof(value) = 'array' THEN value ELSE '[]'::jsonb END
                   ) AS line
                   WHERE jsonb_typeof(line) <> 'object'
               )
        $$ LANGUAGE sql IMMUTABLE
        """
    )


def _attach_policies() -> None:
    """The narrower half of the rule. See the module docstring."""
    op.execute("ALTER TABLE salary_records ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {READ} ON salary_records
        FOR SELECT
        USING (
            employee_id = {ME}
            OR {ROLES} && {PAYROLL_ROLES}
        )
        """
    )
    # The same rule read forwards. Without an INSERT policy, row-level security applies
    # the SELECT policy's `USING` clause to the new row and refuses every insert — and
    # the policy is *not* `employee_id = me`, because nobody writes their own salary:
    # the write is HR's, and the row it inserts belongs to somebody else.
    op.execute(
        f"""
        CREATE POLICY {INSERT} ON salary_records
        FOR INSERT
        WITH CHECK ({ROLES} && {PAYROLL_ROLES})
        """
    )


def downgrade() -> None:
    """Drop the table and everything with it.

    The table *is* the feature: an effective-dated archive has no meaning without its
    rows. A downgrade of ticket 43 is a decision to discard every salary record in the
    installation, and `salary_records` is the only table this revision creates.
    """
    op.execute(f"DROP POLICY IF EXISTS {INSERT} ON salary_records")
    op.execute(f"DROP POLICY IF EXISTS {READ} ON salary_records")
    op.execute("ALTER TABLE salary_records DROP CONSTRAINT IF EXISTS ex_salary_records_no_overlap")
    op.drop_index("uq_salary_records_initial", table_name="salary_records")
    op.drop_index("ix_salary_records_effective", table_name="salary_records")
    op.drop_index("ix_salary_records_employee_from", table_name="salary_records")
    op.drop_table("salary_records")
    # After the table, which is the only thing that referenced it.
    op.execute("DROP FUNCTION IF EXISTS salary_components_are_lines(jsonb)")
