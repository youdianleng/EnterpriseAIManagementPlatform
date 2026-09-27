"""The holiday calendar, cached per year and invalidated by the rows themselves.

`docs/DESIGN.md` §4.4 caches the holiday table for 24 hours, because every
expected-hours figure consults it and the table changes a handful of times a year.
What this module adds is *how* the cache is invalidated, and it follows the pattern
`domain/access/snapshot.py` already argues for:

**A version in the key, not a delete.** The key carries a stamp derived from the
year's rows (`ScheduleRepository.holiday_stamp`), so an edit does not delete
anything — the entry it produced is simply never looked up again. That matters more
here than it does for a permission snapshot, because the holiday table has a second
writer: the import command runs in its own process, and a `psql` session is always
a possibility. A counter maintained by "every writer" would be a counter that one
writer forgets; a value read back out of the table cannot be forgotten by anybody.

**The stamp is read on every lookup, and that is the price.** One indexed aggregate
over a table with a few dozen rows per year, in exchange for a cache that cannot go
stale — including when a rule about "immediate invalidation" is tested by writing
the holiday straight into PostgreSQL.

**An empty year is not cached.** There is nothing to keep, and caching "no holidays
in 2031" under a stamp a first write also produces would serve the empty answer
after the year had holidays in it.
"""

import json
from uuid import UUID

from app.domain.schedule.models import Holiday, HolidayScope

#: 24 hours, from the design's cache table. Correctness does not rest on it: the
#: version in the key is what makes an entry unreachable after a write, and this is
#: what eventually collects the entries nothing points at any more.
HOLIDAY_TTL_SECONDS = 24 * 60 * 60

HOLIDAY_KEY = "holidays:{year}:{stamp}"


def _encode(holidays: tuple[Holiday, ...]) -> str:
    return json.dumps(
        [
            {
                "id": str(holiday.id),
                "date": holiday.date.isoformat(),
                "name_es": holiday.name_es,
                "name_en": holiday.name_en,
                "scope": holiday.scope.value,
                "region_code": holiday.region_code,
                "year": holiday.year,
            }
            for holiday in holidays
        ]
    )


def _decode(raw: str) -> tuple[Holiday, ...]:
    from datetime import date

    return tuple(
        Holiday(
            id=UUID(item["id"]),
            date=date.fromisoformat(item["date"]),
            name_es=item["name_es"],
            name_en=item["name_en"],
            scope=HolidayScope(item["scope"]),
            region_code=item["region_code"],
            year=item["year"],
        )
        for item in json.loads(raw)
    )


class HolidayCache:
    """Redis, and never in the way of the database.

    Every method swallows a cache failure and reports it as a miss. A Redis that is
    down must not stop HR from importing a calendar, and the holidays are in
    PostgreSQL either way — the same direction `app/cache.py` takes for the
    permission snapshot.
    """

    def __init__(self, client=None) -> None:  # noqa: ANN001 - redis client, lazily built
        self._client = client

    def _redis(self):  # noqa: ANN202 - redis client
        if self._client is not None:
            return self._client
        # Resolved per call, not per instance: the client is loop-bound and the
        # test suite drops it between tests.
        from app.cache import get_redis

        return get_redis()

    async def read(self, year: int, stamp: int) -> tuple[Holiday, ...] | None:
        try:
            raw = await self._redis().get(HOLIDAY_KEY.format(year=year, stamp=stamp))
        except Exception:
            return None
        if not raw:
            return None
        try:
            return _decode(raw)
        except Exception:
            return None

    async def write(self, year: int, stamp: int, holidays: tuple[Holiday, ...]) -> None:
        try:
            await self._redis().set(
                HOLIDAY_KEY.format(year=year, stamp=stamp),
                _encode(holidays),
                ex=HOLIDAY_TTL_SECONDS,
            )
        except Exception:
            return


__all__ = ["HOLIDAY_KEY", "HOLIDAY_TTL_SECONDS", "HolidayCache"]
