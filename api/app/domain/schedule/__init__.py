"""Schedules: which pattern governs a day, and what a month was worth.

Two answers, and both are keyed by a *date* — the same discipline the attendance
module states, for the same reason: a person's week changes over time, so "what
does Ana work" has no answer and "what did Ana work on 12 March" has exactly one.

* **Resolution**: an employee override beats the department's schedule, which beats
  the company default (`calculation.resolve_schedule`), and the region a day's
  holidays are read in comes from the department somebody worked in that day.
* **Expected hours**: a month is the sum of its days, each day being the schedule's
  minutes for that weekday unless a holiday zeroes it — and the result is
  snapshotted with the inputs that produced it, because the four-year obligation is
  to show how a figure was reached (`docs/DESIGN.md` §8.1).

Holidays are data: imported from a CSV, edited by HR, and cached per year under a
stamp derived from the rows themselves, so an edit reaches the next read whether it
arrived through the API, through the import command, or through `psql`.

**Nothing is re-exported here, and that is deliberate.** `attendance.models` imports
`DayExpectation` from `schedule.models` — the attendance day is derived against the
schedule's answer — so a package that imported `schedule.service` (which takes its
time source from `attendance.models`) would close a circle the moment anything
imported the attendance models first. The modules are the surface: import
`app.domain.schedule.service` for the service, `app.domain.schedule.models` for the
value objects.
"""
