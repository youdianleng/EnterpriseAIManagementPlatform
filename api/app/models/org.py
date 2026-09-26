"""Organisation structure models.

`departments` is the hierarchy every permission decision walks ("this department
and all of its descendants"). Two columns carry that weight:

* `path` is an ltree materialised path. Ancestry becomes one indexed comparison
  (`path <@ 'company.engineering'`) instead of a recursive query, which matters
  because the permission kernel asks this question on nearly every request.
* `depth` is denormalised from `path` so the four-level limit can be enforced
  without parsing the path in application code.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db_metadata import Base
from app.models.types import Ltree


class Department(Base):
    __tablename__ = "departments"
    __table_args__ = (
        CheckConstraint("depth >= 0", name="ck_departments_depth_non_negative"),
        CheckConstraint(
            "clearance_level IN ('low', 'medium', 'high')",
            name="ck_departments_clearance_level",
        ),
        # Ancestry lookups (`path <@ 'company.engineering'`) are the hot path for
        # every permission decision, so the path needs its own index.
        Index("ix_departments_path", "path"),
        Index("ix_departments_parent_id", "parent_id"),
        # A code may be reused once its department is deactivated, but never twice
        # among active siblings.
        Index(
            "uq_departments_active_code",
            "parent_id",
            "code",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    # Unique per parent among active rows, not globally: see __table_args__.
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name_es: Mapped[str] = mapped_column(String(160), nullable=False)
    name_en: Mapped[str] = mapped_column(String(160), nullable=False)
    parent_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("departments.id", ondelete="RESTRICT"), nullable=True
    )
    # Populated by the service, never by a client: the path is derived state.
    path: Mapped[str] = mapped_column(Ltree, nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clearance_level: Mapped[str] = mapped_column(String(16), nullable=False, default="low")
    manager_employee_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )  # FK added with the employees table (ticket 07)
    cost_center: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description_es: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    parent: Mapped["Department | None"] = relationship(
        back_populates="children", remote_side=[id]
    )
    children: Mapped[list["Department"]] = relationship(
        back_populates="parent", order_by="Department.code"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Department {self.code} path={self.path}>"
