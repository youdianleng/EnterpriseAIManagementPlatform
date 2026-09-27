"""Overtime: the request made in advance, the record, and the month finance exports.

The package re-exports its vocabulary so a caller imports `from app.domain.overtime
import OvertimeService, OvertimeLedger, ENTITY_TYPE` rather than reaching into the
module that happens to hold each name — the same shape `domain/leave` and
`domain/attendance` publish.

The interface is the service's, and its two sweeps are the part worth knowing about
before reading further: `resolve_decided` finishes a *document* (an approved request
becomes a record), and `settle_due` finishes a *record* (the day's actual hours are
compared with the approved ones and the smaller figure is written). `OvertimeLedger` is
the module as the attendance derivation sees it: one question, one answer.
"""

from app.domain.overtime.errors import OvertimeErrorCode
from app.domain.overtime.export import (
    EXPORT_COLUMNS,
    EXPORT_ENTITY,
    EXPORT_EXCLUDES,
    TOTAL_LABEL,
    ExportFile,
    render,
)
from app.domain.overtime.models import (
    ENTITY_TYPE,
    MAX_DAY_MINUTES,
    MAX_DAYS_AHEAD,
    MonthlySummary,
    MonthlyTotal,
    NewOvertimeRecord,
    OvertimeEntry,
    OvertimeEntryType,
    OvertimeExportRow,
    OvertimeRecord,
    OvertimeRecordQuery,
    OvertimeRecordView,
    OvertimeRequest,
    OvertimeRequestInput,
    OvertimeRequestPatch,
    OvertimeRequestQuery,
    OvertimeRequestState,
    OvertimeRequestView,
    ResolveFailure,
    ResolveReport,
    SettleFailure,
    SettleReport,
    month_bucket_of,
    period_of,
    state_of_request,
)
from app.domain.overtime.repository import OvertimeRepository
from app.domain.overtime.service import (
    DEFAULT_CONFIRMATION_THRESHOLD_MINUTES,
    RECORD_ENTITY,
    REQUEST_ENTITY,
    AttendanceDays,
    OvertimeLedger,
    OvertimeService,
)

__all__ = [
    "DEFAULT_CONFIRMATION_THRESHOLD_MINUTES",
    "ENTITY_TYPE",
    "EXPORT_COLUMNS",
    "EXPORT_ENTITY",
    "EXPORT_EXCLUDES",
    "MAX_DAYS_AHEAD",
    "MAX_DAY_MINUTES",
    "RECORD_ENTITY",
    "REQUEST_ENTITY",
    "TOTAL_LABEL",
    "AttendanceDays",
    "ExportFile",
    "MonthlySummary",
    "MonthlyTotal",
    "NewOvertimeRecord",
    "OvertimeEntry",
    "OvertimeEntryType",
    "OvertimeErrorCode",
    "OvertimeExportRow",
    "OvertimeLedger",
    "OvertimeRecord",
    "OvertimeRecordQuery",
    "OvertimeRecordView",
    "OvertimeRepository",
    "OvertimeRequest",
    "OvertimeRequestInput",
    "OvertimeRequestPatch",
    "OvertimeRequestQuery",
    "OvertimeRequestState",
    "OvertimeRequestView",
    "OvertimeService",
    "ResolveFailure",
    "ResolveReport",
    "SettleFailure",
    "SettleReport",
    "month_bucket_of",
    "period_of",
    "render",
    "state_of_request",
]
