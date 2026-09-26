"""Error catalogue.

Single source of truth for error codes: each code carries the HTTP status and
the message key the client renders. Adding a code is one edit here, and the
tests keep both language catalogues in sync.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class ErrorCode(StrEnum):
    # Request shape.
    VALIDATION_FAILED = "ERR_VALIDATION_001"
    INVALID_REQUEST = "ERR_VALIDATION_002"

    # Authentication and authorisation.
    UNAUTHENTICATED = "ERR_AUTH_001"
    FORBIDDEN = "ERR_AUTH_002"
    ACCOUNT_LOCKED = "ERR_AUTH_003"
    PASSWORD_CHANGE_REQUIRED = "ERR_AUTH_004"

    # Resources.
    NOT_FOUND = "ERR_RESOURCE_001"
    CONFLICT = "ERR_RESOURCE_002"
    RESOURCE_GONE = "ERR_RESOURCE_003"

    # Organisation (departments, positions).
    ORG_DEPARTMENT_NOT_FOUND = "ERR_ORG_001"
    ORG_DEPARTMENT_CODE_TAKEN = "ERR_ORG_002"
    ORG_DEPARTMENT_NOT_EMPTY = "ERR_ORG_003"
    ORG_DEPARTMENT_HAS_CHILDREN = "ERR_ORG_004"
    ORG_DEPARTMENT_PARENT_INVALID = "ERR_ORG_005"
    ORG_DEPARTMENT_MOVE_INTO_DESCENDANT = "ERR_ORG_006"
    ORG_DEPARTMENT_DEPTH_EXCEEDED = "ERR_ORG_007"

    # Cross-cutting.
    INTERNAL_ERROR = "ERR_INTERNAL_001"
    SERVICE_UNAVAILABLE = "ERR_INTERNAL_002"


@dataclass(frozen=True, slots=True)
class ErrorDefinition:
    status_code: int
    message_key: str
    # Safe to show the caller? 5xx details leak internals, so they are logged
    # and omitted from the response body.
    expose_detail: bool = True


ERRORS: Final[dict[ErrorCode, ErrorDefinition]] = {
    ErrorCode.VALIDATION_FAILED: ErrorDefinition(422, "errors.validation_failed"),
    ErrorCode.INVALID_REQUEST: ErrorDefinition(400, "errors.invalid_request"),
    ErrorCode.UNAUTHENTICATED: ErrorDefinition(401, "errors.unauthenticated"),
    ErrorCode.FORBIDDEN: ErrorDefinition(403, "errors.forbidden"),
    ErrorCode.ACCOUNT_LOCKED: ErrorDefinition(423, "errors.account_locked"),
    ErrorCode.PASSWORD_CHANGE_REQUIRED: ErrorDefinition(403, "errors.password_change_required"),
    ErrorCode.NOT_FOUND: ErrorDefinition(404, "errors.not_found"),
    ErrorCode.CONFLICT: ErrorDefinition(409, "errors.conflict"),
    ErrorCode.RESOURCE_GONE: ErrorDefinition(410, "errors.resource_gone"),
    ErrorCode.ORG_DEPARTMENT_NOT_FOUND: ErrorDefinition(404, "errors.department_not_found"),
    ErrorCode.ORG_DEPARTMENT_CODE_TAKEN: ErrorDefinition(409, "errors.department_code_taken"),
    ErrorCode.ORG_DEPARTMENT_NOT_EMPTY: ErrorDefinition(409, "errors.department_not_empty"),
    ErrorCode.ORG_DEPARTMENT_HAS_CHILDREN: ErrorDefinition(
        409, "errors.department_has_children"
    ),
    ErrorCode.ORG_DEPARTMENT_PARENT_INVALID: ErrorDefinition(
        422, "errors.department_parent_invalid"
    ),
    ErrorCode.ORG_DEPARTMENT_MOVE_INTO_DESCENDANT: ErrorDefinition(
        409, "errors.department_move_into_descendant"
    ),
    ErrorCode.ORG_DEPARTMENT_DEPTH_EXCEEDED: ErrorDefinition(
        422, "errors.department_depth_exceeded"
    ),
    ErrorCode.INTERNAL_ERROR: ErrorDefinition(500, "errors.internal_error", expose_detail=False),
    ErrorCode.SERVICE_UNAVAILABLE: ErrorDefinition(
        503, "errors.service_unavailable", expose_detail=False
    ),
}


def definition_of(code: ErrorCode) -> ErrorDefinition:
    return ERRORS[code]


class AppError(Exception):
    """Raised by application code to produce a catalogued error response.

    Carries an optional human-readable detail for the *log*; whether it reaches
    the client is decided by the catalogue, not by the raise site.
    """

    def __init__(self, code: ErrorCode, detail: str | None = None) -> None:
        super().__init__(detail or code.value)
        self.code = code
        self.detail = detail

    @property
    def status_code(self) -> int:
        return definition_of(self.code).status_code

    @property
    def message_key(self) -> str:
        return definition_of(self.code).message_key
