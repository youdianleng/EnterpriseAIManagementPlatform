"""Approval domain: the state machine every kind of request shares.

Four operations — `submit`, `decide`, `withdraw`, `state_of` — and no knowledge of
what they move. "Approved but not yet in force" is deliberately absent; the module
that owns a document decides when its approval takes effect.
"""

from app.domain.approval.errors import ApprovalErrorCode
from app.domain.approval.models import (
    OPEN_STATUSES,
    OUTCOME_OF,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalState,
    ApprovalStatus,
    DecisionKind,
    PrimaryPosition,
    StepStatus,
    SubmitContext,
)
from app.domain.approval.repository import ApprovalRepository
from app.domain.approval.service import ApprovalService

__all__ = [
    "OPEN_STATUSES",
    "OUTCOME_OF",
    "ApprovalDecision",
    "ApprovalErrorCode",
    "ApprovalRepository",
    "ApprovalRequest",
    "ApprovalService",
    "ApprovalState",
    "ApprovalStatus",
    "DecisionKind",
    "PrimaryPosition",
    "StepStatus",
    "SubmitContext",
]
