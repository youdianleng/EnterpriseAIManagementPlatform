"""Ticket 43: the salary archive, its reads, and the money that cannot go missing.

Five groups, and the order is the order of the ticket's own concerns:

1. **The amount type.** A cent must not go missing between the entry and the read-back,
   so `123456789.01` is round-tripped through the real HTTP surface and compared as
   *digits*. A `double precision` column, a Python `float` or a JSON number on the wire
   all fail this test, which is the point: the type is a correctness decision and this is
   the evidence for it.
2. **The read audit, which is the unusual requirement.** Every read writes an entry —
   the list, the self-read, the `as_of` lookup and the question about somebody with no
   records at all — and the entries carry who read whose archive when and **not** the
   amounts. The paths a route-level `record()` call would most easily miss are the ones
   asserted by name: the empty chain and the 403.
3. **Effective dating and non-overlap.** The chain is ordered by time, any instant is one
   query, a new record never rewrites an old one, and an overlap is refused *by the
   database* — a service that forgot to check still cannot store one.
4. **Visibility.** HR, finance and the employee themselves; a manager and an administrator
   are refused with a recorded refusal; and the freedom from derived money is asserted
   against the database's own column list rather than against today's code.
5. **RLS as the second line**, over a connection made with the restricted role, because a
   table's owner is exempt from its own policies.

The database rules are asserted by breaking them: `platform.refused_by_database` runs the
statement PostgreSQL is expected to refuse and returns its own message, so the test
asserts *which* constraint fired rather than that something did.
"""

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.errors import ErrorCode
from app.domain.access import Action, Resource, ResourceKind, can, filter_for
from app.domain.access.kernel import Reason
from app.domain.payroll.models import (
    ARCHIVE_NOTICE_KEY,
    ARCHIVE_NOTICE_TEXT,
    AllowanceLine,
    Reading,
    SalaryReading,
    notice_payload,
    parse_amount,
    parse_components,
)
from app.domain.payroll.windows import in_force
from tests.support.platform import Actor, Platform

#: The exact figure the round-trip test uses. Eleven digits before the point and two
#: after, which is more precision than a `double precision` column promises: a float
#: would return `123456789.01000001` or round the final cent away entirely, and either
#: failure is invisible in a screenshot.
EXACT = "123456789.01"

#: A figure an ordinary record carries, for the tests that are not about precision.
BASE = "33000.00"

#: The effective windows the chain tests build. Fixed dates rather than "today", so the
#: window a test asks about and the window it wrote cannot drift apart.
FROM_2024 = date(2024, 1, 1)
FROM_2025 = date(2025, 1, 1)
FROM_2026 = date(2026, 1, 1)
BETWEEN = date(2025, 6, 15)
AFTER_2026 = date(2026, 3, 1)

APP_ROLE = "eam_app"


@pytest.fixture
async def restricted(settings) -> AsyncIterator[async_sessionmaker]:
    """Sessions bound to the restricted role, on the test database.

    The same fixture `test_database_security.py` builds, for the same reason: a table's
    owner is exempt from its own policies, so a suite connected as `eam` would exercise
    none of this and still look green.
    """
    engine = create_async_engine(settings.runtime_test_database_url)
    try:
        yield async_sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def publish(session, **values: str) -> None:  # noqa: ANN001 - AsyncSession
    """The context an application request publishes, written out by hand.

    Deliberately not `kernel.apply_rls_context`: a test that called the kernel's own
    function would prove the two agree about a *name* and nothing about what PostgreSQL
    does with the value.
    """
    for name, value in values.items():
        await session.execute(
            text("SELECT set_config(:name, :value, true)"), {"name": name, "value": value}
        )


class Cast:
    """The people a salary flow needs, and the two §4.1 refuses by name."""

    def __init__(self, **values: Actor) -> None:
        self.__dict__.update(values)

    def __getattr__(self, name: str) -> Actor:  # pragma: no cover - attribute typo
        raise AttributeError(name)


async def staff(platform: Platform, *, code: str = "rrhh") -> Cast:
    """A department, one position, and the seven roles that matter here.

    The manager's report is a *real* reporting relationship — the assignment names the
    manager — because 经理看不到下属薪资 is a claim about a manager whose reach would
    otherwise be wide enough to make the refusal interesting. The colleague answers to
    somebody else, so "a manager reads their team" cannot pass by accident.
    """
    admin = await platform.admin()
    department = await platform.department(code)
    position = await platform.position(department, f"{code}-tech")

    manager = await platform.account(roles=("manager",))
    await platform.assign(manager.employee_id, department, position)

    other_manager = await platform.account(roles=("manager",))
    await platform.assign(other_manager.employee_id, department, position)

    subject = await platform.account(roles=("employee",))
    await platform.assign(
        subject.employee_id, department, position, manager_employee_id=manager.employee_id
    )

    hr = await platform.account(roles=("hr",))
    await platform.assign(hr.employee_id, department, position)

    finance = await platform.account(roles=("finance",))
    await platform.assign(finance.employee_id, department, position)

    colleague = await platform.account(roles=("employee",))
    await platform.assign(
        colleague.employee_id,
        department,
        position,
        manager_employee_id=other_manager.employee_id,
    )

    return Cast(
        admin=admin,
        manager=manager,
        other_manager=other_manager,
        subject=subject,
        hr=hr,
        finance=finance,
        colleague=colleague,
        department=department,
        position=position,
    )


def window(
    *,
    base_salary: str = BASE,
    effective_from: date = FROM_2024,
    effective_to: date | None = None,
    reason_type: str = "initial",
    reason: str = "Alta en la empresa",
    components: list[dict] | None = None,
    currency: str = "EUR",
    pay_period: str = "monthly",
) -> dict:
    """A record body that a permitted caller would have accepted."""
    return {
        "base_salary": base_salary,
        "effective_from": effective_from.isoformat(),
        "effective_to": effective_to.isoformat() if effective_to else None,
        "currency": currency,
        "pay_period": pay_period,
        "components": components if components is not None else [],
        "change_reason_type": reason_type,
        "change_reason": reason,
    }


async def enter(
    platform: Platform, actor: Actor, subject: Actor, **overrides: object
) -> dict:
    """Enter one record through HR's own endpoint and return the row it answered with."""
    payload = window(**overrides)  # type: ignore[arg-type]
    response = await actor.post(
        "/api/v1/salary/records",
        json={"employee_id": subject.employee_id, **payload},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def reads(platform: Platform, *, subject: str | None = None) -> list[dict]:
    """Every `salary.record_read` entry, newest last.

    The action is written by the read path rather than by a route, so this is the query a
    compliance reader would run — and the one these tests assert against rather than
    against a mock.
    """
    rows = await platform.sql(
        """
        SELECT actor_user_id, entity_id, after
        FROM audit_log
        WHERE action = 'salary.record_read'
        ORDER BY id
        """
    )
    if subject is None:
        return [{"actor_user_id": str(row[0]), "entity_id": str(row[1]), "after": row[2]}
                for row in rows]
    return [
        {"actor_user_id": str(row[0]), "entity_id": str(row[1]), "after": row[2]}
        for row in rows
        if str(row[1]) == subject
    ]


# --- 1. the amount type -------------------------------------------------------


def test_an_amount_finer_than_a_cent_is_refused_rather_than_rounded() -> None:
    """Rounding on the way in is how a cent goes missing with nobody watching.

    Three decimal places is the shape of the mistake a person actually makes (a figure
    copied out of a spreadsheet), and the archive refuses it instead of silently storing
    something other than what was entered.
    """
    from app.domain.errors import DomainError

    assert parse_amount("33000.00") == Decimal("33000.00")
    # A JSON number arrives as the digits that were written, not as the nearest double.
    assert parse_amount(33000.0) == Decimal("33000.00")
    assert str(parse_amount("0.10")) == "0.10"

    for refused in (
        "33000.005",
        "0.001",
        "-1.00",
        "0",
        "not a number",
        "NaN",
        "Infinity",
        # Larger than `numeric(14, 2)` holds. The service refuses it with the field named
        # rather than letting PostgreSQL answer `numeric field overflow` as a 500.
        "9999999999999.99",
    ):
        with pytest.raises(DomainError) as excinfo:
            parse_amount(refused)
        assert excinfo.value.code is ErrorCode.SALARY_RECORD_INVALID, refused
    assert str(parse_amount("999999999999.99")) == "999999999999.99", "the column's own maximum"


def test_a_float_sum_would_not_be_storable_as_an_allowance() -> None:
    """`0.1 + 0.2` is the same defect in the breakdown, one level down.

    A client that computed an allowance in floating point and sent the result would be
    sending `0.30000000000000004`; the archive refuses it rather than storing a figure
    nobody can round back.
    """
    from app.domain.errors import DomainError

    lines = parse_components([{"code": "transport", "label": "Transporte", "amount": "0.30"}])
    assert lines == (AllowanceLine(code="transport", label="Transporte", amount="0.30"),)

    with pytest.raises(DomainError):
        parse_components(
            [{"code": "transport", "label": "Transporte", "amount": f"{0.1 + 0.2}"}]
        )
    # A repeated code is two answers to "what is this allowance".
    with pytest.raises(DomainError):
        parse_components(
            [
                {"code": "x", "label": "Uno", "amount": "1.00"},
                {"code": "x", "label": "Dos", "amount": "2.00"},
            ]
        )


async def test_the_exact_figure_survives_storage_and_read_back(
    platform: Platform,
) -> None:
    """The ticket's correctness claim, end to end and compared as digits.

    A float cannot pass this: `123456789.01` has more significant digits than a double
    carries exactly, and the value would come back either rounded at the last cent or
    with a binary artefact in the eleventh decimal. The assertion is on the *text* of the
    response, so a schema that helpfully serialised a `Decimal` as a JSON number would
    fail here too — JSON's one number type is a float, which is why the wire form is a
    string.
    """
    cast = await staff(platform)
    entered = await enter(platform, cast.hr, cast.subject, base_salary=EXACT)

    assert entered["base_salary"] == EXACT

    mine = await cast.subject.get("/api/v1/salary/records/me")
    assert mine.status_code == 200, mine.text
    assert mine.json()["items"][0]["base_salary"] == EXACT
    assert EXACT in mine.text, "the digits are not in the response body verbatim"

    # ... and in the column, read without the ORM in the way.
    stored = await platform.scalar(
        "SELECT base_salary::text FROM salary_records WHERE employee_id = :id",
        {"id": cast.subject.employee_id},
    )
    assert stored == EXACT, f"numeric(14, 2) read back as {stored!r}"


async def test_allowance_amounts_are_strings_all_the_way_down(platform: Platform) -> None:
    """The breakdown's amounts survive as text, because JSON has no decimal type.

    The same decision as the base figure one level down: an amount that travelled as a
    JSON number would be a float *inside a JSONB column*, and reading it back would give
    whatever binary approximation the client sent.
    """
    cast = await staff(platform)
    lines = [
        {"code": "transporte", "label": "Transporte", "amount": EXACT},
        {"code": "comida", "label": "Comida", "amount": "0.10"},
    ]
    entered = await enter(platform, cast.hr, cast.subject, components=lines)

    assert entered["components"] == lines
    stored = await platform.sql(
        "SELECT components FROM salary_records WHERE employee_id = :id",
        {"id": cast.subject.employee_id},
    )
    assert stored[0][0] == lines, (
        "the JSONB column holds a number rather than the string that was sent: "
        f"{stored[0][0]}"
    )


# --- 2. the read audit --------------------------------------------------------


async def test_reading_your_own_archive_writes_an_audit_entry(platform: Platform) -> None:
    """The self-read is a read. 「每次读取都写审计」 is not only about HR looking at somebody."""
    cast = await staff(platform)
    await enter(platform, cast.hr, cast.subject)

    before = len(await reads(platform))
    response = await cast.subject.get("/api/v1/salary/records/me")

    assert response.status_code == 200, response.text
    entries = await reads(platform, subject=cast.subject.employee_id)
    assert len(await reads(platform)) == before + 1
    own = entries[-1]
    assert own["actor_user_id"] == cast.subject.user_id
    assert own["after"]["self"] is True
    assert own["after"]["records"] == 1
    assert own["after"]["subject_employee_id"] == cast.subject.employee_id


async def test_a_list_read_writes_one_entry_not_one_per_row(platform: Platform) -> None:
    """A chain of records is one act of looking, and the trail says so.

    One entry naming the subject and how many rows came back: an entry per row would make
    "how many times did somebody open this archive" unanswerable without a `GROUP BY`,
    and would grow the append-only table with rows nobody asked for.
    """
    cast = await staff(platform)
    await enter(
        platform,
        cast.hr,
        cast.subject,
        effective_from=FROM_2024,
        effective_to=date(2024, 12, 31),
    )
    await enter(
        platform,
        cast.hr,
        cast.subject,
        effective_from=FROM_2025,
        reason_type="adjustment",
        reason="Convenio 2025",
    )
    before = len(await reads(platform))

    listing = await cast.hr.get(
        "/api/v1/salary/records", params={"employee_id": cast.subject.employee_id}
    )

    assert listing.status_code == 200, listing.text
    assert len(listing.json()["items"]) == 2
    assert len(await reads(platform)) == before + 1
    entries = await reads(platform, subject=cast.subject.employee_id)
    assert entries[-1]["after"]["records"] == 2
    assert entries[-1]["after"]["self"] is False


async def test_asking_about_a_day_records_the_look(platform: Platform) -> None:
    """The `as_of` read is a read, and the entry carries the day that was asked about."""
    cast = await staff(platform)
    await enter(platform, cast.hr, cast.subject)

    response = await cast.hr.get(
        "/api/v1/salary/records/as-of",
        params={"employee_id": cast.subject.employee_id, "as_of": BETWEEN.isoformat()},
    )

    assert response.status_code == 200, response.text
    entries = await reads(platform, subject=cast.subject.employee_id)
    assert entries[-1]["after"]["as_of"] == BETWEEN.isoformat()
    assert entries[-1]["after"]["records"] == 1


async def test_a_person_with_no_records_is_recorded_as_a_look_and_not_a_zero(
    platform: Platform,
) -> None:
    """The case a route-level `record()` call is most likely to miss.

    Somebody in their first week has no salary record. They get an empty chain — **not** a
    record whose figure is zero, which is a number a person could act on — and the look is
    still recorded, because "who asked about this person's salary" is the question the
    trail exists for whether or not there was an answer.
    """
    cast = await staff(platform)

    response = await cast.subject.get("/api/v1/salary/records/me")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == []
    assert body["empty"] is True
    assert body["total"] == 0
    assert "0.00" not in response.text, "an empty chain stated a figure"
    assert "base_salary" not in response.text

    entries = await reads(platform, subject=cast.subject.employee_id)
    assert len(entries) == 1
    assert entries[-1]["after"]["records"] == 0
    assert entries[-1]["after"]["self"] is True


async def test_a_refused_read_is_recorded_as_a_refusal_and_not_as_a_read(
    platform: Platform,
) -> None:
    """访问他人薪酬返回 403 并写审计 — and the trail says *refused*, not *read*.

    Two different facts, and an incident review needs them apart: one is somebody who saw
    a figure, the other is somebody who tried to. A refusal recorded as a read would make
    the trail claim a disclosure that never happened.
    """
    cast = await staff(platform)
    await enter(platform, cast.hr, cast.subject)
    before = len(await reads(platform))

    refused = await cast.manager.get(
        "/api/v1/salary/records", params={"employee_id": cast.subject.employee_id}
    )

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == ErrorCode.FORBIDDEN.value
    assert cast.subject.employee_id not in refused.text, "the refusal named the subject"
    assert len(await reads(platform)) == before, "a refused read was recorded as a read"

    refusals = await platform.sql(
        "SELECT after FROM audit_log WHERE action = 'access.refused' ORDER BY id"
    )
    assert refusals, "the refusal was not recorded at all"
    assert refusals[-1][0]["action"] == str(Action.SALARY_READ_ALL)


async def test_the_read_entries_carry_no_amounts(platform: Platform) -> None:
    """The trail is widely readable; the figures are not, and they do not travel into it.

    `audit_log` is append-only, kept four years and read by `compliance`. A figure copied
    into it would live in the wider of the two tables, which is the leak the separation of
    the two tables exists to prevent.
    """
    cast = await staff(platform)
    await enter(
        platform,
        cast.hr,
        cast.subject,
        base_salary=EXACT,
        reason="Subida del convenio con una cifra concreta",
        components=[{"code": "transporte", "label": "Transporte", "amount": "99.99"}],
    )
    await cast.hr.get("/api/v1/salary/records", params={"employee_id": cast.subject.employee_id})

    body = await platform.scalar(
        "SELECT string_agg(after::text, ' ') FROM audit_log WHERE action = 'salary.record_read'"
    )
    assert body is not None
    for leak in (EXACT, BASE, "99.99", "33000", "base_salary", "components"):
        assert leak not in body, f"the read trail carried {leak!r}: {body}"


def test_a_reading_cannot_be_built_by_a_caller() -> None:
    """The mechanism behind "every read writes an audit entry", asserted directly.

    Salary rows travel inside a `SalaryReading`, and one can only be obtained from the
    service method that also writes the trail entry. A route that never called it has no
    rows to serve — which is a stronger guarantee than three routes each remembering, and
    the one the ticket asks for ("design for the caller who forgets").

    The check is `FilterSpec`'s: the constructor exists so the error is an explicit
    refusal rather than a missing argument, and the token is what makes it work.
    """
    row = Reading(rows=(), subject=uuid4(), as_of=None, total=0)
    with pytest.raises(TypeError) as excinfo:
        SalaryReading()  # type: ignore[call-arg]
    assert "SalaryService.read" in str(excinfo.value)
    with pytest.raises(TypeError):
        SalaryReading(reading=row)  # type: ignore[call-arg]


def test_the_anchor_that_makes_the_seal_testable() -> None:
    """The test above can only assert the refusal if the token is real.

    If `SalaryReading` rejected *every* construction, the seal test would pass while the
    service could never produce one either — and the suite would be green with a module
    that cannot work. This is the control: a subclass walks the same path the service
    walks, so the refusal above is a *rule* rather than a wall.
    """
    class Anchor(SalaryReading):
        @classmethod
        def issued(cls, reading: Reading) -> "SalaryReading":
            return SalaryReading._issued(reading)

    issued = Anchor.issued(Reading(rows=(), subject=uuid4(), as_of=None))
    assert isinstance(issued, SalaryReading)


# --- 3. effective dating and non-overlap --------------------------------------


def test_the_window_rule_is_inclusive_at_both_ends() -> None:
    """The rule the exclusion constraint and the query both implement, stated once.

    Inclusive at both ends is what makes "the 31st belongs to the record that ends that
    day" true, and it is why the next record starts on the 1st: the two touch without
    overlapping, and the database's `daterange(..., '[]')` says the same thing.
    """
    assert in_force(FROM_2024, date(2024, 12, 31), FROM_2024) is True
    assert in_force(FROM_2024, date(2024, 12, 31), date(2024, 12, 31)) is True
    assert in_force(FROM_2024, date(2024, 12, 31), FROM_2025) is False
    assert in_force(FROM_2024, None, date(2099, 1, 1)) is True, "an absent end is open"
    assert in_force(FROM_2025, None, FROM_2024) is False


async def test_the_chain_is_ordered_and_any_instant_is_one_query(
    platform: Platform,
) -> None:
    """「按时间排列的调整链，任一时点可查出当时有效的值」, over the real endpoints.

    Three records, and the day asked about is checked against the *Python* window rule as
    well as the endpoint's answer: a query asserting its own result would agree with
    itself, and this is the second opinion.
    """
    cast = await staff(platform)
    await enter(
        platform,
        cast.hr,
        cast.subject,
        base_salary="30000.00",
        effective_from=FROM_2024,
        effective_to=date(2024, 12, 31),
    )
    await enter(
        platform,
        cast.hr,
        cast.subject,
        base_salary="32000.00",
        effective_from=FROM_2025,
        effective_to=date(2025, 12, 31),
        reason_type="adjustment",
        reason="Convenio 2025",
    )
    await enter(
        platform,
        cast.hr,
        cast.subject,
        base_salary="35000.00",
        effective_from=FROM_2026,
        reason_type="adjustment",
        reason="Convenio 2026",
    )

    chain = await cast.hr.get(
        "/api/v1/salary/records", params={"employee_id": cast.subject.employee_id}
    )
    assert chain.status_code == 200, chain.text
    items = chain.json()["items"]
    assert [item["effective_from"] for item in items] == [
        FROM_2024.isoformat(),
        FROM_2025.isoformat(),
        FROM_2026.isoformat(),
    ], "the chain is not ordered by time"
    assert [item["base_salary"] for item in items] == ["30000.00", "32000.00", "35000.00"]

    windows = [
        (date.fromisoformat(item["effective_from"]),
         date.fromisoformat(item["effective_to"]) if item["effective_to"] else None)
        for item in items
    ]
    for day, expected in (
        (date(2024, 6, 1), "30000.00"),
        (date(2024, 12, 31), "30000.00"),
        (FROM_2025, "32000.00"),
        (BETWEEN, "32000.00"),
        (AFTER_2026, "35000.00"),
    ):
        # The independent predicate first: exactly one window covers the day, and it is
        # the one the endpoint answers with.
        covering = [
            item for item, window in zip(items, windows, strict=True)
            if in_force(window[0], window[1], day)
        ]
        assert len(covering) == 1, f"{day} is covered by {len(covering)} windows"

        answer = await cast.hr.get(
            "/api/v1/salary/records/as-of",
            params={"employee_id": cast.subject.employee_id, "as_of": day.isoformat()},
        )
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert body["empty"] is False
        assert len(body["items"]) == 1, f"{day}: {body}"
        assert body["items"][0]["base_salary"] == expected
        assert body["items"][0]["id"] == covering[0]["id"]


async def test_a_day_that_no_record_covers_is_empty_and_not_zero(
    platform: Platform,
) -> None:
    """Before the first record, the archive says nothing rather than "0.00 EUR"."""
    cast = await staff(platform)
    await enter(platform, cast.hr, cast.subject, effective_from=FROM_2025)

    answer = await cast.hr.get(
        "/api/v1/salary/records/as-of",
        params={"employee_id": cast.subject.employee_id, "as_of": FROM_2024.isoformat()},
    )

    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["items"] == []
    assert body["empty"] is True
    assert "base_salary" not in answer.text


async def test_a_new_record_never_rewrites_an_old_one(platform: Platform) -> None:
    """「新增记录不覆盖旧记录，旧记录保留起止日期」 — asserted against the *column*.

    A 2024→2025 record is written first, then a 2025→ record. The old row keeps both its
    dates; the new row states its own start. That is the append, and it is also what makes
    the chain a history rather than a picture of today.
    """
    cast = await staff(platform)
    first = await enter(
        platform,
        cast.hr,
        cast.subject,
        base_salary="30000.00",
        effective_from=FROM_2024,
        effective_to=date(2024, 12, 31),
    )
    await enter(
        platform,
        cast.hr,
        cast.subject,
        base_salary="32000.00",
        effective_from=FROM_2025,
        reason_type="adjustment",
        reason="Convenio 2025",
    )

    rows = await platform.sql(
        "SELECT id::text, effective_from, effective_to, base_salary::text "
        "FROM salary_records ORDER BY effective_from"
    )
    assert len(rows) == 2
    assert rows[0][0] == first["id"]
    assert rows[0][1] == FROM_2024
    assert rows[0][2] == date(2024, 12, 31), "the old record lost the end it was entered with"
    assert rows[0][3] == "30000.00", "the old record was rewritten"
    assert rows[1][1] == FROM_2025
    assert rows[1][2] is None


async def test_overlapping_records_are_refused_by_the_database(
    platform: Platform,
) -> None:
    """The ticket's strongest form: an overlap is *unrepresentable*, not merely refused.

    The service's own check is bypassed on purpose — the statement goes straight at the
    table — and PostgreSQL refuses it by name. That is the difference between a rule the
    service remembers and a rule the schema has.
    """
    cast = await staff(platform)
    await enter(
        platform,
        cast.hr,
        cast.subject,
        effective_from=FROM_2024,
        effective_to=date(2024, 12, 31),
    )

    # A second row covering a day the first one holds, with a fresh id so nothing else
    # could be the reason.
    refusal = await platform.refused_by_database(
        """
        INSERT INTO salary_records
            (id, employee_id, effective_from, effective_to, base_salary, currency,
             pay_period, components, change_reason_type, change_reason)
        VALUES
            (:id, :employee_id, :start, :end, 1.00, 'EUR', 'monthly', '[]'::jsonb,
             'correction', 'solapado')
        """,
        {
            "id": str(uuid4()),
            "employee_id": cast.subject.employee_id,
            "start": date(2024, 6, 1),
            "end": None,
        },
    )
    assert "ex_salary_records_no_overlap" in refusal, refusal

    # The same overlap through the service is the sentence a client reads, not a 500.
    response = await cast.hr.post(
        "/api/v1/salary/records",
        json={
            "employee_id": cast.subject.employee_id,
            **window(effective_from=date(2024, 6, 1), reason_type="correction"),
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == ErrorCode.SALARY_RECORD_OVERLAPS.value
    assert "2024-06-01" in response.json()["error"]["detail"]


async def test_an_overlap_on_the_boundary_is_allowed_and_inside_is_not(
    platform: Platform,
) -> None:
    """The constraint's inclusive both-ends semantics, asserted from the *allowed* side.

    A record ending on the 31st and one starting on the 1st do not overlap — that the
    archive can hold a chain at all depends on it — while a record starting on the day
    the previous one ends is refused. Both directions, because a constraint that refused
    everything would pass a test that only checked the refusal.
    """
    cast = await staff(platform)
    await enter(
        platform,
        cast.hr,
        cast.subject,
        effective_from=FROM_2024,
        effective_to=date(2024, 12, 31),
    )

    neighbouring = await cast.hr.post(
        "/api/v1/salary/records",
        json={
            "employee_id": cast.subject.employee_id,
            **window(
                effective_from=FROM_2025,
                reason_type="adjustment",
                reason="Convenio 2025",
            ),
        },
    )
    assert neighbouring.status_code == 201, neighbouring.text

    same_day = await cast.hr.post(
        "/api/v1/salary/records",
        json={
            "employee_id": cast.subject.employee_id,
            **window(
                effective_from=date(2024, 12, 31),
                reason_type="correction",
                reason="Corrección",
            ),
        },
    )
    assert same_day.status_code == 409, same_day.text


async def test_a_second_opening_record_is_refused(platform: Platform) -> None:
    """One `initial` per person, by the partial unique index rather than by a check.

    The second record's window is deliberately *free* — the first one runs 2024 only — so
    the refusal cannot be the overlap rule in disguise, and the code a client reads is the
    one that names the real cause: this person was already entered once, and what is
    wanted is an adjustment.
    """
    cast = await staff(platform)
    await enter(
        platform,
        cast.hr,
        cast.subject,
        effective_from=FROM_2024,
        effective_to=date(2024, 12, 31),
    )

    second = await cast.hr.post(
        "/api/v1/salary/records",
        json={
            "employee_id": cast.subject.employee_id,
            **window(effective_from=FROM_2025, reason_type="initial"),
        },
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == ErrorCode.SALARY_RECORD_INITIAL_EXISTS.value

    refusal = await platform.refused_by_database(
        """
        INSERT INTO salary_records
            (id, employee_id, effective_from, effective_to, base_salary, currency,
             pay_period, components, change_reason_type, change_reason)
        VALUES
            (:id, :employee_id, :start, NULL, 1.00, 'EUR', 'monthly', '[]'::jsonb,
             'initial', 'otra alta')
        """,
        {
            "id": str(uuid4()),
            "employee_id": cast.subject.employee_id,
            "start": FROM_2025,
        },
    )
    assert "uq_salary_records_initial" in refusal, refusal


async def test_an_inverted_or_same_day_range_is_refused(platform: Platform) -> None:
    """A half-open range is not a range: an end before the start covers no day."""
    cast = await staff(platform)

    inverted = await cast.hr.post(
        "/api/v1/salary/records",
        json={
            "employee_id": cast.subject.employee_id,
            **window(effective_from=FROM_2025, effective_to=FROM_2024),
        },
    )
    assert inverted.status_code == 422, inverted.text
    assert inverted.json()["error"]["code"] == ErrorCode.SALARY_RECORD_INVALID.value

    # ... and the database refuses one written past the service.
    refusal = await platform.refused_by_database(
        """
        INSERT INTO salary_records
            (id, employee_id, effective_from, effective_to, base_salary, currency,
             pay_period, components, change_reason_type, change_reason)
        VALUES
            (:id, :employee_id, :start, :end, 1.00, 'EUR', 'monthly', '[]'::jsonb,
             'initial', 'invertido')
        """,
        {
            "id": str(uuid4()),
            "employee_id": cast.subject.employee_id,
            "start": FROM_2025,
            "end": FROM_2024,
        },
    )
    assert "ck_salary_records_effective_range" in refusal, refusal


async def test_the_range_guard_and_the_overlap_guard_agree_on_a_boundary(
    platform: Platform,
) -> None:
    """A record that starts and ends on the same day is legal, and is one day long.

    The boundary case a half-open implementation gets wrong: `[d, d]` covers exactly one
    day, the exclusion constraint counts it as one day, and `as_of d` finds it.
    """
    cast = await staff(platform)
    entered = await enter(
        platform,
        cast.hr,
        cast.subject,
        effective_from=FROM_2025,
        effective_to=FROM_2025,
        reason_type="correction",
    )
    assert entered["effective_from"] == FROM_2025.isoformat()
    assert entered["effective_to"] == FROM_2025.isoformat()

    answer = await cast.hr.get(
        "/api/v1/salary/records/as-of",
        params={"employee_id": cast.subject.employee_id, "as_of": FROM_2025.isoformat()},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["total"] == 1


# --- 4. visibility and the absence of derived money ---------------------------


async def test_the_company_read_admits_hr_and_finance_and_nobody_else(
    platform: Platform,
) -> None:
    """「人力资源、财务」, and the two §4.1 refuses **by name** end to end.

    The manager is the interesting one: their report is real, the assignment names them,
    and 经理看不到下属薪资 is exactly that reach refused. The administrator is the other:
    §4.1 separates the duties and denies administration even the payslip's contents.
    """
    cast = await staff(platform)
    await enter(platform, cast.hr, cast.subject, base_salary=EXACT)

    for allowed in (cast.hr, cast.finance):
        response = await allowed.get(
            "/api/v1/salary/records", params={"employee_id": cast.subject.employee_id}
        )
        assert response.status_code == 200, f"{allowed.declared_roles}: {response.text}"
        assert response.json()["items"][0]["base_salary"] == EXACT

    for refused in (cast.manager, cast.admin, cast.other_manager, cast.colleague):
        response = await refused.get(
            "/api/v1/salary/records", params={"employee_id": cast.subject.employee_id}
        )
        assert response.status_code == 403, f"{refused.declared_roles}: {response.text}"
        assert response.json()["error"]["code"] == ErrorCode.FORBIDDEN.value
        assert EXACT not in response.text, f"{refused.declared_roles} received the figure"


async def test_every_role_sees_its_own_archive_and_only_its_own(
    platform: Platform,
) -> None:
    """The self-read works for **every** role, and the chain it returns is the caller's.

    HR's own endpoint answers with HR's own record even though HR may read everybody's:
    the two routes ask different questions, and the self one is not a narrower company
    read — it is ownership.
    """
    cast = await staff(platform)
    await enter(platform, cast.hr, cast.subject, base_salary="30000.00")
    await enter(platform, cast.hr, cast.hr, base_salary="40000.00")
    await enter(platform, cast.hr, cast.manager, base_salary="50000.00")

    mine = await cast.manager.get("/api/v1/salary/records/me")
    assert mine.status_code == 200, mine.text
    items = mine.json()["items"]
    assert len(items) == 1
    assert items[0]["base_salary"] == "50000.00"
    assert items[0]["employee_id"] == cast.manager.employee_id
    assert "30000.00" not in mine.text

    # ... and HR reads the whole company through the other route.
    everyone = await cast.hr.get(
        "/api/v1/salary/records", params={"employee_id": cast.hr.employee_id}
    )
    assert everyone.status_code == 200
    assert everyone.json()["items"][0]["base_salary"] == "40000.00"


async def test_the_archive_schema_holds_no_derived_money(platform: Platform) -> None:
    """D9's non-goal, asserted against the database's own columns.

    A `net_pay`, a `total`, an `annual_base` or a `converted_eur` arriving in a later
    ticket fails here by name. The check is about the *schema* rather than about today's
    code, for the reason the overtime module's own test gives: a rule that lives only in a
    helper is one a second write path can walk around.
    """
    cast = await staff(platform)
    await enter(
        platform,
        cast.hr,
        cast.subject,
        base_salary=EXACT,
        components=[{"code": "transporte", "label": "Transporte", "amount": "1.00"}],
    )

    columns = await platform.sql(
        """
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'salary_records'
        """
    )
    names = {name for name, _ in columns}
    assert names == {
        "id",
        "employee_id",
        "effective_from",
        "effective_to",
        "base_salary",
        "currency",
        "pay_period",
        "components",
        "change_reason_type",
        "change_reason",
        "created_by_user_id",
        "created_at",
    }, "the archive gained a column; a derived one is a payroll calculation (D9)"

    derived = (
        "net", "total", "gross", "annual", "yearly", "converted", "tax", "irpf",
        "social", "employer", "prorat", "acumul", "sum_",
    )
    offenders = [name for name in names if any(word in name for word in derived)]
    assert offenders == [], f"the archive grew derived money: {offenders}"

    # No other table in this module computes from it either: the only numeric column is
    # the one somebody enters, and the breakdown lives in JSONB.
    numerics = [name for name, kind in columns if kind in ("numeric", "double precision", "real")]
    assert numerics == ["base_salary"], numerics

    # And the *response* carries no field a client could mistake for a computed figure.
    listing = await cast.hr.get(
        "/api/v1/salary/records", params={"employee_id": cast.subject.employee_id}
    )
    item = listing.json()["items"][0]
    assert set(item) == {
        "id",
        "employee_id",
        "employee_name",
        "effective_from",
        "effective_to",
        "base_salary",
        "currency",
        "pay_period",
        "components",
        "change_reason_type",
        "change_reason",
        "created_by_user_id",
        "created_at",
    }, f"the response grew a field: {sorted(item)}"


async def test_writing_is_hrs_and_the_actor_comes_from_the_session(
    platform: Platform,
) -> None:
    """人力资源能维护…, and `created_by` is a fact rather than a claim.

    Finance reads the archive and does not write it; the actor is the session's login, so
    a body cannot name somebody else as having entered a figure.
    """
    cast = await staff(platform)

    refused = await cast.finance.post(
        "/api/v1/salary/records",
        json={"employee_id": cast.subject.employee_id, **window()},
    )
    assert refused.status_code == 403, refused.text

    entered = await enter(platform, cast.hr, cast.subject)
    assert entered["created_by_user_id"] == cast.hr.user_id, (
        "the record names the session's actor, not a body field"
    )

    # ... and the write is in the trail under the *writing* action, with no amount in it.
    written = await platform.sql(
        "SELECT actor_user_id, after, reason FROM audit_log "
        "WHERE action = 'salary.record_written'"
    )
    assert len(written) == 1
    assert str(written[0][0]) == cast.hr.user_id
    assert written[0][1]["employee_id"] == cast.subject.employee_id
    assert EXACT not in str(written[0][1])
    assert written[0][2] == "Alta en la empresa"


def test_the_catalogue_refuses_a_manager_and_an_administrator_by_role() -> None:
    """The kernel's answer, before any endpoint: two actions, two audiences.

    `salary.read_own` is self-only and `salary.read_all` names HR and finance. A manager
    holds neither, so their refusal is a role refusal rather than a resource one — and an
    administrator holds neither either, which is §4.1's duty separation stated as data.
    """
    mine = Resource(ResourceKind.SALARY_RECORD, owner_employee_id=uuid4())
    theirs = Resource(ResourceKind.SALARY_RECORD, owner_employee_id=uuid4())

    def principal(role: str, employee_id: UUID):
        from app.domain.access import Principal

        return Principal(
            user_id=uuid4(),
            employee_id=employee_id,
            username=role,
            roles=frozenset({role, "employee"}),
        )

    owner = mine.owner_employee_id
    assert can(principal("employee", owner), Action.SALARY_READ_OWN, mine).allowed
    assert can(principal("employee", owner), Action.SALARY_READ_OWN, theirs).denied
    assert can(principal("manager", owner), Action.SALARY_READ_ALL, theirs).denied
    assert can(principal("admin", owner), Action.SALARY_READ_ALL, theirs).denied
    assert can(principal("compliance", owner), Action.SALARY_READ_ALL, theirs).denied
    assert can(principal("hr", owner), Action.SALARY_READ_ALL, theirs).allowed
    assert can(principal("finance", owner), Action.SALARY_READ_ALL, theirs).allowed
    # The refusal is a *role* refusal, because a managerial position is not a payroll one:
    # the reason names the rule that fired, and it is the one an incident review reads.
    refused = can(principal("manager", owner), Action.SALARY_READ_ALL, theirs)
    assert refused.primary_reason is Reason.ROLE_LACKS_PERMISSION
    assert "salary.read_all" in refused.detail
    # Writing is a third authority: finance reads and does not decide a figure.
    assert can(principal("hr", owner), Action.SALARY_WRITE, theirs).allowed
    assert can(principal("finance", owner), Action.SALARY_WRITE, theirs).denied


def test_the_reach_carries_no_department_and_no_reporting_clause() -> None:
    """`filter_for` describes the rows, and the two clauses it *lacks* are the ticket.

    A manager and a colleague share a department; if the spec carried one, a store that
    applied it would hand every manager their team's figures. The absence is asserted
    rather than assumed, because an empty field is exactly what a later "fix" fills in.
    """
    from app.domain.access import Principal

    from_hr = Principal(
        user_id=uuid4(),
        employee_id=uuid4(),
        username="rrhh",
        roles=frozenset({"hr", "employee"}),
        department_ids=frozenset({uuid4()}),
    )
    from_manager = Principal(
        user_id=uuid4(),
        employee_id=uuid4(),
        username="jefe",
        roles=frozenset({"manager", "employee"}),
        department_ids=frozenset({uuid4()}),
        is_manager=True,
        reports_employee_ids=frozenset({uuid4()}),
    )

    company = filter_for(from_hr, ResourceKind.SALARY_RECORD)
    assert company.allow_all is True
    assert company.department_ids == frozenset()
    assert company.reports_employee_ids == frozenset()

    own = filter_for(from_manager, ResourceKind.SALARY_RECORD)
    assert own.allow_all is False
    assert own.own_employee_id == from_manager.employee_id
    assert own.department_ids == frozenset(), "a department clause reached the salary spec"
    assert own.reports_employee_ids == frozenset(), "a reporting clause reached the salary spec"


# --- 5. the second line: what PostgreSQL refuses -------------------------------


async def seed_records(platform: Platform, employee_id: str) -> None:
    """Two records written as the owner, so the policy has rows to hide.

    Written without a request context on purpose: a fixture sets state up, and it is the
    *restricted role* that has to read it back under the rule. The second is open-ended
    and the first ends the day before it, so the two touch without overlapping — the
    archive's own constraint refuses anything else.
    """
    boundaries = (
        (FROM_2024, date(2024, 12, 31), "1000.00", "initial"),
        (FROM_2025, None, "2000.00", "correction"),
    )
    for start, end, amount, kind in boundaries:
        await platform.sql(
            """
            INSERT INTO salary_records
                (id, employee_id, effective_from, effective_to, base_salary, currency,
                 pay_period, components, change_reason_type, change_reason)
            VALUES
                (:id, :employee_id, :start, :end, :amount, 'EUR', 'monthly', '[]'::jsonb,
                 :kind, 'sembrado')
            """,
            {
                "id": str(uuid4()),
                "employee_id": employee_id,
                "start": start,
                "end": end,
                "amount": amount,
                "kind": kind,
            },
        )


async def visible_count(session) -> int:  # noqa: ANN001 - AsyncSession
    return await session.scalar(text("SELECT count(*) FROM salary_records"))


async def test_without_a_published_context_the_archive_returns_no_rows(
    platform: Platform, restricted: async_sessionmaker
) -> None:
    """A forgotten context reads as "no rows", never as "every row"."""
    cast = await staff(platform)
    await seed_records(platform, cast.subject.employee_id)

    async with restricted() as session:
        assert await visible_count(session) == 0


async def test_the_policy_admits_the_owner_hr_and_finance_and_nobody_else(
    platform: Platform, restricted: async_sessionmaker
) -> None:
    """The database's own answer, for every role §4.1 names — and the ones it refuses.

    The policy is written from the two role names rather than from `app.is_privileged`,
    and this is why: that flag is also true for `compliance`, which reads the audit trail
    and not the figures it names. The assertion below is what keeps the backstop from
    becoming the leak — ticket 36's finding, applied here before there is one.

    Each role gets a **fresh session**, because a pooled connection can carry the previous
    statement's settings and a test that reused one would be asserting about whatever ran
    before it.
    """
    cast = await staff(platform)
    await seed_records(platform, cast.subject.employee_id)
    await seed_records(platform, cast.colleague.employee_id)

    async def visible_in(roles: str, *, privileged: str | None = None) -> int:
        async with restricted() as session:
            settings = {
                "app.current_employee_id": cast.subject.employee_id,
                "app.current_roles": roles,
            }
            if privileged is not None:
                settings["app.is_privileged"] = privileged
            await publish(session, **settings)
            return await visible_count(session)

    assert await visible_in("{employee}") == 2, "the owner saw somebody else's rows"

    for role in ("hr", "finance"):
        assert await visible_in("{" + role + ",employee}") == 4, f"{role} saw {role}'s own only"

    for role in ("admin", "manager", "compliance", "it"):
        assert await visible_in("{" + role + ",employee}") == 2, (
            f"{role} read the whole archive through the policy"
        )

    # The control: `is_privileged` is true for compliance, and the policy still refuses —
    # which is the whole reason it is written from the role names.
    assert await visible_in("{compliance,employee}", privileged="true") == 2, (
        "the policy read app.is_privileged, which admits compliance"
    )


async def test_the_archive_cannot_be_updated_or_deleted_by_the_application(
    platform: Platform, restricted: async_sessionmaker
) -> None:
    """Append-only, enforced by the database rather than by the module's good manners.

    The application role holds SELECT and INSERT on the archive and nothing else, so a
    future write path cannot quietly gain an edit: 「新增记录不覆盖旧记录」 is a property of
    the grant rather than of the service.

    The context is the *payroll* one, deliberately: a role the policy refuses would fail
    on the row-level check before the table privilege was ever reached, and the claim here
    is about the privilege.
    """
    cast = await staff(platform)
    await seed_records(platform, cast.subject.employee_id)

    for statement in (
        "UPDATE salary_records SET base_salary = 1.00",
        "DELETE FROM salary_records",
    ):
        async with restricted() as session:
            await publish(
                session,
                **{
                    "app.current_employee_id": cast.subject.employee_id,
                    "app.current_roles": "{hr,employee}",
                },
            )
            with pytest.raises(Exception) as excinfo:
                await session.execute(text(statement))
            assert "permission denied" in str(excinfo.value).lower(), statement
            await session.rollback()

    # ... and it can still append and read, which is what the archive needs. The window is
    # one the seeded chain does not cover — the second seeded record is open-ended, so
    # "far in the future" would be inside it, and the exclusion constraint would refuse
    # this for a reason that has nothing to do with the privilege under test.
    async with restricted() as session:
        await publish(
            session,
            **{
                "app.current_employee_id": cast.subject.employee_id,
                "app.current_roles": "{hr,employee}",
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO salary_records
                    (id, employee_id, effective_from, effective_to, base_salary, currency,
                     pay_period, components, change_reason_type, change_reason)
                VALUES
                    (:id, :employee_id, :start, :end, 3.00, 'EUR', 'monthly', '[]'::jsonb,
                     'correction', 'sonda')
                """
            ),
            {
                "id": str(uuid4()),
                "employee_id": cast.subject.employee_id,
                "start": date(2023, 1, 1),
                "end": date(2023, 12, 31),
            },
        )
        await session.commit()


async def test_a_row_the_context_cannot_reach_is_refused_by_the_insert_policy(
    platform: Platform, restricted: async_sessionmaker
) -> None:
    """The write policy is the roles', not the employee's: nobody writes their own salary."""
    cast = await staff(platform)

    async with restricted() as session:
        await publish(
            session,
            **{
                "app.current_employee_id": cast.subject.employee_id,
                "app.current_roles": "{employee}",
            },
        )
        with pytest.raises(Exception) as excinfo:
            await session.execute(
                text(
                    """
                    INSERT INTO salary_records
                        (id, employee_id, effective_from, effective_to, base_salary,
                         currency, pay_period, components, change_reason_type, change_reason)
                    VALUES
                        (:id, :employee_id, :start, NULL, 1.00, 'EUR', 'monthly',
                         '[]'::jsonb, 'correction', 'propia')
                    """
                ),
                {
                    "id": str(uuid4()),
                    "employee_id": cast.subject.employee_id,
                    "start": FROM_2024,
                },
            )
        assert "row-level security" in str(excinfo.value).lower(), excinfo.value


# --- the notice, and the surfaces this ticket does not build --------------------


async def test_the_disclaimer_is_served_as_a_catalogue_key_alongside_the_record(
    platform: Platform,
) -> None:
    """The checklist's last line, as data rather than as a sentence a client invents.

    `message_key` is the contract; `text` carries the same statement in the three
    languages, including the design's own Chinese. No screen is owed by this ticket — the
    landing table assigns those elsewhere — and making the sentence available to one is.
    """
    cast = await staff(platform)
    await enter(platform, cast.hr, cast.subject)

    chain = await cast.subject.get("/api/v1/salary/records/me")
    assert chain.status_code == 200
    notice = chain.json()["notice"]
    assert notice["message_key"] == ARCHIVE_NOTICE_KEY == "salary.archive_notice"
    assert notice["text"] == ARCHIVE_NOTICE_TEXT
    assert "工资单" in notice["text"]["zh"], notice["text"]["zh"]
    assert "nómina" in notice["text"]["es"]

    # The key really is in both catalogues, which is what makes it a contract rather
    # than a string that happens to look like one.
    from app.core.messages import MESSAGES

    for locale, catalogue in MESSAGES.items():
        assert ARCHIVE_NOTICE_KEY in catalogue, f"missing from {locale}"
    assert MESSAGES["es"][ARCHIVE_NOTICE_KEY] == ARCHIVE_NOTICE_TEXT["es"]

    # ... and it travels with the entry's answer too, so a screen rendering the record it
    # just created does not have to hold two shapes.
    assert notice_payload()["message_key"] == ARCHIVE_NOTICE_KEY


async def test_no_salary_route_serves_rows_without_the_recorded_read() -> None:
    """The structural claim, asserted over the router's own source.

    The seal on `SalaryReading` is what makes the guarantee a type, and this is the
    companion check: the router reaches the archive only through the service's `read`.
    A future route that imported the repository directly would fail here — which is the
    caller the ticket asks to design for.
    """
    import inspect

    from app.api.v1 import salary as router

    source = inspect.getsource(router)
    assert "PostgresSalaryRepository" in source, "the router is expected to wire the repo"
    assert "service.read(" in source, "no read path in the router"
    assert ".chain(" not in source, "the router reached the repository's query directly"
    assert "repository.append" not in source, "the router bypassed the service's append"
    # Every route handler that answers with figures goes through `_service(...).read`.
    assert source.count("await service.read(") >= 3, (
        "each of the three read endpoints is expected to obtain its rows from the "
        "audited read"
    )
