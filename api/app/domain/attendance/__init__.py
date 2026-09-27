"""Attendance: the event stream, and the day the events derive.

The interface is four operations (`docs/architecture/codebase-design.md` §2.4) —
`clock`, `day_view`, `recompute_day`, `range_view` — and every one of them takes
and returns *dates*. The Madrid business day, the DST transitions and the
cross-midnight rule are implementation here; a caller has no way to express a day
incorrectly, which is the property the interface exists for.
"""

from app.domain.attendance.business_day import (
    MADRID,
    TIMEZONE_NAME,
    business_date_of,
    dates_between,
    madrid_today,
)
from app.domain.attendance.derivation import derive
from app.domain.attendance.errors import AttendanceErrorCode
from app.domain.attendance.models import (
    CLOCK_SKEW,
    MAX_RANGE_DAYS,
    MAX_SHIFT,
    PUNCH_EVENT_TYPES,
    TERMINATED_STATUS,
    AttendanceEvent,
    DayRecord,
    DayStatus,
    EventSource,
    EventType,
    NewEvent,
    TimeSource,
    utc_now,
)
from app.domain.attendance.repository import AttendanceRepository
from app.domain.attendance.service import AttendanceService

__all__ = [
    "CLOCK_SKEW",
    "MADRID",
    "MAX_RANGE_DAYS",
    "MAX_SHIFT",
    "PUNCH_EVENT_TYPES",
    "TERMINATED_STATUS",
    "TIMEZONE_NAME",
    "AttendanceErrorCode",
    "AttendanceEvent",
    "AttendanceRepository",
    "AttendanceService",
    "DayRecord",
    "DayStatus",
    "EventSource",
    "EventType",
    "NewEvent",
    "TimeSource",
    "business_date_of",
    "dates_between",
    "derive",
    "madrid_today",
    "utc_now",
]
