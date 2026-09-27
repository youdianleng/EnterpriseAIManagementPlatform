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

    # Attendance: the append-only punch stream and the day derived from it
    # (ticket 21). The punches a state machine refuses are separated from the
    # requests that are simply malformed, so a client can tell "you already
    # clocked in" from "that was not a clock" without reading the message.
    #: A shift is already open. One at a time: a second clock_in is a double click
    #: or a stale tab, not the start of a second shift.
    ATTENDANCE_ALREADY_CLOCKED_IN = "ERR_ATT_001"
    #: No shift this punch could close — none is open, or the only one that is
    #: began longer ago than a shift may last (`attendance.MAX_SHIFT`).
    ATTENDANCE_NO_OPEN_SHIFT = "ERR_ATT_002"
    #: The instant is in the future. Working time records what happened.
    ATTENDANCE_EVENT_IN_FUTURE = "ERR_ATT_003"
    #: A terminated employee's record is closed; a missed punch on it is a
    #: correction, not a new punch (ticket 18 owns what termination does).
    ATTENDANCE_EMPLOYEE_TERMINATED = "ERR_ATT_004"
    #: `clock` was asked to append a correction. A correction restates an event
    #: that exists, with a target and a reason, and arrives through the correction
    #: flow (ticket 24) rather than from a clock button.
    ATTENDANCE_CORRECTION_NOT_A_PUNCH = "ERR_ATT_005"
    #: An inverted range, or one longer than this module will answer in a single
    #: request. Refused rather than answered with an empty list.
    ATTENDANCE_RANGE_INVALID = "ERR_ATT_006"

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
    #: The document's approval route resolves to somebody who has left, so no
    #: decision can ever be taken on it. Refused at submission, naming the people
    #: HR has to reassign (ticket 18); the alternative — falling back to the
    #: approver's own approver — hides a configuration nobody repaired.
    PERSONNEL_APPROVER_TERMINATED = "ERR_PCH_008"

    # Projects and their tasks (ticket 27). Two themes run through these codes: an
    # *archived* project is a state the caller has to be told apart from "you may
    # not", and a task's billable flag is never something the caller got wrong,
    # because the caller never states it.
    PROJECT_NOT_FOUND = "ERR_PRJ_001"
    #: The code is the project's identity in a timesheet and on an invoice, so it
    #: is unique for good rather than among live projects: reusing one would make
    #: two projects share a name in a record that outlives both.
    PROJECT_CODE_TAKEN = "ERR_PRJ_002"
    PROJECT_DATES_INVALID = "ERR_PRJ_003"
    PROJECT_DEPARTMENT_NOT_FOUND = "ERR_PRJ_004"
    PROJECT_MANAGER_NOT_FOUND = "ERR_PRJ_005"
    #: Archived: readable for ever, and closed to new time and to every change to
    #: the project's own configuration. Its own code because the client shows a
    #: different message from "you may not do that".
    PROJECT_ARCHIVED = "ERR_PRJ_006"
    #: Not active yet, so it may not receive time or new tasks. Distinct from
    #: archived because it is a step on the way rather than an end state.
    PROJECT_NOT_ACTIVE = "ERR_PRJ_007"
    PROJECT_TASK_NOT_FOUND = "ERR_PRJ_008"
    #: Unique within the project, not globally: `01` is a drawing number in one
    #: project and means nothing in another.
    PROJECT_TASK_CODE_TAKEN = "ERR_PRJ_009"
    #: The task is switched off, or belongs to a project the caller may not record
    #: time against. One code for the two, because a timesheet row against either
    #: is the same mistake: time that no report can attribute.
    PROJECT_TASK_NOT_RECORDABLE = "ERR_PRJ_010"
    #: The caller is not the project's manager, and does not hold a role whose remit
    #: is every project. The kernel's answer, relayed.
    PROJECT_NOT_MANAGEABLE = "ERR_PRJ_011"
    #: The task is switched off already. Its own code because "it is already off" is a
    #: different answer from "I turned it off": a client about to tell somebody their
    #: time can no longer be booked should not be told it just changed something.
    PROJECT_TASK_ALREADY_INACTIVE = "ERR_PRJ_012"

    # Work schedules, holidays and expected hours (ticket 22). Three themes: a
    # *pattern* that does not state a pattern (`SCHEDULE_INVALID_DAY`), an
    # administrative conflict with something that already exists (a second
    # department schedule, an overlapping override), and a calendar file that
    # cannot be trusted — the last one refused whole, because a half-imported
    # holiday calendar is a payroll figure that is half wrong.
    SCHEDULE_NOT_FOUND = "ERR_SCH_001"
    SCHEDULE_CODE_TAKEN = "ERR_SCH_002"
    #: A day whose window, break and expected minutes do not agree, a weekday given
    #: twice, or a week that does not end after it starts.
    SCHEDULE_INVALID_DAY = "ERR_SCH_003"
    #: A department already has an active schedule, or the company already has a
    #: default. Resolution has exactly one answer, and this is where a second one
    #: is refused rather than resolved by whichever row a query returns first.
    SCHEDULE_ALREADY_SET = "ERR_SCH_004"
    #: A deactivated schedule cannot be given to somebody new. An override that
    #: already points at one survives: withdrawing a part-time pattern must not
    #: silently restore a full-time week.
    SCHEDULE_INACTIVE = "ERR_SCH_005"
    SCHEDULE_OVERRIDE_OVERLAPS = "ERR_SCH_006"
    SCHEDULE_OVERRIDE_NOT_FOUND = "ERR_SCH_007"
    #: A holiday with no region where one is required, a region where none belongs,
    #: or a year that disagrees with its own date.
    SCHEDULE_INVALID_HOLIDAY = "ERR_SCH_008"
    SCHEDULE_HOLIDAY_NOT_FOUND = "ERR_SCH_009"
    #: An import file that is not a holiday calendar. Refused row by row, with the
    #: line numbers, and nothing written.
    SCHEDULE_INVALID_HOLIDAY_FILE = "ERR_SCH_010"
    #: That date, scope and region is already a holiday. An import updates it
    #: instead; a person adding one by hand is told rather than silently overwriting.
    SCHEDULE_HOLIDAY_EXISTS = "ERR_SCH_011"

    # Weekly timesheets (ticket 28). Four themes. A week is identified by its
    # Monday, so "not a Monday" is its own code rather than a validation error: the
    # fix is to send the right date, not to correct a field. An entry's project and
    # task must agree and must be bookable, which is what `ENTRY_*` says. A
    # *locked* week is a state rather than a permission — the caller may well own
    # it — so it reads like `PROJECT_ARCHIVED`'s 409 and not like a 403. And
    # `TIMESHEET_ALREADY_SUBMITTED` is the engine's rule in this module's
    # vocabulary: a rejection is final for the request, so the way forward names
    # the edit that has to happen first.
    TIMESHEET_NOT_FOUND = "ERR_TSH_001"
    #: The week's start is not the Monday of the week it names. The whole module
    #: keys on that, and the database refuses it as well.
    TIMESHEET_WEEK_NOT_MONDAY = "ERR_TSH_002"
    #: One timesheet per person per week, refused by a unique constraint. Its own
    #: code because the answer is "open the one you have", not "try again later".
    TIMESHEET_ALREADY_EXISTS = "ERR_TSH_003"
    #: Submitted, or already decided: not the caller's to edit any more. The
    #: engine's request is what holds the decision, and this is the module's word
    #: for "not in a state you may write in".
    TIMESHEET_NOT_EDITABLE = "ERR_TSH_004"
    TIMESHEET_ENTRY_NOT_FOUND = "ERR_TSH_005"
    #: The task belongs to another project. 404 for the reason the project module
    #: gives: the route names both, so telling them apart is an existence oracle.
    TIMESHEET_ENTRY_TASK_MISMATCH = "ERR_TSH_006"
    #: The project is not `active`: draft, closed or archived. Reported with the
    #: status in the detail, and refused by the database as well as here — a
    #: constraint the application can forget is not the guarantee ticket 27 asked
    #: for. 409 rather than 422, matching `PROJECT_ARCHIVED`.
    TIMESHEET_ENTRY_PROJECT_NOT_RECORDABLE = "ERR_TSH_007"
    #: The date is outside the project's own start and end dates.
    TIMESHEET_ENTRY_OUTSIDE_PROJECT_DATES = "ERR_TSH_008"
    #: Minutes must be a positive integer no longer than `MAX_ENTRY_MINUTES`.
    TIMESHEET_ENTRY_MINUTES_INVALID = "ERR_TSH_009"
    #: The week is not the caller's own. 403, by the ticket's own wording.
    TIMESHEET_NOT_YOURS = "ERR_TSH_010"
    #: Copying a week into itself, or off the calendar.
    TIMESHEET_COPY_SOURCE_INVALID = "ERR_TSH_011"
    #: The week being copied into already has entries. Refused rather than merged:
    #: a copy that added to what was there is a duplicate nobody sees.
    TIMESHEET_COPY_TARGET_NOT_EMPTY = "ERR_TSH_012"
    #: The engine refused the submission — a request already filed, or a rejection
    #: that is final. Carries the engine's own code in the detail.
    TIMESHEET_SUBMISSION_REFUSED = "ERR_TSH_013"

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
    # 409 for the two punches the state machine refuses — the request was fine and
    # the stream says no — and 422 for the two the caller got wrong. Terminated is
    # the conflict: nobody refused the caller anything, their record is closed.
    ErrorCode.ATTENDANCE_ALREADY_CLOCKED_IN: ErrorDefinition(
        409, "errors.attendance_already_clocked_in"
    ),
    ErrorCode.ATTENDANCE_NO_OPEN_SHIFT: ErrorDefinition(409, "errors.attendance_no_open_shift"),
    ErrorCode.ATTENDANCE_EVENT_IN_FUTURE: ErrorDefinition(
        422, "errors.attendance_event_in_future"
    ),
    ErrorCode.ATTENDANCE_EMPLOYEE_TERMINATED: ErrorDefinition(
        409, "errors.attendance_employee_terminated"
    ),
    ErrorCode.ATTENDANCE_CORRECTION_NOT_A_PUNCH: ErrorDefinition(
        400, "errors.attendance_correction_not_a_punch"
    ),
    ErrorCode.ATTENDANCE_RANGE_INVALID: ErrorDefinition(422, "errors.attendance_range_invalid"),
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
    ErrorCode.PERSONNEL_APPROVER_TERMINATED: ErrorDefinition(
        409, "errors.personnel_approver_terminated"
    ),
    ErrorCode.PROJECT_NOT_FOUND: ErrorDefinition(404, "errors.project_not_found"),
    ErrorCode.PROJECT_CODE_TAKEN: ErrorDefinition(409, "errors.project_code_taken"),
    ErrorCode.PROJECT_DATES_INVALID: ErrorDefinition(422, "errors.project_dates_invalid"),
    ErrorCode.PROJECT_DEPARTMENT_NOT_FOUND: ErrorDefinition(
        422, "errors.project_department_not_found"
    ),
    ErrorCode.PROJECT_MANAGER_NOT_FOUND: ErrorDefinition(422, "errors.project_manager_not_found"),
    # 409, not 403: an archived project is a state, and the caller may well be its
    # manager. Telling them apart is what lets the UI offer "restore" rather than
    # "ask for access".
    ErrorCode.PROJECT_ARCHIVED: ErrorDefinition(409, "errors.project_archived"),
    ErrorCode.PROJECT_NOT_ACTIVE: ErrorDefinition(409, "errors.project_not_active"),
    ErrorCode.PROJECT_TASK_NOT_FOUND: ErrorDefinition(404, "errors.project_task_not_found"),
    ErrorCode.PROJECT_TASK_CODE_TAKEN: ErrorDefinition(409, "errors.project_task_code_taken"),
    ErrorCode.PROJECT_TASK_NOT_RECORDABLE: ErrorDefinition(
        422, "errors.project_task_not_recordable"
    ),
    ErrorCode.PROJECT_NOT_MANAGEABLE: ErrorDefinition(403, "errors.project_not_manageable"),
    ErrorCode.PROJECT_TASK_ALREADY_INACTIVE: ErrorDefinition(
        409, "errors.project_task_already_inactive"
    ),
    # 404 for the two lookups, 409 for the two conflicts with an existing decision,
    # and 422 for the two the caller wrote wrong. `SCHEDULE_INACTIVE` is the odd one
    # and deliberately a 422: nothing conflicts, the schedule simply is not in the
    # catalogue any more, and the fix is to choose another one.
    ErrorCode.SCHEDULE_NOT_FOUND: ErrorDefinition(404, "errors.schedule_not_found"),
    ErrorCode.SCHEDULE_HOLIDAY_NOT_FOUND: ErrorDefinition(404, "errors.schedule_holiday_not_found"),
    ErrorCode.SCHEDULE_OVERRIDE_NOT_FOUND: ErrorDefinition(
        404, "errors.schedule_override_not_found"
    ),
    ErrorCode.SCHEDULE_CODE_TAKEN: ErrorDefinition(409, "errors.schedule_code_taken"),
    ErrorCode.SCHEDULE_ALREADY_SET: ErrorDefinition(409, "errors.schedule_already_set"),
    ErrorCode.SCHEDULE_OVERRIDE_OVERLAPS: ErrorDefinition(
        409, "errors.schedule_override_overlaps"
    ),
    ErrorCode.SCHEDULE_INVALID_DAY: ErrorDefinition(422, "errors.schedule_invalid_day"),
    ErrorCode.SCHEDULE_INACTIVE: ErrorDefinition(422, "errors.schedule_inactive"),
    ErrorCode.SCHEDULE_INVALID_HOLIDAY: ErrorDefinition(422, "errors.schedule_invalid_holiday"),
    ErrorCode.SCHEDULE_INVALID_HOLIDAY_FILE: ErrorDefinition(
        422, "errors.schedule_invalid_holiday_file"
    ),
    ErrorCode.SCHEDULE_HOLIDAY_EXISTS: ErrorDefinition(409, "errors.schedule_holiday_exists"),
    # 404 for the two lookups, 409 for the four states the caller has to be told
    # apart from "you may not", 422 for the three the caller wrote wrong. A locked
    # week is the deliberate 409: the caller owns it, and "you cannot write here"
    # has a different remedy from "that is not yours".
    ErrorCode.TIMESHEET_NOT_FOUND: ErrorDefinition(404, "errors.timesheet_not_found"),
    ErrorCode.TIMESHEET_ENTRY_NOT_FOUND: ErrorDefinition(404, "errors.timesheet_entry_not_found"),
    ErrorCode.TIMESHEET_ENTRY_TASK_MISMATCH: ErrorDefinition(
        404, "errors.timesheet_entry_task_mismatch"
    ),
    ErrorCode.TIMESHEET_ALREADY_EXISTS: ErrorDefinition(409, "errors.timesheet_already_exists"),
    ErrorCode.TIMESHEET_NOT_EDITABLE: ErrorDefinition(409, "errors.timesheet_not_editable"),
    ErrorCode.TIMESHEET_ENTRY_PROJECT_NOT_RECORDABLE: ErrorDefinition(
        409, "errors.timesheet_entry_project_not_recordable"
    ),
    ErrorCode.TIMESHEET_COPY_TARGET_NOT_EMPTY: ErrorDefinition(
        409, "errors.timesheet_copy_target_not_empty"
    ),
    ErrorCode.TIMESHEET_SUBMISSION_REFUSED: ErrorDefinition(
        409, "errors.timesheet_submission_refused"
    ),
    ErrorCode.TIMESHEET_NOT_YOURS: ErrorDefinition(403, "errors.timesheet_not_yours"),
    ErrorCode.TIMESHEET_WEEK_NOT_MONDAY: ErrorDefinition(
        422, "errors.timesheet_week_not_monday"
    ),
    ErrorCode.TIMESHEET_ENTRY_OUTSIDE_PROJECT_DATES: ErrorDefinition(
        422, "errors.timesheet_entry_outside_project_dates"
    ),
    ErrorCode.TIMESHEET_ENTRY_MINUTES_INVALID: ErrorDefinition(
        422, "errors.timesheet_entry_minutes_invalid"
    ),
    ErrorCode.TIMESHEET_COPY_SOURCE_INVALID: ErrorDefinition(
        422, "errors.timesheet_copy_source_invalid"
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
