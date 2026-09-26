"""Employee endpoints: profiles, position assignments and the directory.

Serialisation goes through the visibility projection, so a withheld field is
absent from the payload rather than present-and-null. The directory deliberately
returns a different shape from the profile, because it answers a different
question: who is this and how do I reach them.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    current_principal,
    db_session,
    require,
)
from app.api.v1.schemas.employee import (
    AssignmentCreate,
    AssignmentRead,
    DirectoryEntryRead,
    EmergencyContact,
    EmployeeCreate,
    EmployeePrivateIn,
    EmployeePrivateRead,
    EmployeeRead,
    EmployeeUpdate,
)
from app.domain.access import (
    Action,
    Principal,
    ResourceKind,
    to_viewer_context,
)
from app.domain.employee.models import (
    Assignment,
    AssignmentInput,
    Employee,
    EmployeeInput,
    EmployeePatch,
    EmployeePrivate,
    EmployeeRecord,
)
from app.domain.employee.service import EmployeeService
from app.domain.employee.visibility import (
    Projection,
    project_directory_row,
    project_private,
    resolve_visibility,
)
from app.repositories.employee import PostgresEmployeeRepository
from app.repositories.org import PostgresDepartmentRepository

router = APIRouter(tags=["employees"])

guard_read_employee = require(Action.EMPLOYEE_READ, ResourceKind.EMPLOYEE)
guard_read_directory = require(Action.EMPLOYEE_DIRECTORY, ResourceKind.EMPLOYEE)
guard_manage_employees = require(Action.EMPLOYEE_MANAGE, ResourceKind.EMPLOYEE)


def _service(session: AsyncSession) -> EmployeeService:
    return EmployeeService(
        repository=PostgresEmployeeRepository(session),
        departments=PostgresDepartmentRepository(session),
    )


def _assignment_read(assignment: Assignment, fallback_approver: UUID | None) -> AssignmentRead:
    """Expose the approver that would actually be used.

    The explicit manager wins; otherwise the department's manager is the
    fallback. Resolving it here means a client never has to know the rule.
    """
    return AssignmentRead(
        id=assignment.id,
        department_id=assignment.department_id,
        department_code=assignment.department_code,
        department_name_es=assignment.department_name_es,
        department_name_en=assignment.department_name_en,
        job_position_id=assignment.job_position_id,
        job_position_code=assignment.job_position_code,
        job_title_es=assignment.job_title_es,
        job_title_en=assignment.job_title_en,
        is_primary=assignment.is_primary,
        is_part_time=assignment.is_part_time,
        manager_employee_id=assignment.manager_employee_id,
        notification_override_employee_id=assignment.notification_override_employee_id,
        effective_approver_employee_id=assignment.manager_employee_id or fallback_approver,
        start_date=assignment.start_date,
        end_date=assignment.end_date,
    )


def _profile(
    projection: Projection,
    record: EmployeeRecord,
    *,
    approvers: dict[UUID, UUID | None],
    include_assignments: bool,
) -> EmployeeRead:
    employee: Employee = projection.employee
    payload: dict[str, object] = {
        "id": employee.id,
        "first_name": employee.first_name,
        "last_name": employee.last_name,
        "preferred_name": employee.preferred_name,
        "photo_path": employee.photo_path,
        "status": employee.status,
        "visibility": projection.visibility,
        "can_manage": projection.can_manage,
    }

    if projection.allows("email"):
        payload["email"] = employee.email
    if projection.allows("city"):
        payload["city"] = employee.city
        payload["country"] = employee.country
    if projection.allows("hire_date"):
        payload["hire_date"] = employee.hire_date
        payload["termination_date"] = employee.termination_date

    visible_private = project_private(projection)
    if visible_private:
        contact = visible_private.get("emergency_contact")
        payload["private"] = EmployeePrivateRead(
            address_line=visible_private.get("address_line"),
            postal_code=visible_private.get("postal_code"),
            employee_no=visible_private.get("employee_no"),
            birth_date=visible_private.get("birth_date"),
            emergency_contact=EmergencyContact(**contact) if contact else None,
        )

    if include_assignments and projection.allows("assignments"):
        payload["assignments"] = [
            _assignment_read(assignment, approvers.get(assignment.department_id))
            for assignment in record.assignments
        ]

    return EmployeeRead(**payload)


async def _approver_map(session: AsyncSession, record: EmployeeRecord) -> dict[UUID, UUID | None]:
    """Department manager per assignment's department, used as the fallback."""
    departments = PostgresDepartmentRepository(session)
    approvers: dict[UUID, UUID | None] = {}
    for assignment in record.assignments:
        if assignment.department_id in approvers:
            continue
        department = await departments.get(assignment.department_id)
        approvers[assignment.department_id] = (
            getattr(department, "manager_employee_id", None) if department else None
        )
    return approvers


@router.get(
    "/employees/directory",
    response_model=list[DirectoryEntryRead],
    # Without this, a field the projection dropped comes back as null: the
    # response model has the attribute and serialises its default.
    response_model_exclude_none=True,
    summary="Contact list",
    dependencies=[Depends(guard_read_directory)],
)
async def read_directory(
    include_terminated: bool = Query(default=False),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> list[DirectoryEntryRead]:
    """Name, position and — for colleagues — email. Nothing else.

    Everyone in the organisation may read this list; the projection decides which
    rows carry an email address. This is the list view; the profile endpoint
    below answers the different question of what one record contains.
    """
    entries = await _service(session).list_directory(include_terminated=include_terminated)
    return [
        DirectoryEntryRead(**project_directory_row(to_viewer_context(principal), entry))  # type: ignore[arg-type]
        for entry in entries
    ]


@router.post(
    "/employees",
    response_model=EmployeeRead, response_model_exclude_none=True,
    status_code=201,
    summary="Create an employee record",
    dependencies=[Depends(guard_manage_employees)],
)
async def create_employee(
    payload: EmployeeCreate,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> EmployeeRead:
    private = payload.private or EmployeePrivateIn()
    record = await _service(session).create(
        EmployeeInput(
            first_name=payload.first_name,
            last_name=payload.last_name,
            email=str(payload.email),
            hire_date=payload.hire_date,
            preferred_name=payload.preferred_name,
            photo_path=payload.photo_path,
            city=payload.city,
            country=payload.country,
            status=payload.status,
            termination_date=payload.termination_date,
            private=EmployeePrivate(
                address_line=private.address_line,
                postal_code=private.postal_code,
                employee_no=private.employee_no,
                birth_date=private.birth_date,
                emergency_contact=(
                    private.emergency_contact.model_dump() if private.emergency_contact else None
                ),
            ),
        )
    )
    return _profile(
        resolve_visibility(to_viewer_context(principal), record),
        record,
        approvers=await _approver_map(session, record),
        include_assignments=True,
    )


@router.get(
    "/employees/me",
    response_model=EmployeeRead,
    response_model_exclude_none=True,
    summary="My own profile",
)
async def read_my_profile(
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> EmployeeRead:
    """Always the full record for the caller.

    Takes no identifier, so there is nothing to tamper with: the record read is
    the one belonging to the authenticated principal. A request without a valid
    session never reaches here, because the principal dependency refuses first.
    """
    record = await _service(session).get_record(principal.employee_id)
    return _profile(
        resolve_visibility(to_viewer_context(principal), record),
        record,
        approvers=await _approver_map(session, record),
        include_assignments=True,
    )


@router.get(
    "/employees/{employee_id}",
    response_model=EmployeeRead,
    response_model_exclude_none=True,
    summary="Read a profile",
    dependencies=[Depends(guard_read_employee)],
)
async def read_employee(
    employee_id: UUID,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> EmployeeRead:
    record = await _service(session).get_record(employee_id)
    return _profile(
        resolve_visibility(to_viewer_context(principal), record),
        record,
        approvers=await _approver_map(session, record),
        include_assignments=True,
    )


@router.patch(
    "/employees/{employee_id}",
    response_model=EmployeeRead, response_model_exclude_none=True,
    summary="Update a profile",
    dependencies=[Depends(guard_manage_employees)],
)
async def update_employee(
    employee_id: UUID,
    payload: EmployeeUpdate,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> EmployeeRead:
    patch = EmployeePatch(**payload.model_dump(exclude_unset=True))
    record = await _service(session).update(employee_id, patch)
    return _profile(
        resolve_visibility(to_viewer_context(principal), record),
        record,
        approvers=await _approver_map(session, record),
        include_assignments=True,
    )


@router.put(
    "/employees/{employee_id}/private",
    response_model=EmployeeRead, response_model_exclude_none=True,
    summary="Update withheld details",
    dependencies=[Depends(guard_manage_employees)],
)
async def update_employee_private(
    employee_id: UUID,
    payload: EmployeePrivateIn,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> EmployeeRead:
    record = await _service(session).update_private(
        employee_id,
        EmployeePrivate(
            address_line=payload.address_line,
            postal_code=payload.postal_code,
            employee_no=payload.employee_no,
            birth_date=payload.birth_date,
            emergency_contact=(
                payload.emergency_contact.model_dump() if payload.emergency_contact else None
            ),
        ),
    )
    return _profile(
        resolve_visibility(to_viewer_context(principal), record),
        record,
        approvers=await _approver_map(session, record),
        include_assignments=True,
    )


@router.post(
    "/employees/{employee_id}/assignments",
    response_model=EmployeeRead, response_model_exclude_none=True,
    status_code=201,
    summary="Attach a position",
    dependencies=[Depends(guard_manage_employees)],
)
async def add_assignment(
    employee_id: UUID,
    payload: AssignmentCreate,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> EmployeeRead:
    record = await _service(session).assign_position(
        employee_id,
        AssignmentInput(
            department_id=payload.department_id,
            job_position_id=payload.job_position_id,
            start_date=payload.start_date,
            is_part_time=payload.is_part_time,
            manager_employee_id=payload.manager_employee_id,
            notification_override_employee_id=payload.notification_override_employee_id,
            end_date=payload.end_date,
        ),
    )
    return _profile(
        resolve_visibility(to_viewer_context(principal), record),
        record,
        approvers=await _approver_map(session, record),
        include_assignments=True,
    )


@router.delete(
    "/employees/{employee_id}/assignments/{assignment_id}",
    response_model=EmployeeRead, response_model_exclude_none=True,
    summary="End a position assignment",
    dependencies=[Depends(guard_manage_employees)],
)
async def end_assignment(
    employee_id: UUID,
    assignment_id: UUID,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> EmployeeRead:
    record = await _service(session).end_assignment(employee_id, assignment_id)
    return _profile(
        resolve_visibility(to_viewer_context(principal), record),
        record,
        approvers=await _approver_map(session, record),
        include_assignments=True,
    )


@router.put(
    "/employees/{employee_id}/assignments/{assignment_id}/primary",
    response_model=EmployeeRead, response_model_exclude_none=True,
    summary="Change the primary position",
    dependencies=[Depends(guard_manage_employees)],
)
async def set_primary_assignment(
    employee_id: UUID,
    assignment_id: UUID,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> EmployeeRead:
    """Administrative only — this is what drives approval routing."""
    record = await _service(session).set_primary(employee_id, assignment_id)
    return _profile(
        resolve_visibility(to_viewer_context(principal), record),
        record,
        approvers=await _approver_map(session, record),
        include_assignments=True,
    )
