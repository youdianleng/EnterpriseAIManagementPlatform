"""Attendance: the event stream, the day the events derive, and what is wrong with it.

The interface is four operations (`docs/architecture/codebase-design.md` §2.4) —
`clock`, `day_view`, `recompute_day`, `range_view` — and every one of them takes
and returns *dates*. The Madrid business day, the DST transitions and the
cross-midnight rule are implementation here; a caller has no way to express a day
incorrectly, which is the property the interface exists for.

Ticket 23 adds two things beside that interface rather than inside it: the nightly
anomaly scan (`anomaly_service.AnomalyService`), which is a question about a date
rather than an operation on one person's day, and the notifications a finished day
owes (`notify.py`), which decorate the service so a caller cannot forget them.
"""

from app.domain.attendance.anomalies import (
    ANOMALY_ORDER,
    PUNCH_TOLERANCE,
    Anomaly,
    AnomalyReminderReport,
    AnomalyScanReport,
    AnomalyType,
    NewAnomaly,
    detect,
)
from app.domain.attendance.anomaly_repository import (
    AnomalyRepository,
    AssumeNoLeave,
    LeaveLookup,
)
from app.domain.attendance.anomaly_service import AnomalyService
from app.domain.attendance.business_day import (
    MADRID,
    TIMEZONE_NAME,
    business_date_of,
    dates_between,
    madrid_today,
)
from app.domain.attendance.derivation import derive, effective_punches
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
from app.domain.attendance.notify import (
    AnomalyReminder,
    AttendanceNotifier,
    NotificationRoute,
    NotificationRouteSource,
)
from app.domain.attendance.repository import AttendanceRepository
from app.domain.attendance.service import AttendanceService

__all__ = [
    "ANOMALY_ORDER",
    "CLOCK_SKEW",
    "MADRID",
    "MAX_RANGE_DAYS",
    "MAX_SHIFT",
    "PUNCH_EVENT_TYPES",
    "PUNCH_TOLERANCE",
    "TERMINATED_STATUS",
    "TIMEZONE_NAME",
    "Anomaly",
    "AnomalyReminder",
    "AnomalyReminderReport",
    "AnomalyRepository",
    "AnomalyScanReport",
    "AnomalyService",
    "AnomalyType",
    "AssumeNoLeave",
    "AttendanceErrorCode",
    "AttendanceEvent",
    "AttendanceNotifier",
    "AttendanceRepository",
    "AttendanceService",
    "DayRecord",
    "DayStatus",
    "EventSource",
    "EventType",
    "LeaveLookup",
    "NewAnomaly",
    "NewEvent",
    "NotificationRoute",
    "NotificationRouteSource",
    "TimeSource",
    "business_date_of",
    "dates_between",
    "derive",
    "detect",
    "effective_punches",
    "madrid_today",
    "utc_now",
]
