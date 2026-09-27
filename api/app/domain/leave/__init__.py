"""Leave: types, balances, requests, and the calendar the attendance scan reads.

The package re-exports its vocabulary so a caller imports `from app.domain.leave
import LeaveService, LeaveCalendar, ENTITY_TYPE` rather than reaching into the
module that happens to hold each name — the same shape `domain/attendance` and
`domain/schedule` publish.
"""

from app.domain.leave.calculation import counts_by_year, total, working_days
from app.domain.leave.errors import LeaveErrorCode
from app.domain.leave.models import (
    ATTACHMENT_REFERENCE_PATTERN,
    ENTITY_TYPE,
    BalanceGrant,
    LeaveBalance,
    LeaveBalanceEntry,
    LeaveBalanceView,
    LeaveDay,
    LeaveEntryType,
    LeaveRequest,
    LeaveRequestInput,
    LeaveRequestQuery,
    LeaveRequestState,
    LeaveRequestView,
    LeaveType,
    LeaveTypeInput,
    LeaveTypePatch,
    SettleFailure,
    SettleReport,
    YearAllocation,
    state_of_request,
)
from app.domain.leave.repository import LeaveRepository
from app.domain.leave.service import (
    BALANCE_ENTITY,
    MAX_REQUEST_DAYS,
    MAX_YEARS_AHEAD,
    REQUEST_ENTITY,
    TYPE_ENTITY,
    LeaveCalendar,
    LeaveService,
)

__all__ = [
    "ATTACHMENT_REFERENCE_PATTERN",
    "BALANCE_ENTITY",
    "ENTITY_TYPE",
    "MAX_REQUEST_DAYS",
    "MAX_YEARS_AHEAD",
    "REQUEST_ENTITY",
    "TYPE_ENTITY",
    "BalanceGrant",
    "LeaveBalance",
    "LeaveBalanceEntry",
    "LeaveBalanceView",
    "LeaveCalendar",
    "LeaveDay",
    "LeaveEntryType",
    "LeaveErrorCode",
    "LeaveRepository",
    "LeaveRequest",
    "LeaveRequestInput",
    "LeaveRequestQuery",
    "LeaveRequestState",
    "LeaveRequestView",
    "LeaveService",
    "LeaveType",
    "LeaveTypeInput",
    "LeaveTypePatch",
    "SettleFailure",
    "SettleReport",
    "YearAllocation",
    "counts_by_year",
    "state_of_request",
    "total",
    "working_days",
]
