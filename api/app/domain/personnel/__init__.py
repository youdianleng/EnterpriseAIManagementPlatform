"""Personnel change domain: 入转调离 as one document.

Five change types — join, transfer, promotion, salary, termination — over one
shape: an effective date plus a list of `(field, before, after)` changes. Approval
and application are separate events here, and the applier is the only code that
turns an approved document into employee facts.
"""

from app.domain.personnel.errors import PersonnelErrorCode
from app.domain.personnel.models import (
    ApplyFailure,
    ApplyReport,
    ChangeInput,
    ChangeQuery,
    ChangeState,
    ChangeStatus,
    ChangeType,
    FieldChange,
    PersonnelChange,
    PersonnelChangeView,
    parse_changes,
    state_of_change,
)
from app.domain.personnel.repository import PersonnelChangeRepository
from app.domain.personnel.service import ENTITY_TYPE, PersonnelChangeService

__all__ = [
    "ENTITY_TYPE",
    "ApplyFailure",
    "ApplyReport",
    "ChangeInput",
    "ChangeQuery",
    "ChangeState",
    "ChangeStatus",
    "ChangeType",
    "FieldChange",
    "PersonnelChange",
    "PersonnelChangeRepository",
    "PersonnelChangeService",
    "PersonnelChangeView",
    "PersonnelErrorCode",
    "parse_changes",
    "state_of_change",
]
