"""Accounts: the login identity attached to an employee.

One account per employee, enforced by a unique constraint rather than by a
check in application code, because the requirement is one-to-one and a race
between two administrators would otherwise be able to break it.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db_metadata import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("length(username) >= 3", name="ck_users_username_length"),
        # The hash is produced by Argon2id; refusing anything that does not look
        # like one stops a plaintext value or a foreign format being stored here.
        CheckConstraint(
            "password_hash LIKE '$argon2id$%'",
            name="ck_users_password_hash_is_argon2id",
        ),
        CheckConstraint("session_epoch >= 1", name="ck_users_session_epoch"),
        CheckConstraint(
            "clearance_level IN ('low', 'medium', 'high')",
            name="ck_users_clearance_level",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    # One-to-one: the unique constraint is what makes "an employee has at most
    # one account" true regardless of application behaviour.
    employee_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    #: System roles held. Fixed set, small, and read on every request as part of
    #: the permission snapshot, which is why it is a column rather than a join.
    #: The database constrains the values to the known set.
    roles: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[\"employee\"]'::jsonb")
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: Initialised from the primary position's department when the account is
    #: created (DESIGN §10.5) and raisable by hand. The permission snapshot takes
    #: the higher of this and what the person's departments grant: the department
    #: is the authority on what its work may contain, and this is how one person
    #: is raised above it for a specific reason.
    clearance_level: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'low'")
    )

    # Incremented whenever every existing session must stop being valid:
    # deactivation and password reset. Sessions carry the epoch they were issued
    # under, so invalidation is one comparison rather than a key scan.
    session_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    employee = relationship("Employee", lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User {self.username}>"
