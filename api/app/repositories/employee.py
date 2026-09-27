"""PostgreSQL implementation of the employee repository.

Reads never join `employee_private` unless the caller asked for the whole
record, which is what makes "the directory cannot leak an address" a property of
the query rather than of the serialiser.
"""

from datetime import date
from uuid import UUID

from sqlalchemy import delete, func, select, text, true, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from app.domain.employee.approver_gap import ApproverGap
from app.domain.employee.models import (
    TERMINATED_STATUS,
    Assignment,
    AssignmentInput,
    DirectoryEntry,
    Employee,
    EmployeeInput,
    EmployeePatch,
    EmployeePrivate,
    EmployeeRecord,
    EmploymentStatus,
    JobPosition,
)
from app.models.employee import Employee as EmployeeRow
from app.models.employee import EmployeeAssignment as AssignmentRow
from app.models.employee import EmployeePrivate as PrivateRow
from app.models.employee import JobPosition as PositionRow
from app.models.org import Department as DepartmentRow


def _to_employee(row: EmployeeRow) -> Employee:
    return Employee(
        id=row.id,
        first_name=row.first_name,
        last_name=row.last_name,
        preferred_name=row.preferred_name,
        email=row.email,
        photo_path=row.photo_path,
        city=row.city,
        country=row.country,
        hire_date=row.hire_date,
        termination_date=row.termination_date,
        status=EmploymentStatus(row.status),
    )


def _to_private(row: PrivateRow | None) -> EmployeePrivate:
    if row is None:
        return EmployeePrivate()
    return EmployeePrivate(
        address_line=row.address_line,
        postal_code=row.postal_code,
        employee_no=row.employee_no,
        birth_date=row.birth_date,
        emergency_contact=row.emergency_contact,
    )


def _to_assignment(
    row: AssignmentRow, department: DepartmentRow, position: PositionRow
) -> Assignment:
    return Assignment(
        id=row.id,
        employee_id=row.employee_id,
        department_id=row.department_id,
        department_code=department.code,
        department_name_es=department.name_es,
        department_name_en=department.name_en,
        job_position_id=row.job_position_id,
        job_position_code=position.code,
        job_title_es=position.title_es,
        job_title_en=position.title_en,
        is_primary=row.is_primary,
        is_part_time=row.is_part_time,
        manager_employee_id=row.manager_employee_id,
        notification_override_employee_id=row.notification_override_employee_id,
        start_date=row.start_date,
        end_date=row.end_date,
    )


class PostgresEmployeeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- reads -------------------------------------------------------------

    async def get(self, employee_id: UUID) -> Employee | None:
        row = await self._session.get(EmployeeRow, employee_id)
        return _to_employee(row) if row else None

    async def get_by_email(self, email: str) -> Employee | None:
        row = await self._session.scalar(select(EmployeeRow).where(EmployeeRow.email == email))
        return _to_employee(row) if row else None

    async def get_by_employee_no(self, employee_no: str) -> Employee | None:
        row = await self._session.scalar(
            select(EmployeeRow)
            .join(PrivateRow, PrivateRow.employee_id == EmployeeRow.id)
            .where(PrivateRow.employee_no == employee_no)
        )
        return _to_employee(row) if row else None

    async def get_private(self, employee_id: UUID) -> EmployeePrivate:
        row = await self._session.get(PrivateRow, employee_id)
        return _to_private(row)

    async def load(
        self, employee_id: UUID, *, include_assignments: bool = True
    ) -> EmployeeRecord | None:
        row = await self._session.scalar(
            select(EmployeeRow)
            .where(EmployeeRow.id == employee_id)
            .options(selectinload(EmployeeRow.private))
        )
        if row is None:
            return None
        assignments = (
            tuple(await self.list_assignments(employee_id)) if include_assignments else ()
        )
        return EmployeeRecord(
            employee=_to_employee(row),
            private=_to_private(row.private),
            assignments=assignments,
        )

    async def list_directory(
        self,
        *,
        department_ids: frozenset[UUID] | None = None,
        include_terminated: bool = False,
    ) -> list[DirectoryEntry]:
        """One row per employee, showing their primary (or first active) position.

        A leaver is out of the list **by an explicit condition**, not by the
        accident that their assignments happen to have ended: if the applier or a
        migration ever left one active, the default read must still not show them.
        `include_terminated` is the one caller that asks for them — the
        administrative view that has to be able to look somebody up after they
        have gone (`docs/DESIGN.md` §7.6).
        """
        statement = (
            select(
                EmployeeRow.id,
                EmployeeRow.first_name,
                EmployeeRow.last_name,
                EmployeeRow.preferred_name,
                EmployeeRow.photo_path,
                EmployeeRow.email,
                EmployeeRow.status,
                DepartmentRow.id,
                DepartmentRow.name_es,
                DepartmentRow.name_en,
                PositionRow.title_es,
                PositionRow.title_en,
                AssignmentRow.is_primary,
                AssignmentRow.start_date,
            )
            .join(AssignmentRow, AssignmentRow.employee_id == EmployeeRow.id)
            .join(DepartmentRow, DepartmentRow.id == AssignmentRow.department_id)
            .join(PositionRow, PositionRow.id == AssignmentRow.job_position_id)
            .where(AssignmentRow.end_date.is_(None))
        )
        if not include_terminated:
            statement = statement.where(EmployeeRow.status != TERMINATED_STATUS)
        if department_ids is not None:
            if not department_ids:
                return []
            statement = statement.where(AssignmentRow.department_id.in_(department_ids))

        # Primary first, then the earliest start, so one employee yields one row.
        statement = statement.order_by(
            EmployeeRow.last_name,
            EmployeeRow.first_name,
            AssignmentRow.is_primary.desc(),
            AssignmentRow.start_date,
        )

        entries: dict[UUID, DirectoryEntry] = {}
        for row in (await self._session.execute(statement)).all():
            if row[0] in entries:
                continue
            entries[row[0]] = DirectoryEntry(
                employee_id=row[0],
                full_name=f"{row[1]} {row[2]}",
                preferred_name=row[3],
                photo_path=row[4],
                email=row[5],
                department_id=row[7],
                department_name_es=row[8],
                department_name_en=row[9],
                job_title_es=row[10],
                job_title_en=row[11],
            )
        return list(entries.values())

    async def list_assignments(
        self, employee_id: UUID, *, on_date: object | None = None
    ) -> list[Assignment]:
        statement = (
            select(AssignmentRow, DepartmentRow, PositionRow)
            .join(DepartmentRow, DepartmentRow.id == AssignmentRow.department_id)
            .join(PositionRow, PositionRow.id == AssignmentRow.job_position_id)
            .where(AssignmentRow.employee_id == employee_id)
            .order_by(AssignmentRow.is_primary.desc(), AssignmentRow.start_date)
        )
        if isinstance(on_date, date):
            statement = statement.where(
                AssignmentRow.start_date <= on_date,
                (AssignmentRow.end_date.is_(None)) | (AssignmentRow.end_date >= on_date),
            )
        rows = (await self._session.execute(statement)).all()
        return [_to_assignment(*row) for row in rows]

    async def get_position(self, position_id: UUID) -> JobPosition | None:
        row = await self._session.get(PositionRow, position_id)
        if row is None:
            return None
        return JobPosition(
            id=row.id,
            code=row.code,
            title_es=row.title_es,
            title_en=row.title_en,
            department_id=row.department_id,
            is_managerial=row.is_managerial,
            is_active=row.is_active,
        )

    async def get_position_title(self, position_id: UUID) -> tuple[str, str] | None:
        row = await self._session.execute(
            select(PositionRow.title_es, PositionRow.title_en).where(
                PositionRow.id == position_id
            )
        )
        found = row.first()
        return (found[0], found[1]) if found else None

    async def employee_exists(self, employee_id: UUID) -> bool:
        return bool(
            await self._session.scalar(
                select(func.count()).select_from(EmployeeRow).where(EmployeeRow.id == employee_id)
            )
        )

    async def terminated_approvers(self) -> list[ApproverGap]:
        """Every active employee whose approval route resolves to a leaver.

        **The route is the engine's rule, written once more in SQL**: the manager
        on the *primary* active assignment, and the department's manager when the
        position names nobody (`ApprovalService._resolve_level_one`, DESIGN
        §3.4/D12). It is written again here rather than asked of the engine because
        the question is the other way round — not "who approves this person" but
        "everybody now pointing at somebody who has left" — and asking per employee
        would be a hundred round trips for one page of HR's work. `LATERAL` with
        `LIMIT 1` is what makes "the primary one" mean the same row the engine
        reads: an employee with a second, non-primary assignment is not thereby
        given a second approver.

        Both halves are constrained. The *employee* is active and holds a position,
        because somebody already gone is a record being kept rather than a route to
        repair, and an employee with no position has no route at all — that is
        `ERR_APR_003`'s question and it predates this ticket. The *approver* is
        terminated, which is what makes the route dead: a terminated account cannot
        sign in, so nobody can decide the first level.
        """
        approver = aliased(EmployeeRow)
        primary = (
            select(
                AssignmentRow.department_id,
                AssignmentRow.manager_employee_id,
            )
            .where(
                AssignmentRow.employee_id == EmployeeRow.id,
                AssignmentRow.end_date.is_(None),
            )
            .order_by(AssignmentRow.is_primary.desc(), AssignmentRow.start_date)
            .limit(1)
            .lateral("primary_assignment")
        )
        statement = (
            select(
                EmployeeRow.id,
                EmployeeRow.first_name,
                EmployeeRow.last_name,
                approver.id,
                approver.first_name,
                approver.last_name,
                DepartmentRow.code,
                DepartmentRow.name_es,
                primary.c.manager_employee_id.is_not(None).label("named_on_position"),
            )
            # Explicit, because the LATERAL below introduces a second FROM: without
            # it SQLAlchemy cannot tell which one the department joins from.
            .select_from(EmployeeRow)
            .join(primary, true())
            .join(DepartmentRow, DepartmentRow.id == primary.c.department_id)
            .join(
                approver,
                approver.id
                == func.coalesce(primary.c.manager_employee_id, DepartmentRow.manager_employee_id),
            )
            .where(
                EmployeeRow.status != TERMINATED_STATUS,
                approver.status == TERMINATED_STATUS,
            )
            .order_by(DepartmentRow.code, EmployeeRow.last_name, EmployeeRow.first_name)
        )
        rows = (await self._session.execute(statement)).all()
        return [
            ApproverGap(
                employee_id=row[0],
                employee_name=f"{row[1]} {row[2]}",
                approver_employee_id=row[3],
                approver_name=f"{row[4]} {row[5]}",
                department_code=row[6],
                department_name=row[7],
                named_on_position=row[8],
            )
            for row in rows
        ]

    # --- writes ------------------------------------------------------------

    async def save(self, data: EmployeeInput) -> Employee:
        row = EmployeeRow(
            first_name=data.first_name,
            last_name=data.last_name,
            preferred_name=data.preferred_name,
            email=data.email,
            photo_path=data.photo_path,
            city=data.city,
            country=data.country,
            hire_date=data.hire_date,
            termination_date=data.termination_date,
            status=data.status.value,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_employee(row)

    async def update(self, employee_id: UUID, patch: EmployeePatch) -> Employee:
        changes = patch.changes()
        if "status" in changes:
            changes["status"] = EmploymentStatus(changes["status"]).value
        if changes:
            row = await self._session.get(EmployeeRow, employee_id)
            assert row is not None
            for name, value in changes.items():
                setattr(row, name, value)
            await self._session.flush()
        row = await self._session.get(EmployeeRow, employee_id)
        assert row is not None
        return _to_employee(row)

    async def save_private(self, employee_id: UUID, private: EmployeePrivate) -> EmployeePrivate:
        """Write the withheld details without asking for the row back.

        An update is attempted first and an insert is used when there was nothing
        to update, rather than the upsert this started as. `INSERT ... ON CONFLICT
        DO UPDATE` has to read the conflicting row to decide whether there is a
        conflict, and `RETURNING` hands the new row to the select policy; both
        make a write depend on a read. That dependency is avoidable here, and
        avoiding it is what keeps this method's behaviour independent of how the
        read policy is written.

        What it returns is what it wrote, which is all the caller needed.
        """
        values: dict[str, object] = {
            "address_line": private.address_line,
            "postal_code": private.postal_code,
            "employee_no": private.employee_no,
            "birth_date": private.birth_date,
            "emergency_contact": private.emergency_contact,
        }
        updated = await self._session.execute(
            update(PrivateRow)
            .where(PrivateRow.employee_id == employee_id)
            .values(**values, updated_at=func.now())
            .execution_options(synchronize_session=False)
        )
        if updated.rowcount == 0:
            await self._session.execute(
                pg_insert(PrivateRow).values(employee_id=employee_id, **values)
            )
        return private

    async def save_assignment(
        self, employee_id: UUID, data: AssignmentInput, *, is_primary: bool
    ) -> Assignment:
        row = AssignmentRow(
            employee_id=employee_id,
            department_id=data.department_id,
            job_position_id=data.job_position_id,
            is_primary=is_primary,
            is_part_time=data.is_part_time,
            manager_employee_id=data.manager_employee_id,
            notification_override_employee_id=data.notification_override_employee_id,
            start_date=data.start_date,
            end_date=data.end_date,
        )
        self._session.add(row)
        await self._session.flush()
        return await self._assignment(row.id)

    async def _assignment(self, assignment_id: UUID) -> Assignment:
        row = (
            await self._session.execute(
                select(AssignmentRow, DepartmentRow, PositionRow)
                .join(DepartmentRow, DepartmentRow.id == AssignmentRow.department_id)
                .join(PositionRow, PositionRow.id == AssignmentRow.job_position_id)
                .where(AssignmentRow.id == assignment_id)
            )
        ).one()
        return _to_assignment(*row)

    async def end_assignment(self, assignment_id: UUID, *, on_date: date | None = None) -> None:
        row = await self._session.get(AssignmentRow, assignment_id)
        assert row is not None
        # A personnel change states the day it takes effect; everything else ends a
        # position now. Clamped to the start date because the table refuses an
        # assignment that ends before it began, and a same-day move is legitimate.
        row.end_date = max(row.start_date, on_date) if on_date else date.today()
        await self._session.flush()

    async def set_primary(self, employee_id: UUID, assignment_id: UUID) -> None:
        # Clear first: the partial unique index allows only one active primary.
        await self._session.execute(
            text(
                """
                UPDATE employee_assignments
                SET is_primary = false, updated_at = now()
                WHERE employee_id = :employee_id AND is_primary AND end_date IS NULL
                """
            ),
            {"employee_id": employee_id},
        )
        await self._session.execute(
            text(
                """
                UPDATE employee_assignments
                SET is_primary = true, updated_at = now()
                WHERE id = :assignment_id
                """
            ),
            {"assignment_id": assignment_id},
        )
        await self._session.flush()

    async def delete(self, employee_id: UUID) -> None:
        await self._session.execute(delete(EmployeeRow).where(EmployeeRow.id == employee_id))
        await self._session.flush()

    async def commit(self) -> None:
        await self._session.commit()


__all__ = ["PostgresEmployeeRepository"]
