"""Job position catalogue.

A position belongs to a department and is what an assignment points at. The
model already exists (`app.models.employee.JobPosition`); this module adds the
rules for maintaining the catalogue.

Scope note: the role-assignment half of ticket 08 needs a users table, which
arrives with ticket 09. The catalogue half depends only on departments and
employees, so it is built here and the role half follows the users table.
"""

from app.domain.position.errors import PositionErrorCode
from app.domain.position.models import Position, PositionInput, PositionPatch
from app.domain.position.repository import PositionRepository
from app.domain.position.service import PositionService

__all__ = [
    "Position",
    "PositionErrorCode",
    "PositionInput",
    "PositionPatch",
    "PositionRepository",
    "PositionService",
]
