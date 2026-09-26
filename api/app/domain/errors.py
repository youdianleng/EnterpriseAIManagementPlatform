"""Domain error types.

A domain error carries *what went wrong*, never *which HTTP status that is*.
The mapping from code to status lives in `app.core.errors`, so the domain stays
free of transport concerns and the same failure can be surfaced differently by a
different caller (for example a CLI or a background job).
"""

from enum import StrEnum

from app.core.errors import ErrorCode


class DomainErrorCode(StrEnum):
    DEPARTMENT_NOT_FOUND = "ERR_ORG_001"
    DEPARTMENT_CODE_TAKEN = "ERR_ORG_002"
    DEPARTMENT_NOT_EMPTY = "ERR_ORG_003"
    DEPARTMENT_HAS_CHILDREN = "ERR_ORG_004"
    DEPARTMENT_PARENT_INVALID = "ERR_ORG_005"
    DEPARTMENT_MOVE_INTO_DESCENDANT = "ERR_ORG_006"
    DEPARTMENT_DEPTH_EXCEEDED = "ERR_ORG_007"


class DomainError(Exception):
    """A rule was violated.

    `detail` is for the log and for an operator reading it; the client only ever
    receives the catalogue message for `code`.
    """

    def __init__(self, code: DomainErrorCode, detail: str | None = None) -> None:
        super().__init__(detail or code.value)
        self.code = code
        self.detail = detail

    @property
    def http_status(self) -> int:
        """Status this failure maps to at the HTTP edge."""
        from app.core.errors import definition_of

        return definition_of(ErrorCode(self.code.value)).status_code
