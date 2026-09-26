"""In-memory employee repository for the rule tests.

Mirrors the PostgreSQL adapter's observable behaviour, not its SQL. `load`
deliberately calls `list_assignments` the way the real one does, so a service
test exercises the same call sequence.
"""

from collections.abc import Sequence
from datetime import date
from uuid import UUID, uuid4

from app.domain.employee.models import (
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
from app.domain.org.models import ClearanceLevel, Department


class InMemoryEmployeeRepository:
    def __init__(self, departments: dict[UUID, Department] | None = None) -> None:
        self.employees: dict[UUID, Employee] = {}
        self.private: dict[UUID, EmployeePrivate] = {}
        self.assignments: dict[UUID, Assignment] = {}
        self.positions: dict[UUID, JobPosition] = {}
        self.departments = departments or {}
        self.commits = 0

    # --- seeding helpers ---------------------------------------------------

    def seed_position(
        self,
        department: Department,
        *,
        code: str = "engineer",
        is_active: bool = True,
        is_managerial: bool = False,
    ) -> JobPosition:
        position = JobPosition(
            id=uuid4(),
            code=code,
            title_es=f"{code} es",
            title_en=f"{code} en",
            department_id=department.id,
            is_managerial=is_managerial,
            is_active=is_active,
        )
        self.positions[position.id] = position
        self.departments[department.id] = department
        return position

    def seed_employee(self, *, email: str | None = None, **overrides: object) -> Employee:
        employee = Employee(
            id=uuid4(),
            first_name=overrides.get("first_name", "Ana"),
            last_name=overrides.get("last_name", "Martín"),
            preferred_name=overrides.get("preferred_name"),
            email=email or f"{uuid4().hex[:8]}@empresa.es",
            photo_path=None,
            city=overrides.get("city"),
            country=overrides.get("country"),
            hire_date=overrides.get("hire_date", date(2024, 1, 15)),
            termination_date=overrides.get("termination_date"),
            status=EmploymentStatus(overrides.get("status", EmploymentStatus.ACTIVE)),
        )
        self.employees[employee.id] = employee
        self.private.setdefault(employee.id, EmployeePrivate())
        return employee

    def attach(
        self,
        employee: Employee,
        position: JobPosition,
        *,
        is_primary: bool = True,
        end_date: date | None = None,
        manager_employee_id: UUID | None = None,
    ) -> Assignment:
        department = self.departments[position.department_id]
        assignment = Assignment(
            id=uuid4(),
            employee_id=employee.id,
            department_id=department.id,
            department_code=department.code,
            department_name_es=department.name_es,
            department_name_en=department.name_en,
            job_position_id=position.id,
            job_position_code=position.code,
            job_title_es=position.title_es,
            job_title_en=position.title_en,
            is_primary=is_primary,
            is_part_time=False,
            manager_employee_id=manager_employee_id,
            notification_override_employee_id=None,
            start_date=date(2024, 1, 15),
            end_date=end_date,
        )
        self.assignments[assignment.id] = assignment
        return assignment

    # --- repository protocol ----------------------------------------------

    async def get(self, employee_id: UUID) -> Employee | None:
        return self.employees.get(employee_id)

    async def get_by_email(self, email: str) -> Employee | None:
        return next((e for e in self.employees.values() if e.email == email), None)

    async def get_by_employee_no(self, employee_no: str) -> Employee | None:
        for employee_id, private in self.private.items():
            if private.employee_no == employee_no:
                return self.employees.get(employee_id)
        return None

    async def get_private(self, employee_id: UUID) -> EmployeePrivate:
        return self.private.get(employee_id, EmployeePrivate())

    async def load(
        self, employee_id: UUID, *, include_assignments: bool = True
    ) -> EmployeeRecord | None:
        employee = self.employees.get(employee_id)
        if employee is None:
            return None
        return EmployeeRecord(
            employee=employee,
            private=self.private.get(employee_id, EmployeePrivate()),
            assignments=(
                tuple(await self.list_assignments(employee_id)) if include_assignments else ()
            ),
        )

    async def list_directory(
        self, *, department_ids: frozenset[UUID] | None = None, include_terminated: bool = False
    ) -> list[DirectoryEntry]:
        entries: list[DirectoryEntry] = []
        for employee in self.employees.values():
            if not include_terminated and employee.status is EmploymentStatus.TERMINATED:
                continue
            active = [
                a
                for a in self.assignments.values()
                if a.employee_id == employee.id and a.end_date is None
            ]
            if not active:
                continue
            primary = next((a for a in active if a.is_primary), active[0])
            if department_ids is not None and primary.department_id not in department_ids:
                continue
            entries.append(
                DirectoryEntry(
                    employee_id=employee.id,
                    full_name=employee.full_name,
                    preferred_name=employee.preferred_name,
                    photo_path=employee.photo_path,
                    email=employee.email,
                    job_title_es=primary.job_title_es,
                    job_title_en=primary.job_title_en,
                    department_id=primary.department_id,
                    department_name_es=primary.department_name_es,
                    department_name_en=primary.department_name_en,
                )
            )
        return entries

    async def save(self, data: EmployeeInput) -> Employee:
        employee = Employee(
            id=uuid4(),
            first_name=data.first_name,
            last_name=data.last_name,
            preferred_name=data.preferred_name,
            email=data.email,
            photo_path=data.photo_path,
            city=data.city,
            country=data.country,
            hire_date=data.hire_date,
            termination_date=data.termination_date,
            status=data.status,
        )
        self.employees[employee.id] = employee
        self.private[employee.id] = data.private
        return employee

    async def update(self, employee_id: UUID, patch: EmployeePatch) -> Employee:
        current = self.employees[employee_id]
        changes = patch.changes()
        if "status" in changes:
            changes["status"] = EmploymentStatus(changes["status"])
        updated = Employee(**{**self._as_dict(current), **changes})
        self.employees[employee_id] = updated
        return updated

    async def save_private(self, employee_id: UUID, private: EmployeePrivate) -> EmployeePrivate:
        self.private[employee_id] = private
        return private

    async def list_assignments(
        self, employee_id: UUID, *, on_date: object | None = None
    ) -> list[Assignment]:
        rows = [a for a in self.assignments.values() if a.employee_id == employee_id]
        if isinstance(on_date, date):
            rows = [
                a
                for a in rows
                if a.start_date <= on_date and (a.end_date is None or a.end_date >= on_date)
            ]
        return sorted(rows, key=lambda a: (not a.is_primary, a.start_date))

    async def get_position(self, position_id: UUID) -> JobPosition | None:
        return self.positions.get(position_id)

    async def get_position_title(self, position_id: UUID) -> tuple[str, str] | None:
        position = self.positions.get(position_id)
        return (position.title_es, position.title_en) if position else None

    async def save_assignment(
        self, employee_id: UUID, data: AssignmentInput, *, is_primary: bool
    ) -> Assignment:
        position = self.positions[data.job_position_id]
        department = self.departments[data.department_id]
        assignment = Assignment(
            id=uuid4(),
            employee_id=employee_id,
            department_id=department.id,
            department_code=department.code,
            department_name_es=department.name_es,
            department_name_en=department.name_en,
            job_position_id=position.id,
            job_position_code=position.code,
            job_title_es=position.title_es,
            job_title_en=position.title_en,
            is_primary=is_primary,
            is_part_time=data.is_part_time,
            manager_employee_id=data.manager_employee_id,
            notification_override_employee_id=data.notification_override_employee_id,
            start_date=data.start_date,
            end_date=data.end_date,
        )
        self.assignments[assignment.id] = assignment
        return assignment

    async def end_assignment(self, assignment_id: UUID) -> None:
        current = self.assignments[assignment_id]
        self.assignments[assignment_id] = Assignment(
            **{**self._as_dict(current), "end_date": date(2026, 1, 1)}
        )

    async def set_primary(self, employee_id: UUID, assignment_id: UUID) -> None:
        for assignment_id_, assignment in list(self.assignments.items()):
            if assignment.employee_id != employee_id or assignment.end_date is not None:
                continue
            self.assignments[assignment_id_] = Assignment(
                **{**self._as_dict(assignment), "is_primary": assignment_id_ == assignment_id}
            )

    async def employee_exists(self, employee_id: UUID) -> bool:
        return employee_id in self.employees

    async def commit(self) -> None:
        self.commits += 1

    @staticmethod
    def _as_dict(record: Employee | Assignment) -> dict:
        """Field map for rebuilding a frozen dataclass with one value changed.

        One helper for both types: the sets are disjoint, and a second function
        differing only by which fields it lists would drift from this one.
        """
        employee_fields = (
            "id",
            "first_name",
            "last_name",
            "preferred_name",
            "email",
            "photo_path",
            "city",
            "country",
            "hire_date",
            "termination_date",
            "status",
        )
        assignment_fields = (
            "id",
            "employee_id",
            "department_id",
            "department_code",
            "department_name_es",
            "department_name_en",
            "job_position_id",
            "job_position_code",
            "job_title_es",
            "job_title_en",
            "is_primary",
            "is_part_time",
            "manager_employee_id",
            "notification_override_employee_id",
            "start_date",
            "end_date",
        )
        fields = employee_fields if isinstance(record, Employee) else assignment_fields
        return {field: getattr(record, field) for field in fields}


class InMemoryDepartments:
    """The slice of the organisation module the employee service needs."""

    def __init__(self, departments: dict[UUID, Department]) -> None:
        self._departments = departments

    async def get(self, department_id: UUID) -> Department | None:
        return self._departments.get(department_id)

    async def list_subtree(self, path: str, *, include_self: bool = True) -> Sequence[Department]:
        rows = [
            d
            for d in self._departments.values()
            if d.path == path or d.path.startswith(f"{path}.")
        ]
        if not include_self:
            rows = [d for d in rows if d.path != path]
        return rows


def make_department(code: str, path: str | None = None) -> Department:
    resolved = path or code
    return Department(
        id=uuid4(),
        code=code,
        name_es=f"{code} es",
        name_en=f"{code} en",
        parent_id=None,
        path=resolved,
        depth=resolved.count("."),
        clearance_level=ClearanceLevel.LOW,
        cost_center=None,
        manager_employee_id=None,
        description_es=None,
        description_en=None,
        is_active=True,
    )


__all__ = [
    "InMemoryDepartments",
    "InMemoryEmployeeRepository",
    "make_department",
]
