"""Working-day arithmetic and the cross-year split. Pure: values in, values out.

**The schedule module answers the calendar question, and this module asks it.**
`DayExpectation.is_working_day` is "the schedule expected minutes and no holiday
zeroed them", which is exactly the ticket's 自动排除周末与节假日表命中的日期 — and it
is that module's answer rather than one re-derived here, because a second
implementation of "is this a working day" is a second answer that would eventually
disagree with the expected-hours figure a month is measured against.

Three functions, and the first is the one the whole ticket turns on:

* `working_days` keeps the dates the schedule calls working days, in order. A
  request for Friday to Monday is worth two of them, and a holiday inside the range
  is worth none.
* `counts_by_year` is the cross-year rule in one line: each year's share is the
  working days that fall *in that year*. December's days come out of December's
  allowance and January's out of January's, which is both the obvious rule and the
  one a reader can check by hand.
* `total` sums a split, and exists so that "the request is worth N days" and "the
  years were charged N days between them" are the same arithmetic rather than two.
"""

from collections.abc import Iterable, Sequence
from datetime import date

from app.domain.schedule.models import DayExpectation


def working_days(expectations: Iterable[DayExpectation]) -> tuple[date, ...]:
    """The dates somebody was due to work, oldest first.

    Ordered because both the count and the split are read by a person: a range's
    working days are the days the leave costs, and the order is the order they
    happen in.
    """
    return tuple(
        expectation.business_date
        for expectation in sorted(expectations, key=lambda item: item.business_date)
        if expectation.is_working_day
    )


def counts_by_year(days: Sequence[date]) -> dict[int, int]:
    """How many of these days fall in each year, in year order.

    The whole of the cross-year rule. A day belongs to the year its own date is in,
    so a range that spans the 31st of December is charged to two allowances and
    neither of them is guessed at.
    """
    counts: dict[int, int] = {}
    for day in sorted(days):
        counts[day.year] = counts.get(day.year, 0) + 1
    return counts


def total(counts: Iterable[int]) -> int:
    return sum(counts)


__all__ = ["counts_by_year", "total", "working_days"]
