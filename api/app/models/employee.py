"""Employee records and position assignments.

Two tables, split along the visibility line the ticket draws:

* `employees` holds what the whole directory may see.
* `employee_private` holds what only the person, HR, finance and compliance may
  see. Splitting it out means "did you remember to hide this field" stops being
  a question every query has to answer correctly — the column is simply not on
  the row the directory reads.
* `employee_assignments` is the many-to-many position history that makes one
  person able to hold several positions across departments.

`employee_private` carries a CHECK constraint over its own column names, which
turns "the system stores no ID numbers, bank details, health or biometric data"
from a code-review convention into something the database refuses.
"""

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db_metadata import Base


class Employee(Base):
    __tablename__ = "employees"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'on_leave', 'terminated')",
            name="ck_employees_status",
        ),
        CheckConstraint(
            "termination_date IS NULL OR termination_date >= hire_date",
            name="ck_employees_termination_after_hire",
        ),
        CheckConstraint("email ~ '^[^@[:space:]]+@[^@[:space:]]+$'", name="ck_employees_email"),
        Index("ix_employees_last_name", "last_name", "first_name"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    last_name: Mapped[str] = mapped_column(String(120), nullable=False)
    preferred_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    email: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    photo_path: Mapped[str | None] = mapped_column(String(400), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    country: Mapped[str | None] = mapped_column(String(80), nullable=True)
    hire_date: Mapped[date] = mapped_column(Date, nullable=False)
    termination_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    private: Mapped["EmployeePrivate | None"] = relationship(
        back_populates="employee", uselist=False, cascade="all, delete-orphan"
    )
    assignments: Mapped[list["EmployeeAssignment"]] = relationship(
        back_populates="employee", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Employee {self.email}>"


class EmployeePrivate(Base):
    """Fields withheld from the directory.

    Permitted columns are exactly: street address, postal code, employee number,
    birth date, emergency contact.

    A database event trigger (migration 0003) rejects any ALTER TABLE that would
    add an ID-number, bank, health or biometric column. It cannot be expressed as
    a CHECK constraint because PostgreSQL refuses subqueries in CHECK expressions.
    `tests/test_employee_schema.py` asserts the same rule against the live schema,
    so the failure surfaces at test time rather than mid-deploy.
    """

    __tablename__ = "employee_private"
    __table_args__ = (
        CheckConstraint(
            "address_line IS NULL OR length(btrim(address_line)) > 0",
            name="ck_employee_private_address_not_blank",
        ),
        # Never ask for the row back. SQLAlchemy adds `RETURNING` to an insert so
        # it can collect server defaults, and PostgreSQL hands the returned row to
        # the table's *select* policy — so a write would fail whenever the writer
        # may not read what it just wrote. The timestamps are not read on any path
        # here, so not asking for them costs nothing.
        {"implicit_returning": False},
    )

    employee_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        primary_key=True,
    )
    address_line: Mapped[str | None] = mapped_column(Text, nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Internal staff number, not a legal identifier.
    employee_no: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True)
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    emergency_contact: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    employee: Mapped[Employee] = relationship(back_populates="private")


class JobPosition(Base):
    """The catalogue of positions. The management UI arrives in ticket 08."""

    __tablename__ = "job_positions"
    __table_args__ = (
        Index("ix_job_positions_department_id", "department_id"),
        # A code is unique within its department among active rows.
        Index(
            "uq_job_positions_active_code",
            "department_id",
            "code",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    title_es: Mapped[str] = mapped_column(String(160), nullable=False)
    title_en: Mapped[str] = mapped_column(String(160), nullable=False)
    department_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    is_managerial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    department: Mapped["Department"] = relationship()  # noqa: F821 - resolved by SQLAlchemy


class EmployeeAssignment(Base):
    """One employee holding one position for a period.

    `manager_employee_id` and `notification_override_employee_id` are plain UUID
    columns rather than foreign keys to `employees`: an assignment may name a
    manager who has since left, and history must keep resolving. The service
    validates existence on write.
    """

    __tablename__ = "employee_assignments"
    __table_args__ = (
        CheckConstraint(
            "end_date IS NULL OR end_date >= start_date",
            name="ck_assignments_end_after_start",
        ),
        # At most one active primary assignment per employee. Rows that have
        # ended keep is_primary so history stays readable.
        Index(
            "uq_assignments_active_primary",
            "employee_id",
            unique=True,
            postgresql_where=text("is_primary AND end_date IS NULL"),
        ),
        Index(
            "ix_assignments_employee_active",
            "employee_id",
            postgresql_where=text("end_date IS NULL"),
        ),
        Index("ix_assignments_department_id", "department_id"),
        Index("ix_assignments_manager", "manager_employee_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    employee_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    department_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    job_position_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("job_positions.id", ondelete="RESTRICT"), nullable=False
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_part_time: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    manager_employee_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    notification_override_employee_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    employee: Mapped[Employee] = relationship(back_populates="assignments")
    department: Mapped["Department"] = relationship()  # noqa: F821
    job_position: Mapped[JobPosition] = relationship()


from app.models.org import Department  # noqa: E402,F401  (resolves the string refs above)

__all__ = [
    "Employee",
    "EmployeeAssignment",
    "EmployeePrivate",
    "JobPosition",
]
