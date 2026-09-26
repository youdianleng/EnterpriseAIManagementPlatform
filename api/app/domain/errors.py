"""Domain error types.

A domain error carries *what went wrong*, never *which HTTP status that is*.
The mapping from code to status lives in `app.core.errors`, so the domain stays
free of transport concerns and the same failure can be surfaced differently by a
different caller (for example a CLI or a background job).
"""

from enum import StrEnum

from app.core.errors import ErrorCode

__all__ = ["DomainError", "DomainErrorCode"]


class DomainErrorCode(StrEnum):
    """Codes owned by the domain layer.

    Department codes live in `app.domain.org.errors` (`OrgErrorCode`), which
    aliases the same wire values. Both are accepted by `DomainError`.
    """

    EMPLOYEE_NOT_FOUND = "ERR_EMP_001"
    EMPLOYEE_EMAIL_TAKEN = "ERR_EMP_002"
    EMPLOYEE_NUMBER_TAKEN = "ERR_EMP_003"
    EMPLOYEE_DATES_INVALID = "ERR_EMP_004"
    EMPLOYEE_POSITION_NOT_FOUND = "ERR_EMP_005"
    EMPLOYEE_POSITION_INACTIVE = "ERR_EMP_006"
    EMPLOYEE_MANAGER_NOT_FOUND = "ERR_EMP_007"
    EMPLOYEE_ASSIGNMENT_NOT_FOUND = "ERR_EMP_008"
    EMPLOYEE_ASSIGNMENT_ENDED = "ERR_EMP_009"
    EMPLOYEE_LAST_ASSIGNMENT = "ERR_EMP_010"


class DomainError(Exception):
    """A rule was violated.

    `detail` is for the log and for an operator reading it; the client only ever
    receives the catalogue message for `code`. `code` may be a
    `DomainErrorCode` or another StrEnum from the same wire catalogue
    (`OrgErrorCode`), which is why it is typed loosely.
    """

    def __init__(self, code: StrEnum, detail: str | None = None) -> None:
        super().__init__(detail or str(code.value))
        self.code = code
        self.detail = detail

    @property
    def http_status(self) -> int:
        """Status this failure maps to at the HTTP edge."""
        from app.core.errors import definition_of

        return definition_of(ErrorCode(self.code.value)).status_code
