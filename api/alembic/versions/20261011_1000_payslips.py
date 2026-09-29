"""`payslips` and `payslip_batches`: a month of files, and who is missing one (ticket 44).

Revision ID: 0029
Revises: 0028
Created: 2026-10-11

**Chain position:** 0028 (`20261010_1000_salary_records.py`, ticket 43) → 0029 (this,
ticket 44). The directory *and* `alembic heads` were read immediately before this file
was written — `heads` reported `0028` and nothing else — because the ids have collided
repeatedly in this project.

**Two tables, and they are DESIGN §3.5's `payslips` and `payslip_batches` with four
readings recorded here.**

* **A payslip is not a document.** It is not stored in `documents` and no row here is a
  knowledge-base entry: §3.5 gives it its own table, §7.5 calls it 财务批量上传 and the
  only reader the design ever names is its owner (ticket 45). Putting a payslip in
  `documents` would put it in the corpus's *table* — and `documents.owner_employee_id`
  is one of the two clauses of §4.2, so a personal document's own text is retrievable by
  its owner (`repositories/retrieval.py::visible_document_clauses`, clause 1). A
  payslip's text must be retrievable by **nobody**, including its owner, so the table
  that the retrieval predicate reads is the one table it must not be in. The file's
  bytes live under the payroll module's own storage root and the row records the key;
  `tests/test_payslips.py` searches the retrieval path for a payslip's text and asserts
  it finds nothing and that `documents` is empty.

* **`checksum_sha256` and `file_size` are the ticket's 「事后核对是否被替换」.** They are
  the two facts that make a replacement detectable *afterwards* rather than merely
  announced at the time: a stored file whose bytes no longer hash to the row's checksum
  is a file that changed outside this module, and two rows for one `(employee, period)`
  with different checksums are a replacement that happened. `UNIQUE (employee_id,
  period)` is what makes "the payslip for that month" a single answer — a re-upload is
  an `ON CONFLICT DO UPDATE` of that row, so the checksum moves and the size moves, and
  the row that survives is the one the employee will be handed.

* **`status` is `published` or `withdrawn`, and the row-level policy reads it.** §7.5
  says 只有已发布的对员工可见, and the policy below is where that is made true rather
  than promised: a caller reading as the owner sees `status = 'published'` and nothing
  else, so a withdrawn payslip disappears from the employee's own reach at the database
  level. `withdraw_reason` and `withdrawn_at` are §3.5's columns and ticket 46's act;
  this revision stores them and writes neither, and the CHECK that keeps them coherent
  is written now so that ticket 46 cannot add a withdrawn row with no reason.

* **The row's identity is immutable, and a trigger says so.** Unlike `salary_records` the
  table *must* admit an UPDATE — ticket 46 withdraws a payslip, and the replacement rule
  itself is an upsert — so the narrowing is per column rather than `REVOKE UPDATE`.
  `payslips_identity_is_fixed()` raises on any change to `id`, `employee_id`, `period`,
  `created_at` or `uploaded_by_user_id`: whose payslip this is, which month it is for, when
  the slot was opened, and which login opened it. Those five are the row's identity, and
  any of them moving is either an accident or an attempt to make one person's payslip look
  like another's.

  **What the trigger deliberately does not protect is the file's own columns**, and the
  reason is the ticket's replacement rule: 「同一员工同一月份重复上传被视为替换」 *is* an update
  of `storage_path`, `content_sha256` and `file_size` — a re-upload is `INSERT … ON
  CONFLICT (employee_id, period) DO UPDATE`, and a `BEFORE UPDATE` trigger fires on the
  update half of it. The stricter rule (freeze the file's columns as well) was written
  first and refused a legitimate replacement, which is how this was found; the checksum
  still does the job the ticket asks of it, because the row's hash moving is what makes a
  replacement detectable afterwards, and the previous one travels back in the answer.

  **`DELETE` is revoked outright.** Nothing in this ticket removes a payslip and a
  withdrawn one stays behind its status, so the role that serves requests holds no
  `DELETE` on either table.

**The verdict on the employee-number matching is not in this file.** Which employee a
file belongs to is a decision the service makes (`domain/payslip/matching.py`), and the
row simply records the answer. `employee_id` is `NOT NULL` because an unattributed file
has no row at all — it appears in the batch's own answer with a reason, which is what
「不静默丢弃」 means and why `payslip_batches.unmatched` carries the list.

**No `GRANT` statement**: migration 0007's `ALTER DEFAULT PRIVILEGES` gives the runtime
role DML on tables created later in `public`. The narrowings written here are the
trigger, the `REVOKE DELETE`, and the policies.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Spelled here as migration 0007 spells it: a migration describes the schema it
#: applied, and the application's constant for the same name may move afterwards.
APP_ROLE = "eam_app"

#: The policy names, written out so `downgrade` drops exactly what `upgrade` created.
BATCH_READ = "payslip_batches_read"
BATCH_INSERT = "payslip_batches_insert"
BATCH_UPDATE = "payslip_batches_update"
PAYSLIP_READ = "payslips_read"
PAYSLIP_INSERT = "payslips_insert"
PAYSLIP_UPDATE = "payslips_update"

#: The settings the policies read, through the accessors migration 0007 wrote.
#: `app_setting` folds a written-then-abandoned empty string into NULL, so a request
#: with no context reads as NULL, NULL never equals a uuid, and the row is refused —
#: the failure mode of a missing context is silence rather than disclosure.
ME = "app_setting('app.current_employee_id')::uuid"
ROLES = "app_setting_array('app.current_roles')"

#: The one role that may hand a payslip over: §4.1 gives `finance` the payroll record and
#: the payslip batches, and §7.5's flow opens with 财务. Written as a literal array rather
#: than read from the catalogue, because a policy cannot import Python and a migration
#: describes its own moment — and the *narrowness* is the ticket: `hr` and `admin` are
#: refused by the handler with a 403, and the policy refuses them here as well rather than
#: relying on the handler. This is the separation-of-duties rule §4.1 names by denying an
#: administrator even the payslip's contents.
FINANCE = "ARRAY['finance']"

#: The statuses, written as the literals they were applied with rather than imported from
#: `app.domain.payslip.models`: a migration must describe the schema of its own moment.
STATUSES = ("published", "withdrawn")

#: The columns that are the row's *identity*: whose payslip it is, which month it is for,
#: when the slot was opened and which login opened it. See the module docstring for why the
#: file's own columns are deliberately not in this list — the replacement rule is an upsert
#: of exactly those.
IMMUTABLE_COLUMNS = (
    "id",
    "employee_id",
    "period",
    "created_at",
    "uploaded_by_user_id",
)


def upgrade() -> None:
    # The shape validators first: the tables below name them in CHECK constraints, and
    # PostgreSQL resolves a function reference when the constraint is created — so a
    # `CREATE TABLE` that calls a function which does not exist yet is refused outright.
    # (Ticket 43's migration puts its own validator first for the same reason.)
    _create_shape_validators()
    op.create_table(
        "payslip_batches",
        sa.Column("id", sa.UUID(), nullable=False),
        #: `YYYY-MM`. A batch is a month's upload, and the format is enforced rather than
        #: assumed: "2026-3" and "2026-03" would be two batches for one month.
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("uploaded_by_user_id", sa.UUID(), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False),
        #: §3.5's 「缺失清单」 as it stood when the batch was answered. The live list is
        #: derived (`domain/payslip/service.py`), and this column is the point-in-time
        #: record of what the uploader was shown — which is the question an incident
        #: review asks, and the one a live query cannot answer afterwards.
        sa.Column(
            "missing_employee_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        #: The files that matched nobody, with their reasons. The batch's own row carries
        #: them because they belong to no `payslips` row: an unattributed file has no
        #: employee to hang from, and dropping it would be the silent loss 「不静默丢弃」
        #: refuses.
        sa.Column(
            "unmatched",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'", name="ck_payslip_batches_period"),
        sa.CheckConstraint("total_count >= 0", name="ck_payslip_batches_total"),
        sa.CheckConstraint("success_count >= 0", name="ck_payslip_batches_success"),
        sa.CheckConstraint(
            "success_count <= total_count", name="ck_payslip_batches_success_within_total"
        ),
        sa.CheckConstraint(
            "payslip_entries_are_objects(missing_employee_ids)",
            name="ck_payslip_batches_missing_are_uuids",
        ),
        sa.CheckConstraint(
            "payslip_unmatched_are_entries(unmatched)",
            name="ck_payslip_batches_unmatched_are_entries",
        ),
        sa.ForeignKeyConstraint(["uploaded_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    # The history of one month's uploads, newest first: what a payroll reader asks when a
    # payslip looks wrong and they want to see who filed the month and when.
    op.create_index(
        "ix_payslip_batches_period", "payslip_batches", ["period", "created_at"]
    )

    op.create_table(
        "payslips",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("employee_id", sa.UUID(), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        #: The key under the payroll storage root, never an absolute path. Same shape as
        #: `documents.storage_path`: `<sha256[0:2]>/<sha256>.pdf`.
        sa.Column("storage_path", sa.String(length=200), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        #: The content hash, which is what makes a replacement detectable afterwards.
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("original_filename", sa.String(length=200), nullable=False),
        sa.Column("uploaded_by_user_id", sa.UUID(), nullable=False),
        sa.Column("batch_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdraw_reason", sa.Text(), nullable=True),
        sa.Column("download_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'", name="ck_payslips_period"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in STATUSES) + ")",
            name="ck_payslips_status",
        ),
        sa.CheckConstraint("file_size > 0", name="ck_payslips_file_size"),
        sa.CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_payslips_checksum"),
        sa.CheckConstraint("download_count >= 0", name="ck_payslips_download_count"),
        # A withdrawn payslip states when and why. Written now, while the columns exist
        # and before anything can write one: ticket 46's withdrawal is the first row that
        # will have to satisfy it, and a rule added after the fact is a rule that would
        # have to be backfilled.
        sa.CheckConstraint(
            "(status = 'published' AND withdrawn_at IS NULL AND withdraw_reason IS NULL) "
            "OR (status = 'withdrawn' AND withdrawn_at IS NOT NULL "
            "AND length(btrim(withdraw_reason)) > 0)",
            name="ck_payslips_withdrawal_is_stated",
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["uploaded_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["batch_id"], ["payslip_batches.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        # The ticket's replacement rule, as a database fact: one payslip per person per
        # month. A re-upload is an upsert of this row, so "the payslip for that month" is
        # a single answer rather than a list a download would have to choose from.
        sa.UniqueConstraint("employee_id", "period", name="uq_payslips_employee_period"),
    )
    op.create_index("ix_payslips_period", "payslips", ["period", "employee_id"])
    # The missing list's query: which of these employees already have a `published`
    # payslip for this month.
    op.create_index(
        "ix_payslips_period_status", "payslips", ["period", "status"]
    )

    _create_immutability_guard()
    _attach_policies()

    # Nothing in this ticket removes a payslip. A withdrawal is a status, and the row
    # stays behind it, so the role that serves requests has no DELETE at all. UPDATE is
    # deliberately *not* revoked — ticket 46's withdrawal needs it — and the trigger above
    # is what narrows it to the status columns instead.
    op.execute(f"REVOKE DELETE ON payslips FROM {APP_ROLE}")
    op.execute(f"REVOKE DELETE ON payslip_batches FROM {APP_ROLE}")


def _create_shape_validators() -> None:
    """The two JSONB shape rules, as functions the CHECK constraints call.

    A CHECK may not contain a subquery, and both rules are "every element of this array
    is ...". They are `IMMUTABLE` because a check may only call something whose answer
    cannot change, and total because `jsonb_array_elements` raises on the inputs the
    first clause exists to refuse — `STRICT` is deliberately not used, so a NULL answers
    `false` rather than abstaining.
    """
    # `missing_employee_ids` is an array of uuid *strings*. The rule the database can
    # state is "an array whose every element is a string"; whether each string is a uuid
    # is not expressible without a cast that would raise, and the service is what composes
    # this value (`domain/payslip/models.py`).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION payslip_entries_are_objects(value jsonb)
        RETURNS boolean AS $$
            SELECT jsonb_typeof(value) = 'array'
               AND NOT EXISTS (
                   SELECT 1
                   FROM jsonb_array_elements(
                       CASE WHEN jsonb_typeof(value) = 'array' THEN value ELSE '[]'::jsonb END
                   ) AS entry
                   WHERE jsonb_typeof(entry) <> 'string'
               )
        $$ LANGUAGE sql IMMUTABLE
        """
    )
    # `unmatched` is an array of objects, one per file that matched nobody: a filename and
    # a reason. A prose blob in that column is the shape 不静默丢弃 refuses, because
    # "which files were not attributed, and why" would stop being a query.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION payslip_unmatched_are_entries(value jsonb)
        RETURNS boolean AS $$
            SELECT jsonb_typeof(value) = 'array'
               AND NOT EXISTS (
                   SELECT 1
                   FROM jsonb_array_elements(
                       CASE WHEN jsonb_typeof(value) = 'array' THEN value ELSE '[]'::jsonb END
                   ) AS entry
                   WHERE jsonb_typeof(entry) <> 'object'
               )
        $$ LANGUAGE sql IMMUTABLE
        """
    )


def _create_immutability_guard() -> None:
    """The row's identity, frozen against UPDATE.

    See the module docstring for why this table is narrowed per column rather than by
    `REVOKE UPDATE`, and for why the *file's* columns are not in the list: the replacement
    rule is an upsert of them. The comparison is `IS DISTINCT FROM` on the row's `to_jsonb`
    image, so a column a later migration adds is not protected until somebody adds it here
    — which is the honest direction: an unknown column is not silently frozen, and the list
    above is the statement of what this revision protects.
    """
    columns = ", ".join(f"'{name}'" for name in IMMUTABLE_COLUMNS)
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION payslips_immutable_columns()
        RETURNS trigger AS $$
        DECLARE
            before_image jsonb := to_jsonb(OLD);
            after_image jsonb := to_jsonb(NEW);
            column_name text;
        BEGIN
            FOREACH column_name IN ARRAY ARRAY[{columns}] LOOP
                IF before_image -> column_name IS DISTINCT FROM after_image -> column_name THEN
                    RAISE EXCEPTION
                        'payslips.% is immutable (old=%, new=%): a replacement is a new '
                        'upload, not an edit of the stored file',
                        column_name,
                        before_image -> column_name,
                        after_image -> column_name
                        USING ERRCODE = 'check_violation';
                END IF;
            END LOOP;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_payslips_immutable_columns
        BEFORE UPDATE ON payslips
        FOR EACH ROW EXECUTE FUNCTION payslips_immutable_columns()
        """
    )


def _attach_policies() -> None:
    """The narrower half of the rule, written from roles rather than `is_privileged`.

    For the reason ticket 43's migration records: `app.is_privileged` is also true for
    `compliance`, and §4.1 gives compliance the audit trail rather than the payroll
    record. A policy *wider* than the rule it backs up is the direction ticket 36 found
    and fixed on documents, so this one names `finance` and the owner and nothing else.
    """
    op.execute("ALTER TABLE payslips ENABLE ROW LEVEL SECURITY")
    # The owner sees their own **published** payslips; finance sees the month. A withdrawn
    # payslip is invisible to the employee through this clause, which is §7.5's
    # 只有已发布的对员工可见 made true at the database rather than promised by a query.
    # Ticket 45's self-service read is what will exercise the owner's half; it is stated
    # here because the policy is a property of the row, not of the endpoint that reads it.
    op.execute(
        f"""
        CREATE POLICY {PAYSLIP_READ} ON payslips
        FOR SELECT
        USING (
            (employee_id = {ME} AND status = 'published')
            OR {ROLES} && {FINANCE}
        )
        """
    )
    # Only finance files a payslip: nobody uploads their own, and HR does not upload
    # anybody's. Written as `ROLES && ARRAY['finance']` rather than
    # `uploaded_by_user_id = me`, because the row's subject is somebody else.
    op.execute(
        f"""
        CREATE POLICY {PAYSLIP_INSERT} ON payslips
        FOR INSERT
        WITH CHECK ({ROLES} && {FINANCE})
        """
    )
    # The status transition (ticket 46) is finance's too, and the trigger is what bounds
    # *which* columns it can touch.
    op.execute(
        f"""
        CREATE POLICY {PAYSLIP_UPDATE} ON payslips
        FOR UPDATE
        USING ({ROLES} && {FINANCE})
        WITH CHECK ({ROLES} && {FINANCE})
        """
    )

    op.execute("ALTER TABLE payslip_batches ENABLE ROW LEVEL SECURITY")
    # A batch's own list is finance's: it names who is missing a payslip, which is
    # payroll material rather than something its subjects read. There is deliberately no
    # owner clause here — an employee reaching their batch would learn which colleagues
    # were missing one.
    op.execute(
        f"""
        CREATE POLICY {BATCH_READ} ON payslip_batches
        FOR SELECT
        USING ({ROLES} && {FINANCE})
        """
    )
    op.execute(
        f"""
        CREATE POLICY {BATCH_INSERT} ON payslip_batches
        FOR INSERT
        WITH CHECK ({ROLES} && {FINANCE})
        """
    )
    # **An UPDATE policy, and without it the batch's own counts could not be written.** A
    # batch row is created before its files — `payslips.batch_id` is a foreign key — and its
    # counts are filled in after them, which is an `UPDATE`. With row-level security enabled
    # and no `FOR UPDATE` policy, PostgreSQL denies *every* update to the table, silently:
    # the statement reports zero rows rather than raising, so the symptom is a batch that
    # says it filed a month with nothing in it. Four columns move (the counts and the two
    # lists) and the uploader, the month and `created_at` do not; the policy is the same
    # finance-only rule as the other two.
    op.execute(
        f"""
        CREATE POLICY {BATCH_UPDATE} ON payslip_batches
        FOR UPDATE
        USING ({ROLES} && {FINANCE})
        WITH CHECK ({ROLES} && {FINANCE})
        """
    )


def downgrade() -> None:
    """Drop the two tables and everything with them.

    The tables *are* the feature: a batch with no payslips is a list of nothing, and the
    files under the storage root would be orphans of rows that no longer exist. A
    downgrade of ticket 44 is a decision to discard the month's payslips, and it says so
    rather than pretending the files survive it.
    """
    for name in (PAYSLIP_UPDATE, PAYSLIP_INSERT, PAYSLIP_READ):
        op.execute(f"DROP POLICY IF EXISTS {name} ON payslips")
    for name in (BATCH_UPDATE, BATCH_INSERT, BATCH_READ):
        op.execute(f"DROP POLICY IF EXISTS {name} ON payslip_batches")

    op.execute("DROP TRIGGER IF EXISTS trg_payslips_immutable_columns ON payslips")
    op.drop_index("ix_payslips_period_status", table_name="payslips")
    op.drop_index("ix_payslips_period", table_name="payslips")
    op.drop_table("payslips")
    op.drop_index("ix_payslip_batches_period", table_name="payslip_batches")
    op.drop_table("payslip_batches")

    # After the tables, which are the only things that referenced them.
    op.execute("DROP FUNCTION IF EXISTS payslips_immutable_columns()")
    op.execute("DROP FUNCTION IF EXISTS payslip_unmatched_are_entries(jsonb)")
    op.execute("DROP FUNCTION IF EXISTS payslip_entries_are_objects(jsonb)")
