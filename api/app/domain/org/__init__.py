"""Organisation domain: departments and (later) positions.

Nothing here imports FastAPI or SQLAlchemy. The service depends on a Protocol,
so the rules can be tested without a database and the calling layer can decide
what a domain error becomes over HTTP.
"""

from app.domain.org.errors import OrgErrorCode
from app.domain.org.models import (
    ClearanceLevel,
    Department,
    DepartmentInput,
    DepartmentPatch,
    DepartmentTree,
)
from app.domain.org.repository import DepartmentRepository
from app.domain.org.service import DepartmentService

__all__ = [
    "ClearanceLevel",
    "Department",
    "DepartmentInput",
    "DepartmentPatch",
    "DepartmentRepository",
    "DepartmentService",
    "DepartmentTree",
    "OrgErrorCode",
]
