"""The Madrid business day. This module's business, and nobody else's.

`docs/architecture/codebase-design.md` §2.4 fixes the shape: the attendance module
takes and returns **dates**, never raw timestamps, for anything that aggregates by
day. The conversion from an instant to the day it counts against happens here,
once, on the way in, and every later read uses the stored result. That is the
whole mitigation for the risk the design register names ("考勤业务日与 UTC 混淆"):
a caller cannot get it wrong because a caller is never asked.

Two rules, stated rather than left to `astimezone` to imply:

* **The day is the Madrid calendar day of the instant, not the UTC one.** At
  00:30 Madrid in summer it is still 22:30 of the previous day in UTC. The punch
  belongs to the day the person was at work, which is the Madrid one.
* **The attribution window is bounded by `MAX_SHIFT`.** A punch that closes a
  shift belongs to the shift's own day even across midnight; a punch recorded
  longer after the shift began than any shift could last belongs to its own day
  instead, because it cannot be the end of that shift. The bound lives in
  `models.MAX_SHIFT` and the decision is made by the service, which is the only
  place that knows whether a shift is open.

**Never a fixed offset.** Europe/Madrid is +01:00 in winter and +02:00 in summer —
the two sides of a transition are an hour apart, and in 2026 the transitions are
29 March and 25 October, which no formula in this repository computes. `ZoneInfo`
reads the tz database; `timezone(timedelta(hours=1))` would be wrong for half the
year and silently so.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

#: The company's timezone. Named as a constant so that "which offset" has exactly
#: one answer in the codebase, and it is the one with DST in it.
MADRID = ZoneInfo("Europe/Madrid")

TIMEZONE_NAME = "Europe/Madrid"


def _aware(instant: datetime) -> datetime:
    """Refuse a naive datetime instead of guessing an offset for it.

    A naive instant means the caller has already lost the information this module
    exists to apply, and every guess available (UTC? local? the container's zone?)
    is wrong some of the time. `astimezone` would happily treat it as the server's
    local time, which is how a correct-looking application records the wrong day
    twice a year.
    """
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError(
            "an attendance instant must carry a timezone; a naive datetime cannot be "
            "attributed to a business day"
        )
    return instant


def business_date_of(instant: datetime) -> date:
    """The Madrid calendar day an instant falls on.

    This is the one conversion the module performs, and it is done once per event
    on the way in. It is deliberately *not* applied to `occurred_at` on read: the
    stored `business_date` is the answer, and re-deriving it later would let a
    later change to this function rewrite history.
    """
    return _aware(instant).astimezone(MADRID).date()


def madrid_today(now: datetime) -> date:
    """Today, as the business calendar counts it.

    The service asks this to tell a shift that is still running from one that was
    never closed, and the API asks it for "no date given, show me today" — which
    is the question a browser in another timezone would otherwise answer with its
    own midnight.
    """
    return business_date_of(now)


def dates_between(from_date: date, to_date: date) -> list[date]:
    """Every date in an inclusive range, in order.

    Inclusive at both ends because both ends are days somebody worked: the range a
    payroll export asks for is "the 1st to the 31st", and a half-open interval
    would drop one of them silently.
    """
    if to_date < from_date:
        return []
    return [
        from_date + timedelta(days=offset) for offset in range((to_date - from_date).days + 1)
    ]


__all__ = ["MADRID", "TIMEZONE_NAME", "business_date_of", "dates_between", "madrid_today"]
