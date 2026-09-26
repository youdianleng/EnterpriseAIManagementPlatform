"""Append-only audit records.

The table records *who did what to which entity, with what before and after*.
It is written by an INSERT-only connection (see `app.audit`), so application code
cannot alter or remove a record even by accident.

Ticket 14 adds the compliance query surface and the full action catalogue; this
module exists now because ticket 09 requires three account operations to be
audited before it can claim to be done.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db_metadata import Base


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_entity", "entity_type", "entity_id"),
        Index("ix_audit_log_actor", "actor_user_id"),
        Index("ix_audit_log_occurred_at", "occurred_at"),
        Index("ix_audit_log_action", "action"),
    )

    # A sequence rather than a UUID: audit rows are read in order, and a
    # monotonic id makes "everything after this point" a single comparison.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Nullable: some events (a failed login for an unknown username) have no actor.
    actor_user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    # The roles held *at the time*. Stored as a snapshot so a later role change
    # cannot rewrite history: "was this person allowed to do it then" must stay
    # answerable.
    actor_roles: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    action: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    # user | agent | system — an action the assistant proposed reads differently
    # from one a person took.
    initiated_by: Mapped[str] = mapped_column(String(16), nullable=False, server_default="user")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AuditLog {self.id} {self.action}>"


__all__ = ["AuditLog"]
