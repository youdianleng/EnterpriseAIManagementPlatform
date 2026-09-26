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
    #: A method the path does not serve. Its own code because the status has to
    #: stay 405: a client telling "you cannot write here" apart from "your request
    #: was malformed" is the difference between a clear error and a mystery.
    METHOD_NOT_ALLOWED = "ERR_VALIDATION_003"

    # Authentication and authorisation.
    UNAUTHENTICATED = "ERR_AUTH_001"
    FORBIDDEN = "ERR_AUTH_002"
    ACCOUNT_LOCKED = "ERR_AUTH_003"

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
    #: A department manager has to work in the department they approve for.
    ORG_MANAGER_NOT_IN_DEPARTMENT = "ERR_ORG_008"

    # Employees and their position assignments.
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

    # Job position catalogue.
    POSITION_NOT_FOUND = "ERR_POS_001"
    POSITION_CODE_TAKEN = "ERR_POS_002"
    POSITION_DEPARTMENT_INVALID = "ERR_POS_003"
    POSITION_IN_USE = "ERR_POS_004"

    # Accounts.
    ACCOUNT_NOT_FOUND = "ERR_ACC_001"
    ACCOUNT_USERNAME_TAKEN = "ERR_ACC_002"
    ACCOUNT_EMPLOYEE_HAS_ACCOUNT = "ERR_ACC_003"
    ACCOUNT_EMPLOYEE_NOT_ACTIVE = "ERR_ACC_004"
    ACCOUNT_ALREADY_IN_STATE = "ERR_ACC_005"
    ACCOUNT_PASSWORD_POLICY = "ERR_ACC_006"
    ACCOUNT_INVALID_CREDENTIALS = "ERR_ACC_007"
    ACCOUNT_DISABLED = "ERR_ACC_008"
    ACCOUNT_PASSWORD_REUSED = "ERR_ACC_009"
    #: A role outside the fixed set. A data error, not an extension point.
    ACCOUNT_ROLE_UNKNOWN = "ERR_ACC_010"
    #: Refusing to remove the last administrator: the alternative is a system
    #: nobody can administer.
    ACCOUNT_LAST_ADMINISTRATOR = "ERR_ACC_011"

    # Sessions.
    SESSION_INVALID = "ERR_SES_001"
    PASSWORD_CHANGE_REQUIRED = "ERR_SES_002"

    # Approval engine, shared by every kind of request.
    APPROVAL_NOT_FOUND = "ERR_APR_001"
    #: One entity, one open request. The database enforces it as well; this code
    #: is what makes the refusal readable.
    APPROVAL_ALREADY_OPEN = "ERR_APR_002"
    #: Nobody is configured to approve this person: neither the primary position
    #: nor its department names one.
    APPROVAL_APPROVER_UNRESOLVED = "ERR_APR_003"
    #: The second level is HR, and no HR account exists other than the requester.
    APPROVAL_HR_UNAVAILABLE = "ERR_APR_004"
    #: Not the approver this level resolved to. Administrators are included: the
    #: engine has no role bypass.
    APPROVAL_NOT_APPROVER = "ERR_APR_005"
    APPROVAL_NOT_REQUESTER = "ERR_APR_006"
    APPROVAL_NOT_WITHDRAWABLE = "ERR_APR_007"
    #: A decision was attempted on a request that is not awaiting one.
    APPROVAL_NOT_PENDING = "ERR_APR_008"
    #: A rejection is final for the entity: it may not be submitted again.
    APPROVAL_PREVIOUSLY_REJECTED = "ERR_APR_009"

    # Notifications. One code, not two: "there is no such notification" and "that
    # notification is somebody else's" are the same answer to the caller, and
    # saying which one it was would turn the endpoint into an existence oracle.
    NOTIFICATION_NOT_YOURS = "ERR_NTF_001"

    # Personnel changes: one document for 入转调离, five change types (ticket 17).
    PERSONNEL_CHANGE_NOT_FOUND = "ERR_PCH_001"
    #: The payload is missing, empty, names a field its change type does not
    #: carry, omits a required one, or carries a value of the wrong kind.
    PERSONNEL_CHANGE_INVALID_PAYLOAD = "ERR_PCH_002"
    #: Every change type but a join is about somebody who already works here.
    PERSONNEL_CHANGE_EMPLOYEE_REQUIRED = "ERR_PCH_003"
    #: Only a draft is the caller's to correct or to file.
    PERSONNEL_CHANGE_NOT_DRAFT = "ERR_PCH_004"
    #: Applied is final. The way back is a counter-change, and the message says so
    #: rather than leaving the caller to guess.
    PERSONNEL_CHANGE_ALREADY_APPLIED = "ERR_PCH_005"
    #: Cancelled already, or rejected by the engine: nothing left to cancel.
    PERSONNEL_CHANGE_NOT_CANCELLABLE = "ERR_PCH_006"
    #: The record the change was written against moved between approval and its
    #: effective date. Reported by the job; whatever the change had already
    #: written is rolled back, so it leaves no half-applied state.
    PERSONNEL_CHANGE_APPLY_FAILED = "ERR_PCH_007"

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
    ErrorCode.METHOD_NOT_ALLOWED: ErrorDefinition(405, "errors.method_not_allowed"),
    ErrorCode.UNAUTHENTICATED: ErrorDefinition(401, "errors.unauthenticated"),
    ErrorCode.FORBIDDEN: ErrorDefinition(403, "errors.forbidden"),
    ErrorCode.ACCOUNT_LOCKED: ErrorDefinition(423, "errors.account_locked"),
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
    ErrorCode.ORG_MANAGER_NOT_IN_DEPARTMENT: ErrorDefinition(
        422, "errors.org_manager_not_in_department"
    ),
    ErrorCode.EMPLOYEE_NOT_FOUND: ErrorDefinition(404, "errors.employee_not_found"),
    ErrorCode.EMPLOYEE_EMAIL_TAKEN: ErrorDefinition(409, "errors.employee_email_taken"),
    ErrorCode.EMPLOYEE_NUMBER_TAKEN: ErrorDefinition(409, "errors.employee_number_taken"),
    ErrorCode.EMPLOYEE_DATES_INVALID: ErrorDefinition(422, "errors.employee_dates_invalid"),
    ErrorCode.EMPLOYEE_POSITION_NOT_FOUND: ErrorDefinition(
        422, "errors.employee_position_not_found"
    ),
    ErrorCode.EMPLOYEE_POSITION_INACTIVE: ErrorDefinition(
        422, "errors.employee_position_inactive"
    ),
    ErrorCode.EMPLOYEE_MANAGER_NOT_FOUND: ErrorDefinition(
        422, "errors.employee_manager_not_found"
    ),
    ErrorCode.EMPLOYEE_ASSIGNMENT_NOT_FOUND: ErrorDefinition(
        404, "errors.employee_assignment_not_found"
    ),
    ErrorCode.EMPLOYEE_ASSIGNMENT_ENDED: ErrorDefinition(
        409, "errors.employee_assignment_ended"
    ),
    ErrorCode.EMPLOYEE_LAST_ASSIGNMENT: ErrorDefinition(409, "errors.employee_last_assignment"),
    ErrorCode.POSITION_NOT_FOUND: ErrorDefinition(404, "errors.position_not_found"),
    ErrorCode.POSITION_CODE_TAKEN: ErrorDefinition(409, "errors.position_code_taken"),
    ErrorCode.POSITION_DEPARTMENT_INVALID: ErrorDefinition(
        422, "errors.position_department_invalid"
    ),
    ErrorCode.POSITION_IN_USE: ErrorDefinition(409, "errors.position_in_use"),
    ErrorCode.ACCOUNT_NOT_FOUND: ErrorDefinition(404, "errors.account_not_found"),
    ErrorCode.ACCOUNT_USERNAME_TAKEN: ErrorDefinition(409, "errors.account_username_taken"),
    ErrorCode.ACCOUNT_EMPLOYEE_HAS_ACCOUNT: ErrorDefinition(
        409, "errors.account_employee_has_account"
    ),
    ErrorCode.ACCOUNT_EMPLOYEE_NOT_ACTIVE: ErrorDefinition(
        422, "errors.account_employee_not_active"
    ),
    ErrorCode.ACCOUNT_ALREADY_IN_STATE: ErrorDefinition(409, "errors.account_already_in_state"),
    ErrorCode.ACCOUNT_PASSWORD_POLICY: ErrorDefinition(422, "errors.account_password_policy"),
    ErrorCode.ACCOUNT_INVALID_CREDENTIALS: ErrorDefinition(
        401, "errors.account_invalid_credentials"
    ),
    ErrorCode.ACCOUNT_DISABLED: ErrorDefinition(403, "errors.account_disabled"),
    ErrorCode.ACCOUNT_PASSWORD_REUSED: ErrorDefinition(422, "errors.account_password_reused"),
    ErrorCode.ACCOUNT_ROLE_UNKNOWN: ErrorDefinition(422, "errors.account_role_unknown"),
    ErrorCode.ACCOUNT_LAST_ADMINISTRATOR: ErrorDefinition(
        409, "errors.account_last_administrator"
    ),
    # 401 rather than 403: the session is not merely unauthorised for this
    # resource, it is no longer a session at all, and the client should sign in.
    ErrorCode.SESSION_INVALID: ErrorDefinition(401, "errors.session_invalid"),
    ErrorCode.APPROVAL_NOT_FOUND: ErrorDefinition(404, "errors.approval_not_found"),
    ErrorCode.APPROVAL_ALREADY_OPEN: ErrorDefinition(409, "errors.approval_already_open"),
    ErrorCode.APPROVAL_APPROVER_UNRESOLVED: ErrorDefinition(
        422, "errors.approval_approver_unresolved"
    ),
    ErrorCode.APPROVAL_HR_UNAVAILABLE: ErrorDefinition(422, "errors.approval_hr_unavailable"),
    ErrorCode.APPROVAL_NOT_APPROVER: ErrorDefinition(403, "errors.approval_not_approver"),
    ErrorCode.APPROVAL_NOT_REQUESTER: ErrorDefinition(403, "errors.approval_not_requester"),
    ErrorCode.APPROVAL_NOT_WITHDRAWABLE: ErrorDefinition(
        409, "errors.approval_not_withdrawable"
    ),
    ErrorCode.APPROVAL_NOT_PENDING: ErrorDefinition(409, "errors.approval_not_pending"),
    ErrorCode.APPROVAL_PREVIOUSLY_REJECTED: ErrorDefinition(
        409, "errors.approval_previously_rejected"
    ),
    ErrorCode.NOTIFICATION_NOT_YOURS: ErrorDefinition(403, "errors.notification_not_yours"),
    ErrorCode.PERSONNEL_CHANGE_NOT_FOUND: ErrorDefinition(
        404, "errors.personnel_change_not_found"
    ),
    ErrorCode.PERSONNEL_CHANGE_INVALID_PAYLOAD: ErrorDefinition(
        422, "errors.personnel_change_invalid_payload"
    ),
    ErrorCode.PERSONNEL_CHANGE_EMPLOYEE_REQUIRED: ErrorDefinition(
        422, "errors.personnel_change_employee_required"
    ),
    ErrorCode.PERSONNEL_CHANGE_NOT_DRAFT: ErrorDefinition(
        409, "errors.personnel_change_not_draft"
    ),
    ErrorCode.PERSONNEL_CHANGE_ALREADY_APPLIED: ErrorDefinition(
        409, "errors.personnel_change_already_applied"
    ),
    ErrorCode.PERSONNEL_CHANGE_NOT_CANCELLABLE: ErrorDefinition(
        409, "errors.personnel_change_not_cancellable"
    ),
    ErrorCode.PERSONNEL_CHANGE_APPLY_FAILED: ErrorDefinition(
        409, "errors.personnel_change_apply_failed"
    ),
    # 403 with its own code so the client can route to the change-password screen
    # instead of showing a permission error.
    ErrorCode.PASSWORD_CHANGE_REQUIRED: ErrorDefinition(403, "errors.password_change_required"),
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
