"""Employee rules.

Everything structural lives here: uniqueness of email and staff number, which
assignment becomes primary, when a manager reference is valid, and what is
withheld from whom.

The service does not decide who the viewer is — it is handed a `ViewerContext`
and applies `visibility.resolve_visibility`. That keeps "who may read this" in
one place and means ticket 11 only has to change where the context comes from.
"""

from datetime import date
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

#: Aliased because `record` is this module's word for an employee record, and a
#: function of the same name would be shadowed by the first local variable a
#: reader meets.
from app.audit import AuditAction
from app.audit import record as audit_record
from app.domain.employee.models import (
    Assignment,
    AssignmentInput,
    DirectoryEntry,
    EmployeeInput,
    EmployeePatch,
    EmployeePrivate,
    EmployeeRecord,
)
from app.domain.employee.repository import DepartmentLookup, EmployeeRepository
from app.domain.employee.visibility import ViewerContext
from app.domain.errors import DomainError, DomainErrorCode
from app.domain.org.errors import OrgErrorCode


def _private_fields(private: EmployeePrivate) -> set[str]:
    """Which withheld fields carry a value, without carrying the values."""
    return {
        name
        for name in ("address_line", "postal_code", "employee_no", "birth_date",
                     "emergency_contact")
        if getattr(private, name) is not None
    }


class EmployeeService:
    def __init__(
        self,
        repository: EmployeeRepository,
        departments: DepartmentLookup,
        session: AsyncSession | None = None,
    ) -> None:
        self._repository = repository
        self._departments = departments
        # Optional for unit tests of the profile rules; every write path in the
        # application passes one, because every write is audited.
        self._session = session

    async def _audit(
        self,
        action: AuditAction,
        employee_id: UUID,
        *,
        before: dict[str, object] | None = None,
        after: dict[str, object] | None = None,
        reason: str | None = None,
    ) -> None:
        if self._session is None:
            return
        await audit_record(
            self._session,
            action=action,
            entity_type="employee",
            entity_id=employee_id,
            before=before,
            after=after,
            reason=reason,
        )

    # --- reads -------------------------------------------------------------

    async def get_record(self, employee_id: UUID) -> EmployeeRecord:
        record = await self._repository.load(employee_id, include_assignments=True)
        if record is None:
            raise DomainError(
                DomainErrorCode.EMPLOYEE_NOT_FOUND, detail=f"unknown employee {employee_id}"
            )
        return record

    async def list_directory(
        self,
        *,
        include_terminated: bool = False,
    ) -> list[DirectoryEntry]:
        return await self._repository.list_directory(include_terminated=include_terminated)

    async def list_assignments(self, employee_id: UUID) -> list[Assignment]:
        await self.get_record(employee_id)  # 404 before returning an empty list
        return await self._repository.list_assignments(employee_id)

    # --- profile -----------------------------------------------------------

    async def create(self, data: EmployeeInput) -> EmployeeRecord:
        if await self._repository.get_by_email(data.email) is not None:
            raise DomainError(
                DomainErrorCode.EMPLOYEE_EMAIL_TAKEN, detail=f"email {data.email} is in use"
            )
        if data.private.employee_no and (
            await self._repository.get_by_employee_no(data.private.employee_no) is not None
        ):
            raise DomainError(
                DomainErrorCode.EMPLOYEE_NUMBER_TAKEN,
                detail=f"staff number {data.private.employee_no} is in use",
            )
        if data.termination_date and data.termination_date < data.hire_date:
            raise DomainError(
                DomainErrorCode.EMPLOYEE_DATES_INVALID,
                detail=f"termination {data.termination_date} precedes hire {data.hire_date}",
            )

        employee = await self._repository.save(data)
        await self._repository.save_private(employee.id, data.private)
        await self._audit(
            AuditAction.EMPLOYEE_CREATED,
            employee.id,
            after={
                "email": employee.email,
                "hire_date": str(employee.hire_date),
                "status": str(employee.status),
            },
        )
        await self._repository.commit()
        return await self.get_record(employee.id)

    async def update(self, employee_id: UUID, patch: EmployeePatch) -> EmployeeRecord:
        current = await self.get_record(employee_id)
        changes = patch.changes()

        new_email = changes.get("email")
        if isinstance(new_email, str) and new_email != current.employee.email:
            if await self._repository.get_by_email(new_email) is not None:
                raise DomainError(
                    DomainErrorCode.EMPLOYEE_EMAIL_TAKEN, detail=f"email {new_email} is in use"
                )

        hire_date = changes.get("hire_date", current.employee.hire_date)
        termination = changes.get("termination_date", current.employee.termination_date)
        if isinstance(hire_date, date) and isinstance(termination, date):
            if termination < hire_date:
                raise DomainError(
                    DomainErrorCode.EMPLOYEE_DATES_INVALID,
                    detail=f"termination {termination} precedes hire {hire_date}",
                )

        await self._repository.update(employee_id, patch)
        await self._audit(
            AuditAction.EMPLOYEE_UPDATED,
            employee_id,
            # The patch is the statement of intent, so it and what it replaced are
            # both kept: a record that only shows the new value cannot answer
            # "what did this used to say".
            before={name: getattr(current.employee, name) for name in changes},
            after=dict(changes),
        )
        await self._repository.commit()
        return await self.get_record(employee_id)

    async def update_private(
        self, employee_id: UUID, private: EmployeePrivate
    ) -> EmployeeRecord:
        await self.get_record(employee_id)

        if private.employee_no and private.employee_no != (
            await self._repository.get_private(employee_id)
        ).employee_no:
            existing = await self._repository.get_by_employee_no(private.employee_no)
            if existing is not None and existing.id != employee_id:
                raise DomainError(
                    DomainErrorCode.EMPLOYEE_NUMBER_TAKEN,
                    detail=f"staff number {private.employee_no} is in use",
                )

        await self._repository.save_private(employee_id, private)
        await self._audit(
            AuditAction.EMPLOYEE_PRIVATE_UPDATED,
            employee_id,
            # Which fields were written, not their values: an emergency contact
            # and a home address are exactly the kind of thing an audit trail
            # should be able to point at without becoming a second copy of.
            after={"fields": sorted(_private_fields(private))},
        )
        await self._repository.commit()
        return await self.get_record(employee_id)

    # --- assignments -------------------------------------------------------

    async def assign_position(
        self, employee_id: UUID, data: AssignmentInput
    ) -> EmployeeRecord:
        """Attach a position, making it primary when the employee has none.

        The first position is primary by construction, and a caller cannot ask
        for the flag: promoting a different position is an administrative act
        (`set_primary`), which keeps a client from promoting itself.
        """
        await self.get_record(employee_id)
        await self._validate_assignment(employee_id, data)

        existing_active = [
            assignment
            for assignment in await self._repository.list_assignments(employee_id)
            if assignment.end_date is None
        ]
        is_primary = not any(assignment.is_primary for assignment in existing_active)

        await self._repository.save_assignment(employee_id, data, is_primary=is_primary)
        await self._audit(
            AuditAction.ASSIGNMENT_ADDED,
            employee_id,
            after={
                "department_id": str(data.department_id),
                "job_position_id": str(data.job_position_id),
                "is_primary": is_primary,
                "is_part_time": data.is_part_time,
                "start_date": str(data.start_date),
            },
        )
        await self._repository.commit()
        return await self.get_record(employee_id)

    async def end_assignment(self, employee_id: UUID, assignment_id: UUID) -> EmployeeRecord:
        assignments = await self._repository.list_assignments(employee_id)
        target = next((item for item in assignments if item.id == assignment_id), None)
        if target is None:
            raise DomainError(
                DomainErrorCode.EMPLOYEE_ASSIGNMENT_NOT_FOUND,
                detail=f"assignment {assignment_id} does not belong to {employee_id}",
            )

        active = [item for item in assignments if item.end_date is None]
        if target.end_date is None and len(active) <= 1:
            # Ending the only remaining position would leave someone employed
            # with nowhere to be, which every later feature assumes cannot happen.
            raise DomainError(
                DomainErrorCode.EMPLOYEE_LAST_ASSIGNMENT,
                detail="an employee must keep at least one active position",
            )

        await self._repository.end_assignment(assignment_id)

        if target.is_primary:
            # Promote a remaining position, so "the primary one" always resolves.
            remaining = [
                item for item in active if item.id != assignment_id and item.end_date is None
            ]
            if remaining:
                await self._repository.set_primary(employee_id, remaining[0].id)

        await self._audit(
            AuditAction.ASSIGNMENT_ENDED,
            employee_id,
            before={
                "assignment_id": str(target.id),
                "department_id": str(target.department_id),
                "job_position_id": str(target.job_position_id),
                "is_primary": target.is_primary,
            },
        )
        await self._repository.commit()
        return await self.get_record(employee_id)

    async def set_primary(self, employee_id: UUID, assignment_id: UUID) -> EmployeeRecord:
        """Administrative: change which position is the primary one."""
        assignments = await self._repository.list_assignments(employee_id)
        target = next((item for item in assignments if item.id == assignment_id), None)
        if target is None:
            raise DomainError(
                DomainErrorCode.EMPLOYEE_ASSIGNMENT_NOT_FOUND,
                detail=f"assignment {assignment_id} does not belong to {employee_id}",
            )
        if target.end_date is not None:
            raise DomainError(
                DomainErrorCode.EMPLOYEE_ASSIGNMENT_ENDED,
                detail=f"assignment {assignment_id} has already ended",
            )

        await self._repository.set_primary(employee_id, assignment_id)
        await self._audit(
            AuditAction.ASSIGNMENT_PRIMARY_CHANGED,
            employee_id,
            # The primary position decides the approval route, so which one it is
            # before and after is the whole content of the change.
            before={
                "primary_department_id": str(
                    next(
                        (item.department_id for item in assignments if item.is_primary),
                        "",
                    )
                )
            },
            after={"primary_department_id": str(target.department_id)},
        )
        await self._repository.commit()
        return await self.get_record(employee_id)

    # --- internals ---------------------------------------------------------

    async def _validate_assignment(self, employee_id: UUID, data: AssignmentInput) -> None:
        department = await self._departments.get(data.department_id)
        if department is None:
            raise DomainError(
                OrgErrorCode.ORG_DEPARTMENT_NOT_FOUND,
                detail=f"department {data.department_id} does not exist",
            )

        position = await self._repository.get_position(data.job_position_id)
        if position is None:
            raise DomainError(
                DomainErrorCode.EMPLOYEE_POSITION_NOT_FOUND,
                detail=f"position {data.job_position_id} does not exist",
            )
        if not position.is_active:
            raise DomainError(
                DomainErrorCode.EMPLOYEE_POSITION_INACTIVE,
                detail=f"position {position.code} is not active",
            )

        # An approval route that points at nobody is worse than no route at all,
        # so the manager is checked here rather than discovered during an approval.
        if data.manager_employee_id is not None:
            if not await self._repository.employee_exists(data.manager_employee_id):
                raise DomainError(
                    DomainErrorCode.EMPLOYEE_MANAGER_NOT_FOUND,
                    detail=f"manager {data.manager_employee_id} does not exist",
                )
            if data.manager_employee_id == employee_id:
                raise DomainError(
                    DomainErrorCode.EMPLOYEE_MANAGER_NOT_FOUND,
                    detail="an employee cannot be their own approver",
                )

        if data.end_date is not None and data.end_date < data.start_date:
            raise DomainError(
                DomainErrorCode.EMPLOYEE_DATES_INVALID,
                detail=f"end {data.end_date} precedes start {data.start_date}",
            )


async def resolve_viewer_context(
    *,
    employee_id: UUID | None,
    roles: frozenset[str],
    clearance_level: str,
    departments: DepartmentLookup,
    employee_repository: EmployeeRepository,
) -> ViewerContext:
    """Build the viewer context, expanding the viewer's departments downward.

    "A colleague in my department" means the department and everything beneath
    it, so the expansion happens once here instead of inside every comparison.
    Replaced by the real session in ticket 11; the shape does not change.
    """
    if employee_id is None:
        return ViewerContext(
            employee_id=None, roles=roles, clearance_level=clearance_level
        )

    reachable: set[UUID] = set()
    for assignment in await employee_repository.list_assignments(employee_id):
        if assignment.end_date is not None:
            continue
        reachable.add(assignment.department_id)
        department = await departments.get(assignment.department_id)
        path = department.path if department is not None else None
        if path:
            for descendant in await departments.list_subtree(path):
                reachable.add(descendant.id)

    return ViewerContext(
        employee_id=employee_id,
        roles=roles,
        clearance_level=clearance_level,
        department_ids=frozenset(reachable),
    )
