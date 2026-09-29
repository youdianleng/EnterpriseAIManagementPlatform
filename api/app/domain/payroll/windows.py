"""The effective-date window, as one rule with one meaning.

「同一位员工的历史记录形成按时间排列的调整链，任一时点可查出当时有效的值」 is a statement
about a *range*, and this module is that statement in Python. It exists beside the two
places the same rule is expressed in other languages — the repository's `covers()` SQL
and the exclusion constraint's `daterange(effective_from, effective_to, '[]')` — so a
test can hold the query's answer against an independent predicate instead of against
itself.

The range is **inclusive at both ends** on purpose, and the exclusion constraint says
the same thing: a record that ends on the 31st is in force *on* the 31st, so the next
one starts on the 1st and the two touch without overlapping. An exclusive end would make
"what was in force on the 31st" unanswerable for whichever record the author chose not
to include.
"""

from collections.abc import Iterable
from datetime import date

from app.domain.payroll.models import SalaryRecord


def in_force(record_from: date, record_to: date | None, day: date) -> bool:
    """True when `[record_from, record_to]` covers `day`. An absent end is open."""
    return record_from <= day and (record_to is None or record_to >= day)


def on(records: Iterable[SalaryRecord], day: date) -> SalaryRecord | None:
    """The record in force on `day`, from a chain.

    A fold for *tests and callers that already hold the chain*; the API answers the same
    question in SQL. It returns `None` rather than a zero-valued record when nothing
    covers the day — "no record in force" and "a salary of zero" are different facts,
    and only one of them is a number somebody could act on.
    """
    for record in records:
        if in_force(record.effective_from, record.effective_to, day):
            return record
    return None


__all__ = ["in_force", "on"]
