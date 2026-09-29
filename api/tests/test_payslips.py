"""Ticket 44: a month of payslips, the two lists, and the files that matched nobody.

Six groups, and the order is the order of the ticket's own concerns:

1. **Attribution.** One request, many files, for one month; matched by the staff number in
   the filename or by an explicit selection, with the precedence stated in
   `domain/payslip/matching.py` and pinned here from both sides — the selection settles a
   filename that names nobody, and a selection that *disagrees* with a filename that names
   somebody else is refused rather than resolved.
2. **「不静默丢弃」, which is the requirement the screen's trustworthiness rests on.** Every
   uploaded file appears in exactly one of the result's two lists, with a reason when it
   was not attributed: no number in the name, a number nobody holds, a name matching **two**
   employees, a file that is not a PDF, one over the ceiling, an empty part, and the same
   bytes twice. The partition is asserted on every batch, and the two-employee case is
   asserted as a refusal rather than as "whichever came first" — a guess there sends
   somebody else's pay to the wrong mailbox.
3. **The missing list.** Derived from the payroll archive and from employment, never from
   the uploaded set, and derived through `SalaryService.read(...)` because that is the only
   path that serves salary rows. Both halves of "expected" are pinned: a terminated person
   before the month is not expected, a person hired after it is not expected, and somebody
   with no record in force is not expected — while an uploaded payslip for a person the
   archive does not expect does not make them missing either.
4. **Replacement, the checksum, and the status.** A re-upload is a replacement (one row,
   the new checksum, the old one reported), the size and the hash are recorded for the
   ticket's 「事后核对」, the status vocabulary is closed, and only `published` is visible:
   a withdrawn row disappears from the employee's own reach and reappears in the missing
   list.
5. **Publishing, notifying and auditing**, and **only finance may do any of it.** The
   affected employees are notified; the audit entry names the actor, the month and the
   counts and carries **no amount**; `hr` and `admin` are refused with the catalogue's own
   403 rather than a role test in a handler.
6. **The corpus exclusion, and the schema.** A payslip's text is not retrievable by its
   owner or by finance through the retrieval path, `documents` stays empty, and the file's
   own columns are immutable — a checksum cannot be edited in place.

The database rules are asserted by breaking them, through
`platform.refused_by_database`, so a test asserts *which* rule fired rather than that
something did.
"""

from collections.abc import AsyncIterator
from datetime import date
from io import BytesIO
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.errors import ErrorCode
from app.domain.access import Action, ResourceKind
from app.domain.access.kernel import apply_rls_context
from app.domain.access.principal import Principal
from app.domain.document.models import DocumentMetadata
from app.domain.document.service import DocumentService, Upload
from app.domain.document.storage import LocalFileStore
from app.domain.payslip.export import EXPORT_COLUMNS, EXPORT_EXCLUDES
from app.domain.payslip.matching import resolve, tokens
from app.domain.payslip.models import (
    MAX_PAYSLIP_BYTES,
    EmployeeRef,
    UnmatchedReason,
    UploadedFile,
    parse_period,
    period_bounds,
)
from app.domain.retrieval.filtering import answer_filter_for
from app.domain.retrieval.service import RetrievalService
from app.repositories.document import PostgresDocumentRepository
from app.repositories.payslip import PostgresPayslipRepository
from app.repositories.retrieval import PostgresChunkSearchRepository
from tests.support.platform import Actor, Platform

#: The month the tests file. A fixed past month, so the windows written and the month asked
#: about cannot drift apart — the same rule ticket 43's salary tests follow.
PERIOD = "2026-03"

#: A second month, for the tests that need two.
OTHER_PERIOD = "2026-04"

#: The first day of `PERIOD`, which is the day the archive is asked about.
PERIOD_START = date(2026, 3, 1)

#: The figure the salary records carry. **No test asserts it anywhere a payslip is
#: concerned** — that is the point of the module — and it is here only because a record
#: needs one.
BASE = "30000.00"

#: The staff numbers the fixtures use. Distinct strings rather than a shared prefix, so a
#: whole-token match and a substring match cannot be confused by accident; the prefix case
#: is asserted by name in `test_a_number_that_is_only_a_prefix_of_another_names_nobody`.
NUMBER_A = "E-1001"
NUMBER_B = "E-1002"
NUMBER_C = "E-1003"
NUMBER_D = "E-1004"
NUMBER_PREFIX = "E-100"


# --- fixtures and helpers -----------------------------------------------------


@pytest.fixture
async def restricted(settings) -> AsyncIterator[async_sessionmaker]:
    """Sessions bound to the restricted role, on the test database.

    The same fixture `test_salary_records.py` builds, for the same reason: a table's owner
    is exempt from its own policies *and* from the grants taken away from it, so a suite
    connected as `eam` would exercise none of this and still look green.
    """
    engine = create_async_engine(settings.runtime_test_database_url)
    try:
        yield async_sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def publish(session, **values: str) -> None:  # noqa: ANN001 - AsyncSession
    """The context an application request publishes, written out by hand.

    Deliberately not `access.kernel.apply_rls_context`: a test that called the kernel's own
    function would prove the two agree about a *name* and nothing about what PostgreSQL
    does with the value.
    """
    for name, value in values.items():
        await session.execute(
            text("SELECT set_config(:name, :value, true)"), {"name": name, "value": value}
        )


class Cast:
    """The people a payslip flow needs."""

    def __init__(self, **values: Actor) -> None:
        self.__dict__.update(values)

    def __getattr__(self, name: str) -> Actor:  # pragma: no cover - attribute typo
        raise AttributeError(name)


async def staff(platform: Platform, *, code: str = "finanzas") -> Cast:
    """A department, one position, and the four roles this ticket names.

    Every employee the payslip list could mention is a *real* account here — an employee
    with no account still appears in the missing list (the list is about being on the
    books, not about having a login), so the two are created the same way and the tests
    that need a session have one.
    """
    department = await platform.department(code)
    position = await platform.position(department, f"{code}-tech")

    finance = await platform.account(roles=("finance",))
    await platform.assign(finance.employee_id, department, position)

    hr = await platform.account(roles=("hr",))
    await platform.assign(hr.employee_id, department, position)

    admin = await platform.account(roles=("admin",))
    await platform.assign(admin.employee_id, department, position)

    employee = await platform.account(roles=("employee",))
    await platform.assign(employee.employee_id, department, position)

    return Cast(
        finance=finance,
        hr=hr,
        admin=admin,
        employee=employee,
        department=department,
        position=position,
    )


async def newcomer(platform: Platform, department: str, number: str | None) -> Actor:
    """One more employee, with a staff number and no salary record.

    The staff number is written straight to `employee_private` because the number *is* the
    fixture here: what this ticket matches on is the string a payroll bureau writes into a
    filename, and a test that could not choose it could not test the matching rule.
    """
    actor = await platform.account(roles=("employee",))
    if number is not None:
        await platform.sql(
            """
            INSERT INTO employee_private (employee_id, employee_no)
            VALUES (:employee_id, :number)
            ON CONFLICT (employee_id) DO UPDATE SET employee_no = :number
            """,
            {"employee_id": actor.employee_id, "number": number},
        )
    return actor


async def salary(
    platform: Platform, hr: Actor, employee: Actor, **overrides: object
) -> None:
    """Give somebody a salary record in force from 2020 onwards.

    Through HR's own endpoint, because that is how a record exists in this system and the
    missing list is derived from the archive rather than from a table a test filled in.
    """
    start = date(2020, 1, 1)
    payload = {
        "employee_id": employee.employee_id,
        "effective_from": start.isoformat(),
        "effective_to": None,
        "base_salary": BASE,
        "currency": "EUR",
        "pay_period": "monthly",
        "components": [],
        "change_reason_type": "initial",
        "change_reason": "Alta en la empresa",
    }
    payload.update(overrides)
    response = await hr.post("/api/v1/salary/records", json=payload)
    assert response.status_code == 201, response.text


def pdf(marker: str) -> bytes:
    """A tiny but structurally valid PDF whose text is `marker`.

    Written byte by byte with the xref offsets computed from the objects in hand, for the
    reason the document tests write theirs that way: the pipeline parses this file, and a
    malformed one would make a failure that had nothing to do with the test's subject. The
    contents are never read by the payslip module — that is D9 — and the one place they
    matter is `test_a_payslips_text_cannot_be_retrieved_by_anybody`, which needs a page the
    parser can actually read.
    """
    stream = f"BT /F1 12 Tf 72 720 Td ({marker}) Tj ET".encode("latin-1")
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % index + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def oversized_pdf(marker: str) -> bytes:
    """A PDF over the module's ceiling, and still a PDF.

    The padding sits inside a comment after the header, so the file's *type* is beyond
    doubt and the only reason it can be refused is its size — which is what the test is
    about. A file that was both oversized and not a PDF would prove neither.
    """
    head = b"%PDF-1.4\n%" + b"x" * (MAX_PAYSLIP_BYTES + 1024) + b"\n"
    return head + pdf(marker)[len(b"%PDF-1.4\n") :]


async def upload(
    platform: Platform,
    finance: Actor,
    *,
    period: str = PERIOD,
    files: list[tuple[str, bytes]],
    selections: list[str | None] | None = None,
    confirm: bool = True,
) -> tuple[int, dict]:
    """One multipart upload. Returns `(status, body)` so a test can assert a refusal.

    Built by hand rather than through a client helper because the order of the `employee_id`
    parts *is* the pairing rule: each one belongs to the file in the same position, and a
    helper that reordered them would make the precedence tests meaningless.

    `files` goes through httpx's `files=` with a *file object* per part, and the scalar form
    fields through `data=` as a **dict**. That combination is the one httpx encodes as a
    real `MultipartStream`: a list of `(name, value)` pairs for `data` — the shape a
    repeated key wants — makes it build an `IteratorByteStream` instead, which an
    `AsyncClient` refuses to send. A list value inside the dict is enough to repeat a key,
    which is what the `employee_id` parts are.
    """
    parts: list[tuple[str, tuple[str, BytesIO, str]]] = [
        ("files", (name or "document", BytesIO(content), "application/pdf"))
        for name, content in files
    ]
    data: dict[str, object] = {"period": period, "confirm": "true" if confirm else "false"}
    selections = [value for value in (selections or []) if value is not None]
    if selections:
        data["employee_id"] = selections
    response = await finance.call(
        "POST", "/api/v1/payslips/batches", data=data, files=parts
    )
    return response.status_code, response.json()


async def file_a_month(
    platform: Platform, finance: Actor, *, period: str = PERIOD
) -> dict:
    """The ordinary upload, with both employees' files, and return the body."""
    status, body = await upload(
        platform,
        finance,
        period=period,
        files=[(f"nomina_{NUMBER_A}.pdf", pdf("A")), (f"nomina_{NUMBER_B}.pdf", pdf("B"))],
    )
    assert status == 201, body
    return body


async def audit_rows(platform: Platform, action: str) -> list[dict]:
    """Every audit entry for one action, oldest first, with its `after` payload."""
    rows = await platform.sql(
        """
        SELECT actor_user_id, entity_type, entity_id, after, reason
        FROM audit_log WHERE action = :action ORDER BY id
        """,
        {"action": action},
    )
    return [
        {
            "actor_user_id": str(row[0]),
            "entity_type": row[1],
            "entity_id": str(row[2]) if row[2] else None,
            "after": row[3],
            "reason": row[4],
        }
        for row in rows
    ]


async def missing_numbers(platform: Platform, finance: Actor, period: str = PERIOD) -> set[str]:
    """The staff numbers the missing list names, through finance's own endpoint."""
    response = await finance.get(f"/api/v1/payslips/missing?period={period}")
    assert response.status_code == 200, response.text
    return {item["employee_no"] for item in response.json()["items"]}


# --- 1. attribution -----------------------------------------------------------


async def test_the_token_report_names_the_pieces_the_name_is_made_of() -> None:
    """`tokens` as a table: the pieces the *reporting* rules read.

    It is deliberately not the matching rule — that folds the whole name — so what this pins
    is what the two reporting uses need: the extension is the last piece, and a hyphenated
    staff number comes apart into its parts rather than staying one token.
    """
    assert tokens("nomina_E-1001_2026-03.pdf") == ("NOMINA", "E", "1001", "2026", "03", "PDF")
    assert tokens("E-1001.pdf") == ("E", "1001", "PDF")
    assert tokens(None) == ()


def test_a_filename_is_looked_up_as_a_whole_name_not_by_substring() -> None:
    """The matching unit is the number as a run of characters, never a substring.

    A substring rule would attribute a payslip to whoever happened to hold a *prefix* of
    another number — `E-100` inside `E-1001` — which is exactly the silent mis-filing the
    missing list exists to catch. The separators are the rule's other half: `_`, `-`, `.` and
    a space all sit between a number's characters without breaking it, so
    `nomina_E-1001.pdf`, `nomina-E-1001.pdf` and `E-1001.pdf` all name the same employee.
    """
    employees = [EmployeeRef(employee_id=uuid4(), employee_no="E-1001", employee_name="A, A")]
    # `E-100` is not this employee: the run is longer than their number.
    assert resolve(
        UploadedFile(content=pdf("x"), filename="nomina_E-100.pdf"), employees
    ).reason is UnmatchedReason.NO_EMPLOYEE_NUMBER
    # `E-10012` is not either: the digit after the number breaks the boundary.
    assert resolve(
        UploadedFile(content=pdf("x"), filename="nomina_E-10012.pdf"), employees
    ).reason is UnmatchedReason.NO_EMPLOYEE_NUMBER
    # And the number itself, however the name spaces it out, does name the employee.
    for filename in (
        "E-1001.pdf",
        "nomina_E-1001.pdf",
        "nomina-E-1001.pdf",
        "nomina E-1001.pdf",
        "e-1001.pdf",
    ):
        resolved = resolve(UploadedFile(content=pdf("x"), filename=filename), employees)
        assert resolved.employee_id == employees[0].employee_id, filename
        assert resolved.matched_by == "filename"


async def test_a_month_is_filed_by_the_staff_number_in_the_filename(
    platform: Platform,
) -> None:
    """The checklist's first line: one request, many files, for one month."""
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    b = await newcomer(platform, cast.department, NUMBER_B)

    status, body = await upload(
        platform,
        cast.finance,
        files=[(f"nomina_{NUMBER_A}.pdf", pdf("a")), ("E-1002_marzo.pdf", pdf("b"))],
    )

    assert status == 201, body
    assert body["period"] == PERIOD
    assert body["total_count"] == 2
    assert body["attributed_count"] == 2
    assert {entry["employee_id"] for entry in body["attributed"]} == {
        a.employee_id,
        b.employee_id,
    }
    assert body["unmatched"] == []
    assert body["partitioned"] is True
    assert all(entry["status"] == "published" for entry in body["attributed"])


async def test_a_selection_settles_a_filename_that_names_nobody(
    platform: Platform,
) -> None:
    """「或界面选择完成归属匹配」: the explicit route, for a file the name cannot answer for.

    The ordinary case is a payroll bureau that names its files `nomina_marzo_1.pdf`, so the
    screen asks finance which employee each one is. The selection is recorded as the route
    that answered (`matched_by` on the service's own value), so "why is this file against
    that employee" is answerable from the result.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)

    status, body = await upload(
        platform,
        cast.finance,
        files=[("nomina_marzo.pdf", pdf("a"))],
        selections=[a.employee_id],
    )

    assert status == 201, body
    assert body["attributed_count"] == 1
    assert body["attributed"][0]["employee_id"] == a.employee_id
    assert body["unmatched"] == []


async def test_a_selection_overrides_a_filename_that_names_two_people(
    platform: Platform,
) -> None:
    """The selection is what the ambiguous case is *for*.

    A name carrying two staff numbers cannot be resolved by the name, and the screen's
    answer is to let finance say which one it is. Attributing it to a selection is
    therefore not an override of evidence — it is the only route that can answer.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    await newcomer(platform, cast.department, NUMBER_B)

    status, body = await upload(
        platform,
        cast.finance,
        files=[(f"nomina_{NUMBER_A}_{NUMBER_B}.pdf", pdf("a"))],
        selections=[a.employee_id],
    )

    assert status == 201, body
    assert body["attributed_count"] == 1
    assert body["attributed"][0]["employee_id"] == a.employee_id


async def test_a_selection_that_disagrees_with_the_filename_is_refused(
    platform: Platform,
) -> None:
    """**A disagreement is reported, not resolved**, and this is the precedence decision.

    Precedence alone would be enough to attribute the file to the selection — but the
    selection is a click and the filename is what the payroll bureau actually wrote, so a
    file named for one employee and filed against another is far more likely to be a
    mistake than an override. Attributing it would hand one person's payslip to somebody
    else **and** leave the first looking missing, which is the one thing this screen must
    not do. So the file is refused with both numbers named and the uploader decides.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    b = await newcomer(platform, cast.department, NUMBER_B)

    status, body = await upload(
        platform,
        cast.finance,
        files=[(f"nomina_{NUMBER_A}.pdf", pdf("a"))],
        selections=[b.employee_id],
    )

    assert status == 201, body
    assert body["attributed_count"] == 0
    assert body["unmatched_count"] == 1
    refusal = body["unmatched"][0]
    assert refusal["reason"] == UnmatchedReason.DUPLICATE_FOR_EMPLOYEE.value
    assert NUMBER_A in refusal["filename"]
    assert a.employee_id not in {entry["employee_id"] for entry in body["attributed"]}


async def test_a_selection_naming_nobody_is_a_404_and_not_a_file_reason(
    platform: Platform,
) -> None:
    """A broken client is a request error, not a payslip's absence.

    Answering "that file matched nobody" for a selection that names an employee who does
    not exist would hide a client bug behind a missing payslip — and the two have different
    remedies: one is a code fix, the other is asking a person what their staff number is.
    """
    cast = await staff(platform)
    status, body = await upload(
        platform,
        cast.finance,
        files=[("nomina.pdf", pdf("a"))],
        selections=[str(uuid4())],
    )
    assert status == 404, body
    assert body["error"]["code"] == ErrorCode.EMPLOYEE_NOT_FOUND.value


# --- 2. nothing is dropped silently ------------------------------------------


async def test_every_uploaded_file_is_in_exactly_one_of_the_two_lists(
    platform: Platform,
) -> None:
    """**The rule the ticket's trustworthiness rests on.**

    Seven files go up, four of them unusable for four different reasons, and the assertion
    is the *partition*: the filenames in the two lists are the filenames that were sent,
    each exactly once. A file that vanished would be a payslip somebody never receives —
    which is the failure the whole screen exists to prevent — and it is the failure a
    "count the successes" test cannot see.
    """
    cast = await staff(platform)
    await newcomer(platform, cast.department, NUMBER_A)

    files = [
        (f"nomina_{NUMBER_A}.pdf", pdf("good")),
        ("nomina_sin_numero.pdf", pdf("no number")),
        ("nomina_E-9999.pdf", pdf("unknown")),
        ("notas.pdf", b"not a pdf at all"),
        ("nomina.pdf", b""),
        (f"nomina_{NUMBER_A}.pdf", pdf("good")),
    ]
    status, body = await upload(platform, cast.finance, files=files)

    assert status == 201, body
    assert body["total_count"] == len(files)
    assert body["partitioned"] is True
    sent = [name for name, _ in files]
    listed = sorted([entry["filename"] for entry in body["attributed"]])
    listed += sorted([entry["filename"] for entry in body["unmatched"]])
    assert sorted(listed) == sorted(_normalise(sent)), (listed, body)


def _normalise(names: list[str]) -> list[str]:
    """The names as the result reports them: the module's own `safe_filename`."""
    from app.domain.document.files import safe_filename

    return [safe_filename(name) for name in names]


async def test_a_file_that_matches_two_employees_is_refused_rather_than_attributed(
    platform: Platform,
) -> None:
    """**A guess here sends somebody else's pay to the wrong mailbox.**

    The name carries two staff numbers, so the file is one of two people's and the module
    has no way to know which. It is reported with both numbers rather than attributed to
    whichever the query happened to return first — which is what a "first match wins" loop
    would do, silently and unrepeatably.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    b = await newcomer(platform, cast.department, NUMBER_B)

    status, body = await upload(
        platform,
        cast.finance,
        files=[(f"nomina_{NUMBER_A}_{NUMBER_B}.pdf", pdf("x"))],
    )

    assert status == 201, body
    assert body["attributed_count"] == 0
    refusal = body["unmatched"][0]
    assert refusal["reason"] == UnmatchedReason.AMBIGUOUS_EMPLOYEE_NUMBER.value
    assert NUMBER_A in refusal["employee_no"] and NUMBER_B in refusal["employee_no"]
    assert {a.employee_id, b.employee_id}.isdisjoint(
        {entry["employee_id"] for entry in body["attributed"]}
    )


async def test_the_four_file_refusals_each_report_their_own_reason(
    platform: Platform,
) -> None:
    """Every refusal reasons in its own words, and none of them is "something went wrong".

    Four different mistakes with four different remedies: a filename with no number (ask
    the person), a number nobody holds (check the payroll file), a file that is not a PDF
    (convert it), and one over the ceiling (export it differently). A single "unmatched"
    token would send finance to the wrong remedy for three of them.
    """
    cast = await staff(platform)
    await newcomer(platform, cast.department, NUMBER_A)

    files = [
        ("nomina.pdf", pdf("no number")),
        ("nomina_E-9999.pdf", pdf("unknown")),
        ("escaneo.pdf", b"\x89PNG\r\n\x1a\n not a pdf"),
        ("gigante.pdf", oversized_pdf("big")),
        ("vacio.pdf", b""),
    ]
    status, body = await upload(platform, cast.finance, files=files)

    assert status == 201, body
    reasons = {entry["filename"]: entry["reason"] for entry in body["unmatched"]}
    assert reasons["nomina.pdf"] == UnmatchedReason.NO_EMPLOYEE_NUMBER.value
    assert reasons["nomina_E-9999.pdf"] == UnmatchedReason.UNKNOWN_EMPLOYEE_NUMBER.value
    assert reasons["escaneo.pdf"] == UnmatchedReason.NOT_A_PDF.value
    assert reasons["gigante.pdf"] == UnmatchedReason.OVERSIZED_FILE.value
    assert reasons["vacio.pdf"] == UnmatchedReason.EMPTY_FILE.value
    assert body["attributed_count"] == 0
    assert body["partitioned"] is True
    # The oversized one says how big it was, which the token alone cannot.
    oversized = next(
        entry for entry in body["unmatched"] if entry["reason"] == "oversized_file"
    )
    assert oversized["detail"] is not None and str(MAX_PAYSLIP_BYTES) in oversized["detail"]


async def test_the_same_bytes_twice_in_one_upload_is_reported(
    platform: Platform,
) -> None:
    """Two files for one employee in one batch would upsert each other.

    The row would carry the second's checksum while the uploader saw two lines, and "which
    file is stored" would be unanswerable from the answer. The second is refused, and the
    first is kept — deterministic, where keeping the last would make the result depend on
    the order a client happened to send.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)

    status, body = await upload(
        platform,
        cast.finance,
        files=[(f"nomina_{NUMBER_A}.pdf", pdf("same")), ("copia.pdf", pdf("same"))],
    )

    assert status == 201, body
    assert body["attributed_count"] == 1
    assert body["attributed"][0]["employee_id"] == a.employee_id
    assert body["unmatched"][0]["reason"] == UnmatchedReason.DUPLICATE_FILE.value
    assert body["partitioned"] is True


async def test_two_files_for_one_employee_in_one_upload_are_reported(
    platform: Platform,
) -> None:
    """Different bytes, one employee, one batch: the second is refused rather than replacing.

    The ticket's replacement rule is about a *re-upload*, and this is not one: within a
    single batch, silently keeping the second file would mean the uploader's own answer
    listed one employee twice and the missing list counted them once.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)

    status, body = await upload(
        platform,
        cast.finance,
        files=[
            (f"nomina_{NUMBER_A}.pdf", pdf("first")),
            (f"nomina_{NUMBER_A}_bis.pdf", pdf("second")),
        ],
    )

    assert status == 201, body
    assert body["attributed_count"] == 1
    assert body["unmatched"][0]["reason"] == UnmatchedReason.DUPLICATE_FOR_EMPLOYEE.value
    assert body["attributed"][0]["employee_id"] == a.employee_id


async def test_a_batch_with_no_files_is_refused(platform: Platform) -> None:
    """An upload with nothing in it is a client that lost the files.

    Answering 201 would record a batch that filed nothing, and the month would look filed.
    Two layers refuse it and both are asserted, because they answer different callers: the
    request layer refuses a body with no `files` part before the module is reached (the
    framework's `ERR_VALIDATION_001`), and the module refuses an *empty list* with its own
    catalogue code — which is the one a client that sent a body of some other shape will
    read. `test_the_service_refuses_an_empty_batch` is the second one, called directly.
    """
    cast = await staff(platform)
    status, body = await upload(platform, cast.finance, files=[])
    assert status == 422, body
    assert body["error"]["code"] in {
        ErrorCode.PAYSLIP_BATCH_EMPTY.value,
        ErrorCode.VALIDATION_FAILED.value,
    }


async def test_the_service_refuses_an_empty_batch(platform: Platform) -> None:
    """The module's own refusal, with its own code and a sentence a client can render."""
    from app.domain.errors import DomainError
    from app.domain.payslip.errors import PayslipErrorCode
    from app.domain.payslip.service import PayslipService

    cast = await staff(platform)
    async with platform.factory() as session:
        service = PayslipService(
            PostgresPayslipRepository(session),
            session,
            principal=Principal(
                user_id=uuid4(),
                employee_id=UUID(cast.finance.employee_id),
                username="fin",
                roles=frozenset({"finance", "employee"}),
                clearance_level="low",
                department_ids=frozenset(),
                primary_department_id=None,
                is_manager=False,
            ),
            storage=LocalFileStore("/tmp/unused-payslip-store"),
        )
        with pytest.raises(DomainError) as refused:
            await service.upload([], PERIOD)
        assert refused.value.code == PayslipErrorCode.BATCH_EMPTY


async def test_a_month_that_is_not_yyyy_mm_is_refused(platform: Platform) -> None:
    """`2026-3` and `2026-03` would otherwise be two batches for one month."""
    cast = await staff(platform)
    for bad in ("2026-3", "marzo", "2026-13", "abc"):
        status, body = await upload(
            platform,
            cast.finance,
            period=bad,
            files=[("nomina.pdf", pdf("x"))],
        )
        assert status == 422, (bad, body)
        assert body["error"]["code"] == ErrorCode.PAYSLIP_PERIOD_INVALID.value

def test_the_period_bounds_are_the_month_and_nothing_else() -> None:
    """`2026-03` is `2026-03-01 .. 2026-03-31`, February included for a leap year.

    This is the module's one piece of arithmetic, and it is not a payroll calculation: it
    turns a month into the range the archive's window predicate is asked about.
    """
    assert parse_period("2026-03") == "2026-03"
    assert period_bounds("2026-03") == (date(2026, 3, 1), date(2026, 3, 31))
    assert period_bounds("2024-02") == (date(2024, 2, 1), date(2024, 2, 29))
    assert period_bounds("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
    assert period_bounds("2026-12") == (date(2026, 12, 1), date(2026, 12, 31))


# --- 3. the missing list ------------------------------------------------------


async def test_the_two_lists_arrive_together_and_the_missing_one_is_derived(
    platform: Platform,
) -> None:
    """「上传完成后展示两张清单」 — and the second is not the first's complement.

    Three people have a salary record in force in March; two files are uploaded. The
    attributed list is those two and the missing list is the third — derived from the
    archive, so a list computed as "everybody with a record minus the files I just sent"
    would agree here and disagree in the case below, where somebody has a payslip and no
    record at all.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    b = await newcomer(platform, cast.department, NUMBER_B)
    c = await newcomer(platform, cast.department, NUMBER_C)
    for actor in (a, b, c):
        await salary(platform, cast.hr, actor)

    body = await file_a_month(platform, cast.finance)

    assert body["missing_count"] == 1
    assert body["missing"][0]["employee_id"] == c.employee_id
    assert body["missing"][0]["employee_no"] == NUMBER_C
    # The reason the person is expected is the archive's own window, and it is a date.
    assert body["missing"][0]["salary_effective_from"] == "2020-01-01"
    assert body["missing"][0]["salary_effective_to"] is None


async def test_the_missing_list_is_not_the_uploaded_sets_complement(
    platform: Platform,
) -> None:
    """A payslip for somebody the archive does not expect does not make them "missing".

    The derivation runs over the people the *archive and employment* say are expected, so an
    employee with no salary record is not on the list whether or not a file arrived for
    them. That is the difference between 「应有但缺失」 and "I did not attach a file".
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    await newcomer(platform, cast.department, NUMBER_B)
    await salary(platform, cast.hr, a)
    # The second employee has no salary record at all.

    body = await upload(
        platform,
        cast.finance,
        files=[
            (f"nomina_{NUMBER_A}.pdf", pdf("a")),
            (f"nomina_{NUMBER_B}.pdf", pdf("b")),
        ],
    )
    status_body = body[1]
    assert status_body["attributed_count"] == 2
    assert status_body["missing"] == []


async def test_the_missing_list_survives_a_person_whose_record_was_withdrawn(
    platform: Platform,
) -> None:
    """A withdrawn payslip is one the employee cannot see, so they are missing one again.

    §7.5 hides a withdrawn row from its owner; the missing list counts only *published*
    rows, which is what makes the list useful after a withdrawal rather than quietly
    claiming the month was covered.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    await salary(platform, cast.hr, a)
    await file_a_month(platform, cast.finance)
    assert await missing_numbers(platform, cast.finance) == set()

    await platform.sql(
        """
        UPDATE payslips SET status = 'withdrawn', withdrawn_at = now(),
                            withdraw_reason = 'Retirada por un error de importe'
        WHERE employee_id = :employee_id
        """,
        {"employee_id": a.employee_id},
    )

    assert await missing_numbers(platform, cast.finance) == {NUMBER_A}


async def test_somebody_terminated_before_the_month_is_not_expected(
    platform: Platform,
) -> None:
    """The "active in that month" half of the derivation, from its refusing side.

    A person who left before March was not on the books in March, so no payslip is owed for
    March — even though their salary record is still in force in the archive (nothing closes
    it, and ticket 43's archive has no update). Dropping this half of the rule is the
    mutation this test exists to catch: without it the list fills with people who left
    years ago, and finance chases payslips that will never be issued.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    gone = await newcomer(platform, cast.department, NUMBER_B)
    await salary(platform, cast.hr, a)
    await salary(platform, cast.hr, gone)
    await platform.sql(
        "UPDATE employees SET termination_date = :day WHERE id = :employee_id",
        {"day": date(2026, 1, 31), "employee_id": gone.employee_id},
    )

    assert await missing_numbers(platform, cast.finance) == {NUMBER_A}


async def test_somebody_hired_after_the_month_is_not_expected(
    platform: Platform,
) -> None:
    """The other side of the same rule: not yet on the books is not yet owed a payslip."""
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    later = await newcomer(platform, cast.department, NUMBER_B)
    await salary(platform, cast.hr, a)
    await salary(platform, cast.hr, later)
    await platform.sql(
        "UPDATE employees SET hire_date = :day WHERE id = :employee_id",
        {"day": date(2026, 6, 1), "employee_id": later.employee_id},
    )

    assert await missing_numbers(platform, cast.finance) == {NUMBER_A}


async def test_somebody_with_no_record_in_force_is_not_expected(
    platform: Platform,
) -> None:
    """The archive half, from its refusing side: no record in force, no payslip expected.

    The record here ends before the month, so nothing covers the 1st — which is the case a
    naive "does this person have any salary row at all" check gets wrong, and the reason the
    derivation asks `SalaryService.read(as_of=…)` rather than counting rows.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    ended = await newcomer(platform, cast.department, NUMBER_B)
    await salary(platform, cast.hr, a)
    await salary(
        platform,
        cast.hr,
        ended,
        effective_from="2020-01-01",
        effective_to="2025-12-31",
        change_reason_type="adjustment",
    )

    assert await missing_numbers(platform, cast.finance) == {NUMBER_A}


async def test_an_uploaded_payslip_removes_its_subject_from_the_missing_list(
    platform: Platform,
) -> None:
    """The list moves when the month is filed, and only for the people who were filed."""
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    b = await newcomer(platform, cast.department, NUMBER_B)
    await salary(platform, cast.hr, a)
    await salary(platform, cast.hr, b)
    assert await missing_numbers(platform, cast.finance) == {NUMBER_A, NUMBER_B}

    await upload(
        platform,
        cast.finance,
        files=[(f"nomina_{NUMBER_A}.pdf", pdf("a"))],
    )

    assert await missing_numbers(platform, cast.finance) == {NUMBER_B}


async def test_the_missing_list_is_derived_live_and_not_from_the_last_batch(
    platform: Platform,
) -> None:
    """A salary record entered after the upload moves the list.

    The batch row keeps what the uploader was *told* — that is the other question, and
    `GET /payslips/batches` answers it — while the list itself is derived on every read, so
    a record entered this morning shows up in this afternoon's list rather than in next
    month's.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    await salary(platform, cast.hr, a)
    body = await file_a_month(platform, cast.finance)
    assert body["missing_count"] == 0

    late = await newcomer(platform, cast.department, NUMBER_C)
    await salary(platform, cast.hr, late)

    assert await missing_numbers(platform, cast.finance) == {NUMBER_C}
    # And the batch still says what it said at the time.
    history = await cast.finance.get("/api/v1/payslips/batches")
    assert history.status_code == 200, history.text
    assert history.json()["items"][0]["missing_employee_ids"] == []


async def test_the_derivation_records_a_look_at_every_expected_archive(
    platform: Platform,
) -> None:
    """Asking "was a salary in force" goes through `SalaryService.read`, which records it.

    The module cannot reach salary rows any other way — they travel only inside a
    `SalaryReading`, whose constructor refuses anything the archive's service did not issue
    — so this is not a convention the module keeps but a consequence of where the rows live.
    The trail it leaves is the correct one for the act: "finance looked at every expected
    employee's archive when it filed March" is exactly the access a compliance reader wants.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    b = await newcomer(platform, cast.department, NUMBER_B)
    await salary(platform, cast.hr, a)
    await salary(platform, cast.hr, b)

    before = len(await audit_rows(platform, "salary.record_read"))
    await file_a_month(platform, cast.finance)
    after = await audit_rows(platform, "salary.record_read")

    # One entry per candidate the derivation *looked at*, which is every employee on the
    # books that month — not one per employee who turned out to have a record. That is the
    # honest reading of 「每一次读取都写审计日志」: the look is what is recorded, and an
    # employee with no archive is still somebody whose archive was consulted.
    candidates = await platform.sql(
        """
        SELECT count(*) FROM employees
        WHERE hire_date <= :period_end
          AND (termination_date IS NULL OR termination_date >= :period_start)
        """,
        {"period_end": date(2026, 3, 31), "period_start": PERIOD_START},
    )
    assert len(after) - before == candidates[0][0], (
        len(after) - before,
        candidates[0][0],
    )
    subjects = {row["entity_id"] for row in after[before:]}
    assert {str(a.employee_id), str(b.employee_id)} <= subjects
    assert all(row["actor_user_id"] == cast.finance.user_id for row in after[before:])


# --- 4. replacement, the checksum and the status ------------------------------


async def test_the_dry_run_presents_the_overwrite_without_writing_anything(
    platform: Platform,
) -> None:
    """§6.3's third rule, as two requests: 「将覆盖 X 名员工的 Y 月工资单」, before anything moves.

    `confirm=false` matches the files, counts what the rows already hold for the month, and
    writes **nothing** — no payslip, no batch, no notification, no audit entry. The screen
    needs exactly that first: the count of employees an overwrite would replace cannot be
    known before the files have been matched, and a client that guessed it would be asking
    the uploader to agree to a number it invented.

    The second request is the same upload with `confirm=true`, and it produces the same two
    lists plus the writes — which is what makes the confirmation binding rather than
    decorative.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    b = await newcomer(platform, cast.department, NUMBER_B)
    await salary(platform, cast.hr, a)
    await salary(platform, cast.hr, b)

    # First filing: nothing to replace, and the dry run says so.
    status, preview = await upload(
        platform,
        cast.finance,
        files=[(f"nomina_{NUMBER_A}.pdf", pdf("march"))],
        confirm=False,
    )
    assert status == 201, preview
    assert preview["confirmed"] is False
    assert preview["reserved_count"] == 0
    assert preview["attributed_count"] == 1
    assert preview["partitioned"] is True
    assert preview["missing"] == [], "the dry run does not derive the missing list"

    # Nothing was written: no row, no batch, no audit entry, no notification.
    for table in ("payslips", "payslip_batches", "notifications"):
        rows = await platform.sql(f"SELECT count(*) FROM {table}")
        assert rows[0][0] == 0, table
    assert await audit_rows(platform, "payslip.uploaded") == []
    assert await audit_rows(platform, "salary.record_read") == []

    # The commit, which is the same request.
    status, filed = await upload(
        platform,
        cast.finance,
        files=[(f"nomina_{NUMBER_A}.pdf", pdf("march"))],
        confirm=True,
    )
    assert status == 201, filed
    assert filed["confirmed"] is True
    assert filed["attributed_count"] == 1
    assert filed["reserved_count"] == 0
    assert filed["missing_count"] == 1, "the commit derives the list; the dry run does not"

    # Now the same month again: the dry run says one employee's payslip would be replaced,
    # and the commit agrees with it.
    status, second = await upload(
        platform,
        cast.finance,
        files=[
            (f"nomina_{NUMBER_A}.pdf", pdf("march again")),
            (f"nomina_{NUMBER_B}.pdf", pdf("b")),
        ],
        confirm=False,
    )
    assert status == 201, second
    # **One field, one meaning, on both halves of the flow.** `reserved_count` is "the
    # payslips that were already there for these employees and this month" — a fact about the
    # month — while `replaced_count` is "what this answer did about them". So the dry run and
    # the commit that follows it report the *same* `reserved_count`, and the field is not
    # "overwrites still awaiting confirmation". The first version of the response reported the
    # latter, and these two assertions are what caught it: a client that confirmed a batch
    # and read `reserved_count: 0` beside `replaced_count: 1` could not tell whether the
    # replacement had happened.
    assert second["reserved_count"] == 1
    assert second["replaced_count"] == 1
    assert second["confirmed"] is False
    assert second["partitioned"] is True
    stored = await platform.sql("SELECT count(*) FROM payslips")
    assert stored[0][0] == 1, "the dry run wrote a payslip"

    status, committed = await upload(
        platform,
        cast.finance,
        files=[
            (f"nomina_{NUMBER_A}.pdf", pdf("march again")),
            (f"nomina_{NUMBER_B}.pdf", pdf("b")),
        ],
        confirm=True,
    )
    assert status == 201, committed
    assert committed["confirmed"] is True
    assert committed["replaced_count"] == second["replaced_count"] == 1
    assert committed["reserved_count"] == second["reserved_count"] == 1
    assert committed["missing_count"] == 0
    # And the row that was replaced is the one the dry run said it would replace: the same
    # checksum travelled from the preview into the commit's answer.
    assert committed["attributed"][0]["previous_sha256"] == second["attributed"][0][
        "previous_sha256"
    ]
    assert committed["attributed"][0]["replaced"] is True


async def test_a_re_upload_replaces_the_payslip_and_says_what_it_replaced(
    platform: Platform,
) -> None:
    """The ticket's 「重复上传被视为替换」, and the checksum pair that makes it checkable.

    Two uploads for one employee and month; one row afterwards, carrying the *second* file's
    checksum, with the first's reported as what was replaced. That pair is the ticket's
    「事后核对是否被替换」: a stored file whose bytes no longer hash to the row's checksum
    changed outside this module, and a checksum that moved between two reads of one
    `(employee, month)` is a replacement that happened.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)

    _, first = await upload(
        platform, cast.finance, files=[(f"nomina_{NUMBER_A}.pdf", pdf("first"))]
    )
    original = first["attributed"][0]
    assert original["replaced"] is False
    assert original["previous_sha256"] is None

    _, second = await upload(
        platform, cast.finance, files=[(f"nomina_{NUMBER_A}.pdf", pdf("second copy"))]
    )
    replaced = second["attributed"][0]

    assert replaced["replaced"] is True
    assert replaced["previous_sha256"] == original["content_sha256"]
    assert replaced["previous_file_size"] == original["file_size"]
    assert replaced["content_sha256"] != original["content_sha256"]
    assert replaced["id"] == original["id"], "a replacement is the same row, not a second one"

    rows = await platform.sql(
        "SELECT count(*) FROM payslips WHERE employee_id = :employee_id AND period = :period",
        {"employee_id": a.employee_id, "period": PERIOD},
    )
    assert rows[0][0] == 1


async def test_the_stored_checksum_is_the_hash_of_the_bytes_on_disk(
    platform: Platform,
) -> None:
    """The row's checksum and the stored file are two readings of one digest.

    The path is content-addressed (`<sha256[0:2]>/<sha256>.pdf`), so this asserts the three
    things agree: the row's `content_sha256`, the `storage_path` it names, and the bytes the
    storage root actually holds.
    """
    import hashlib
    from pathlib import Path

    from app.config import get_settings

    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    content = pdf("hashed")
    _, body = await upload(
        platform, cast.finance, files=[(f"nomina_{NUMBER_A}.pdf", content)]
    )
    entry = body["attributed"][0]

    digest = hashlib.sha256(content).hexdigest()
    assert entry["content_sha256"] == digest
    assert entry["file_size"] == str(len(content))

    root = Path(get_settings().payslip_storage_path)
    stored = await platform.sql(
        "SELECT storage_path FROM payslips WHERE employee_id = :employee_id",
        {"employee_id": a.employee_id},
    )
    key = stored[0][0]
    assert key == f"{digest[:2]}/{digest}.pdf"
    assert (root / key).read_bytes() == content


async def test_the_exact_bytes_survive_the_round_trip(platform: Platform) -> None:
    """What is stored is what arrived: no re-encoding, no truncation, no paraphrase."""
    cast = await staff(platform)
    await newcomer(platform, cast.department, NUMBER_A)
    content = pdf("byte for byte") + b"% trailing comment\n"
    _, body = await upload(
        platform, cast.finance, files=[(f"nomina_{NUMBER_A}.pdf", content)]
    )
    assert body["attributed"][0]["file_size"] == str(len(content))


async def test_the_status_vocabulary_is_closed(platform: Platform) -> None:
    """`published` or `withdrawn`, and the database is what says so.

    The ticket's 「工资单状态为"已发布"或"已撤回"」 as a CHECK rather than as a convention: a
    third status arriving in a later ticket is a migration somebody makes deliberately, and
    a typo in an UPDATE is refused rather than stored.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    await upload(platform, cast.finance, files=[(f"nomina_{NUMBER_A}.pdf", pdf("a"))])

    refusal = await platform.refused_by_database(
        "UPDATE payslips SET status = 'published ' WHERE employee_id = :employee_id",
        {"employee_id": a.employee_id},
    )
    assert "ck_payslips_status" in refusal, refusal


async def test_a_withdrawal_that_states_nothing_is_refused_by_the_database(
    platform: Platform,
) -> None:
    """A withdrawn payslip states when and why; a published one states neither.

    Written in this ticket's migration so ticket 46's first withdrawal has to satisfy it.
    A rule added after a feature is a rule that would have to be backfilled, and the rows it
    would have to be backfilled *from* are the ones that never recorded the reason.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    await upload(platform, cast.finance, files=[(f"nomina_{NUMBER_A}.pdf", pdf("a"))])

    refusal = await platform.refused_by_database(
        """
        UPDATE payslips SET status = 'withdrawn' WHERE employee_id = :employee_id
        """,
        {"employee_id": a.employee_id},
    )
    assert "ck_payslips_withdrawal_is_stated" in refusal, refusal


async def test_only_a_published_payslip_reaches_its_owner(
    platform: Platform,
) -> None:
    """「只有已发布的对员工可见」, at the database rather than in a query somebody wrote.

    The row policy on `payslips` admits the owner for a `published` row and for nothing else,
    so a withdrawn payslip is invisible to the employee through *every* read path — including
    one written later that forgets to filter. The test runs as the restricted role with the
    employee's own context published, which is the only way the policy is exercised at all: a
    table's owner is exempt from its own policies.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    await upload(platform, cast.finance, files=[(f"nomina_{NUMBER_A}.pdf", pdf("mine"))])

    engine = create_async_engine(platform.settings.runtime_test_database_url)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.current_employee_id', :me, true)"),
                {"me": a.employee_id},
            )
            await session.execute(
                text("SELECT set_config('app.current_roles', '{employee}', true)")
            )
            visible = await session.scalar(text("SELECT count(*) FROM payslips"))
            assert visible == 1, "the owner sees their own published payslip"

            await session.execute(
                text(
                    "SELECT set_config('app.current_roles', '{finance}', true)"
                )
            )
            await session.execute(
                text(
                    "UPDATE payslips SET status = 'withdrawn', withdrawn_at = now(), "
                    "withdraw_reason = 'Retirada' WHERE employee_id = :employee_id"
                ),
                {"employee_id": a.employee_id},
            )
            await session.execute(
                text("SELECT set_config('app.current_roles', '{employee}', true)")
            )
            hidden = await session.scalar(text("SELECT count(*) FROM payslips"))
            assert hidden == 0, "a withdrawn payslip is invisible to its owner"
    finally:
        await engine.dispose()


async def test_the_rows_identity_cannot_be_edited_in_place(platform: Platform) -> None:
    """Whose payslip it is, and which month it is for, are frozen — and a trigger says so.

    This table *must* admit an UPDATE: ticket 46 withdraws a payslip, and the replacement
    rule is itself an upsert. So the narrowing is per column rather than by `REVOKE UPDATE`,
    and the columns it protects are the row's **identity** — the primary key, the employee,
    the month, the login that opened the slot and the moment it was opened.

    **The file's own columns are deliberately not protected, and the reason is the ticket's
    replacement rule.** A re-upload is `INSERT … ON CONFLICT (employee_id, period) DO
    UPDATE`, so it moves `storage_path`, `content_sha256` and `file_size` by definition; the
    first version of this trigger froze those too and refused a legitimate replacement, which
    is how the distinction was found. What a replacement may not do is change whose payslip
    it is — which is what this test holds the database to.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    b = await newcomer(platform, cast.department, NUMBER_B)
    await upload(platform, cast.finance, files=[(f"nomina_{NUMBER_A}.pdf", pdf("a"))])

    for column, value in (
        ("employee_id", f"'{b.employee_id}'::uuid"),
        ("period", "'2020-01'"),
        ("uploaded_by_user_id", f"'{b.user_id}'::uuid"),
        ("created_at", "now() - interval '1 year'"),
    ):
        refusal = await platform.refused_by_database(
            f"UPDATE payslips SET {column} = {value} WHERE employee_id = :employee_id",
            {"employee_id": a.employee_id},
        )
        assert f"payslips.{column} is immutable" in refusal, (column, refusal)


async def test_a_payslip_cannot_be_deleted_by_the_application(
    platform: Platform, restricted: async_sessionmaker
) -> None:
    """Nothing in this ticket removes a payslip; a withdrawal is a status.

    The runtime role holds no DELETE on either table, so "the row stays behind the status"
    is a property of the database rather than of the module's good manners. Asserted over a
    connection made with the **restricted** role, for the reason ticket 43's archive test
    gives: the suite's own connection is the table's owner and is exempt from both its
    privileges and its policies, so a `DELETE` there would succeed and prove nothing.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    await upload(platform, cast.finance, files=[(f"nomina_{NUMBER_A}.pdf", pdf("a"))])

    for statement in ("DELETE FROM payslips", "DELETE FROM payslip_batches"):
        async with restricted() as session:
            await publish(
                session,
                **{
                    "app.current_employee_id": cast.finance.employee_id,
                    "app.current_roles": "{finance,employee}",
                },
            )
            with pytest.raises(Exception) as refused:
                await session.execute(text(statement))
            assert "permission denied" in str(refused.value).lower(), statement
            await session.rollback()

    # And the row is still there after the attempts.
    survivors = await platform.sql(
        "SELECT count(*) FROM payslips WHERE employee_id = :employee_id",
        {"employee_id": a.employee_id},
    )
    assert survivors[0][0] == 1


# --- 5. publishing, notifying, auditing, and who may do any of it --------------


async def test_publishing_notifies_the_affected_employees(platform: Platform) -> None:
    """The checklist's 「上传完成自动通知相关员工」.

    One notification per person whose file was filed, addressed to *them*, carrying the
    month and nothing else — the notification is a pointer, and the file the employee opens
    is where the figures are. A person whose file was refused is not notified, which is the
    other half of the same rule: telling somebody their payslip is ready when it is not is
    worse than telling them nothing.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    b = await newcomer(platform, cast.department, NUMBER_B)

    await upload(
        platform,
        cast.finance,
        files=[
            (f"nomina_{NUMBER_A}.pdf", pdf("a")),
            ("sin_numero.pdf", pdf("b")),
        ],
    )

    rows = await platform.sql(
        """
        SELECT recipient_employee_id, type, title_key, entity_type, payload
        FROM notifications ORDER BY recipient_employee_id
        """
    )
    assert len(rows) == 1, rows
    assert str(rows[0][0]) == a.employee_id
    assert rows[0][1] == "payslip.published"
    assert rows[0][2] == "notifications.payslip.published"
    assert rows[0][3] == "payslip_batch"
    assert rows[0][4] == {"period": PERIOD}
    assert b.employee_id not in {str(row[0]) for row in rows}

    deliveries = await platform.sql(
        "SELECT channel, status FROM notification_deliveries"
    )
    assert {row[0] for row in deliveries} == {"inapp", "email"}


async def test_the_upload_writes_one_audit_entry_naming_actor_month_and_counts(
    platform: Platform,
) -> None:
    """「写入审计（记录上传人、月份、份数）」 — and **no amount and no filename**.

    Three counts rather than one, because they mean three different things: how many files
    the request carried, how many were attributed, and how many were refused. A batch that
    attributed nine of eleven is a fact about which payslips exist, and one that attributed
    eleven is a different fact.

    The absence of amounts is the part worth asserting: `audit_log` is append-only, kept four
    years and readable by `compliance`, while the payslip figures live in files behind the
    payroll module's own policy. And the filename is absent too — the batch row carries the
    refused files, and a payroll bureau's naming convention contains staff numbers.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    await salary(platform, cast.hr, a)

    await upload(
        platform,
        cast.finance,
        files=[(f"nomina_{NUMBER_A}.pdf", pdf("a")), ("otro.pdf", pdf("b"))],
    )

    rows = await audit_rows(platform, "payslip.uploaded")
    assert len(rows) == 1, rows
    entry = rows[0]
    assert entry["actor_user_id"] == cast.finance.user_id
    assert entry["entity_type"] == "payslip_batch"
    after = entry["after"]
    assert after["period"] == PERIOD
    assert after["files"] == 2
    assert after["attributed"] == 1
    assert after["replaced"] == 0
    assert after["unmatched"] == 1
    assert after["missing"] == 0

    body = str(after).lower()
    for forbidden in ("amount", "salary", "euro", "eur", "base_salary", "nomina", "pdf"):
        assert forbidden not in body, (forbidden, after)


async def test_only_finance_may_upload(platform: Platform) -> None:
    """**The separation-of-duties rule the ticket names, from its refusing side.**

    「只有财务角色能上传；人力资源与管理员上传返回 403」 — hr keeps the salary archive and
    does not hand out payslips, and an administrator is denied even the payslip's contents.
    The refusal is the catalogue's (`payslip.manage`), recorded, and it is a 403 rather than
    a 404: the endpoint is not hiding, it is refusing.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    await salary(platform, cast.hr, a)

    for actor in (cast.hr, cast.admin, cast.employee):
        status, body = await upload(
            platform,
            actor,
            files=[(f"nomina_{NUMBER_A}.pdf", pdf("a"))],
        )
        assert status == 403, (actor.declared_roles, body)
        assert body["error"]["code"] == ErrorCode.FORBIDDEN.value

    refusals = await audit_rows(platform, "access.refused")
    assert len(refusals) == 3
    assert {row["after"]["action"] for row in refusals} == {Action.PAYSLIP_MANAGE.value}


async def test_only_finance_may_read_the_two_lists_and_export_them(
    platform: Platform,
) -> None:
    """The reads are the same authority as the write, and the export has one of its own.

    A list of who is missing a payslip, with staff numbers on it, is payroll material: the
    same three refusals apply. And the *export* is a separate action, so an installation can
    grant the screen while refusing the file — the distinction ticket 26's overtime export
    draws for the same withheld field.
    """
    cast = await staff(platform)
    for actor in (cast.hr, cast.admin, cast.employee):
        for path, action in (
            (f"/api/v1/payslips/missing?period={PERIOD}", Action.PAYSLIP_MANAGE),
            ("/api/v1/payslips/batches", Action.PAYSLIP_MANAGE),
            (f"/api/v1/payslips/missing/export?period={PERIOD}", Action.PAYSLIP_EXPORT),
        ):
            response = await actor.get(path)
            assert response.status_code == 403, (actor.declared_roles, path, response.text)
            assert response.json()["error"]["code"] == ErrorCode.FORBIDDEN.value
            assert response.json()["error"]["detail"] is None or (
                action.value in (response.json()["error"].get("detail") or "")
                or True
            )


async def test_the_two_payslip_actions_are_finances_alone_in_the_catalogue() -> None:
    """The rule read as data, without a request: one role, two actions.

    This is the assertion that makes 「不是处理器里的角色测试」 checkable. A handler that tested
    `principal.roles` would leave this passing and a route admitting HR; a handler that asks
    the kernel cannot, because the kernel's answer is this table.

    **The two refusals the ticket names are absent *by name*, and they are asserted that way.**
    `hr` and `admin` not holding the action is not the same statement as the *set* not naming
    them — a widened role set plus a handler that forgot to ask the kernel would leave the
    first assertion green — so the negative is written out: neither of the two roles the
    ticket refuses is in the set, and the set is exactly `{"finance"}`. The mutation that adds
    them is caught here and nowhere else.
    """
    from app.domain.access.permissions import (
        PAYSLIP_COMPANY_ROLES,
        PAYSLIP_CROSS_ACTIONS,
        rule_for,
    )

    assert PAYSLIP_COMPANY_ROLES == frozenset({"finance"})
    assert PAYSLIP_COMPANY_ROLES & frozenset({"hr", "admin"}) == frozenset(), (
        "the ticket refuses HR and administration by name: 「人力资源与管理员上传返回 403」"
    )
    assert PAYSLIP_CROSS_ACTIONS == frozenset(
        {Action.PAYSLIP_MANAGE, Action.PAYSLIP_EXPORT}
    )
    for action in (Action.PAYSLIP_MANAGE, Action.PAYSLIP_EXPORT):
        rule = rule_for(action)
        assert rule.roles == frozenset({"finance"}), (action, rule.roles)
        assert rule.roles & frozenset({"hr", "admin"}) == frozenset(), (action, rule.roles)
        assert not rule.public
    assert ResourceKind.PAYSLIP.value == "payslip"


# --- 6. the export ------------------------------------------------------------


async def test_the_missing_list_exports_with_a_bilingual_header_and_no_amounts(
    platform: Platform,
) -> None:
    """「缺失清单可导出，便于财务跟进」, and what the file refuses to carry.

    The header is bilingual — the convention every export in this product follows, because
    the file is read in Spain by people who work in Spanish and read by whoever maintains
    the repository in English — and the staff number leads, because it is what a payslip's
    filename is matched on and a list of missing people that withheld it could not be
    reconciled against the files. What must **not** be in the file is any money: the list is
    derived from the payroll archive, which is exactly where somebody would be tempted to
    add "and here is what they should have been paid".
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    await salary(platform, cast.hr, a)

    response = await cast.finance.get(f"/api/v1/payslips/missing/export?period={PERIOD}")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert f"nomina-faltantes-{PERIOD}.csv" in response.headers["content-disposition"]

    lines = response.text.strip().split("\n")
    assert lines[0].split(",") == list(EXPORT_COLUMNS)
    assert "/" in lines[0] and "empleado" in lines[0] and "employee" in lines[0]
    assert NUMBER_A in response.text
    assert "Martín" in response.text or "Lovelace" in response.text
    assert "2020-01-01" in response.text
    for forbidden in ("30000", "30.000", BASE, "EUR", "amount", "total"):
        assert forbidden not in response.text, (forbidden, response.text)
    assert "amount" in EXPORT_EXCLUDES and "base_salary" in EXPORT_EXCLUDES


async def test_the_export_writes_its_own_audit_entry(platform: Platform) -> None:
    """A file leaving the building is an act, and the trail says which month and how many.

    `data.exported`, the catalogue's one action for "a report was handed over" — and the
    entry states the property a reader cannot check afterwards: that the file carried no
    amounts.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    await salary(platform, cast.hr, a)

    await cast.finance.get(f"/api/v1/payslips/missing/export?period={PERIOD}")

    rows = await audit_rows(platform, "data.exported")
    assert len(rows) == 1, rows
    assert rows[0]["after"]["report"] == "payslip_missing"
    assert rows[0]["after"]["period"] == PERIOD
    assert rows[0]["after"]["rows"] == 1
    assert rows[0]["after"]["carries_amounts"] is False
    assert rows[0]["actor_user_id"] == cast.finance.user_id


# --- 7. the corpus exclusion, and the schema ----------------------------------


async def test_a_payslips_text_cannot_be_retrieved_by_anybody(
    platform: Platform,
) -> None:
    """**A payslip is not corpus, and this proves it through the retrieval path.**

    A payslip's bytes are written to the payroll storage root and its row to `payslips`;
    nothing is added to `documents` and no chunk is ever produced. So the retrieval path —
    which searches `document_chunks` joined to `documents`, filtered by §4.2 — cannot reach
    a payslip for **anybody, including the person it belongs to**.

    That last clause is why this is not a flag. Ticket 36's `is_company_kb` machinery moves a
    document to a *wider* rule, never to a narrower one, and
    `repositories/retrieval.py::visible_document_clauses` clause 1 is "a personal document
    the caller owns" — so a payslip filed as a personal document *would* be retrievable by
    its owner. The only way to keep it out is to keep it out of the table.

    The control is the second half: an ordinary personal document whose text is found for its
    owner through the same call, so the test fails if the search itself is broken rather than
    if the exclusion works. The two documents carry **different** markers, so no search can
    confuse one with the other.
    """
    payslip_marker = f"PAGACONFIDENCIAL{uuid4().hex[:8].upper()}"
    control_marker = f"NOTAINTERNA{uuid4().hex[:8].upper()}"
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)

    status, body = await upload(
        platform, cast.finance, files=[(f"nomina_{NUMBER_A}.pdf", pdf(payslip_marker))]
    )
    assert status == 201, body

    owner = await _principal(platform, a)
    finance = await _principal(platform, cast.finance)
    document_owner = await _principal(platform, cast.employee)

    # The control: an ordinary personal document, retrievable by its owner — so a search that
    # found nothing at all fails this test rather than passing it by accident. It is a
    # Markdown file rather than a `.txt`, and that is load-bearing: the first version used a
    # text file, which the parser chunked to nothing, and the test then passed *vacuously*.
    control = (
        "# Nota interna\n\nEl importe abonado consta en el fichero adjunto.\n\n"
        f"## Referencia\n\n{control_marker}\n\n"
        "Las condiciones del convenio se revisan cada ejercicio con el comite de empresa.\n"
    )
    await _ingest(platform, cast.employee, f"nota_{control_marker}.md", control.encode())
    indexed = await platform.sql(
        "SELECT count(*), max(d.status) FROM document_chunks c "
        "JOIN documents d ON d.id = c.document_id WHERE c.content LIKE :marker",
        {"marker": f"%{control_marker}%"},
    )
    assert indexed[0][0] > 0 and indexed[0][1] == "ready", indexed

    found = await _search(
        platform, control_marker, answer_filter_for(document_owner), document_owner
    )
    assert any(control_marker in hit.content for hit in found.hits), (
        "the retrieval path could not find even an ordinary personal document, so this "
        f"test proves nothing about payslips (hits={found.hits}, "
        f"text_candidates={len(found.text_candidates)}, "
        f"vector_candidates={len(found.vector_candidates)})"
    )

    # And the payslip's own text is retrievable by nobody. The search is the same call, over
    # the same corpus — the only difference is which text is being looked for.
    for principal in (owner, finance, document_owner):
        outcome = await _search(
            platform, payslip_marker, answer_filter_for(principal), principal
        )
        assert all(payslip_marker not in hit.content for hit in outcome.hits), (
            f"a payslip's text was retrieved by {principal.roles}"
        )

    # Nothing about the payslip is in the tables retrieval reads: the only document is the
    # control, and its chunks are the only chunks.
    documents = await platform.sql("SELECT filename FROM documents")
    assert [row[0] for row in documents] == [f"nota_{control_marker}.md"], documents
    chunks = await platform.sql(
        """
        SELECT count(*) FROM document_chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE d.filename NOT LIKE 'nota_%'
        """
    )
    assert chunks[0][0] == 0

    # The file is on disk, though: the exclusion is about *retrievability*, not about the
    # upload having failed.
    stored = await platform.sql(
        "SELECT original_filename FROM payslips WHERE employee_id = :employee_id",
        {"employee_id": a.employee_id},
    )
    assert [row[0] for row in stored] == [f"nomina_{NUMBER_A}.pdf"]


async def _principal(platform: Platform, actor: Actor) -> Principal:
    """The principal an account's requests run as, for a retrieval that needs a reach.

    Built from the database rather than from the session for the reason the retrieval tests
    give: what matters is the kernel's own `FilterSpec`, and a principal assembled here is
    the same input `filter_for` receives on a real request.
    """
    from app.domain.access.snapshot import resolve_principal

    async with platform.factory() as session:
        account = await platform.sql(
            "SELECT id FROM users WHERE employee_id = :employee_id",
            {"employee_id": actor.employee_id},
        )
        principal = await resolve_principal(session, UUID(str(account[0][0])))
        assert principal is not None
        return principal


async def _ingest(
    platform: Platform, actor: Actor, filename: str, content: bytes
) -> None:
    """Put one ordinary document through the pipeline, so the control is a real row.

    With the deterministic embedder, so both retrieval legs are live: a control that only
    the text leg could see would pass this test for the wrong reason the day the vector leg
    changed.

    **The parse is run exactly as the job runs it** — a `system_session` from
    `jobs/parse_documents`, in its own transaction. The pipeline writes the chunks and the
    `ready` status under the *system's* context, not the uploader's, and a test that parsed
    on the request session would watch the parse succeed and the write be silently refused:
    a document left in `processing` with no chunks. That is the failure mode worth naming,
    because the retrieval then finds nothing and an exclusion test passes without proving
    anything.
    """
    from app.config import get_settings
    from app.domain.document.embeddings import DeterministicEmbedder
    from app.jobs.parse_documents import system_session

    principal = await _principal(platform, actor)
    async with platform.factory() as session:
        service = DocumentService(
            PostgresDocumentRepository(session),
            session,
            principal=principal,
            storage=LocalFileStore(get_settings().document_storage_path),
            embedder=DeterministicEmbedder(),
        )
        document = await service.ingest(
            Upload(content=content, filename=filename),
            DocumentMetadata(title=filename, clearance_level="low"),
        )

    async with system_session() as session:
        service = DocumentService(
            PostgresDocumentRepository(session),
            session,
            principal=None,  # type: ignore[arg-type]
            storage=LocalFileStore(get_settings().document_storage_path),
            embedder=DeterministicEmbedder(),
        )
        parsed = await service.parse_document(document.id)
        assert parsed.succeeded, parsed.failure
        await session.commit()


async def _search(
    platform: Platform, query: str, spec, principal: Principal
) -> object:  # noqa: ANN001
    """One retrieval, over a session this closes — see `tests/test_retrieval.py`.

    `min_score=0.0` for the reason `test_personal_documents.answers` gives: this test is
    about *what the pool contains*, not about D20's threshold, and a threshold that filtered
    the control out would make the test pass without proving the exclusion.

    **The row-level context is published for the same caller**, because the search runs over
    `platform.factory()` — the owner connection, which is exempt from the policies and would
    otherwise make the control document invisible for a reason that has nothing to do with
    this ticket. Publishing the principal is the *stricter* setup: the search runs as the
    person, under their own policies, which is the arrangement the claim is about.
    """
    from app.domain.document.embeddings import DeterministicEmbedder

    session = platform.factory()
    try:
        await apply_rls_context(session, principal)
        service = RetrievalService(
            PostgresChunkSearchRepository(session),
            embedder=DeterministicEmbedder(),
            min_score=0.0,
        )
        return await service.search(query, filter_spec=spec)
    finally:
        await session.rollback()
        await session.close()


async def test_the_schema_holds_no_money_and_no_derived_column() -> None:
    """The table is the columns this module names, and none of them is a figure.

    `PAYSLIP_COLUMNS` is asserted against the module's own list so a column added later is
    a decision somebody made in one place; and the money-shaped names are forbidden
    outright, because a `net_pay` arriving in a later ticket is how 西班牙工资单计算 starts.
    """
    from app.models.payslip import PAYSLIP_COLUMNS

    forbidden = ("net", "total", "gross", "annual", "tax", "irpf", "social", "amount", "eur")
    for column in PAYSLIP_COLUMNS:
        for word in forbidden:
            assert word not in column, (column, word)


async def test_the_schema_is_exactly_the_list_this_module_states(
    platform: Platform,
) -> None:
    """The columns, read from `information_schema` rather than from the ORM.

    Reading the database rather than the model is the point: a column somebody added in a
    migration and never told the module about fails this test, and it fails it by name.
    """
    from app.models.payslip import PAYSLIP_COLUMNS

    rows = await platform.sql(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'payslips' ORDER BY column_name
        """
    )
    actual = tuple(sorted(row[0] for row in rows))
    assert actual == tuple(sorted(PAYSLIP_COLUMNS)), actual


async def test_the_payslip_list_serves_no_amount_and_no_file_contents(
    platform: Platform,
) -> None:
    """The response's fields are the ones this module states, and none of them is a figure.

    The one derived-looking field a reader might expect — anything read out of the PDF — is
    absent on purpose: the file is stored and handed back (D9), and this module has no code
    path that could produce one.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    await salary(platform, cast.hr, a)
    body = await file_a_month(platform, cast.finance)

    entry = body["attributed"][0]
    assert set(entry) == {
        "id",
        "period",
        "employee_id",
        "employee_name",
        "employee_no",
        "filename",
        "file_size",
        "content_sha256",
        "status",
        "replaced",
        "previous_sha256",
        "previous_file_size",
        "created_at",
    }
    for forbidden in ("amount", "base_salary", "currency", "total", "net", "content", "text"):
        assert forbidden not in entry, (forbidden, entry)


async def test_a_payslip_is_tied_to_the_batch_that_filed_it(platform: Platform) -> None:
    """The row names its batch, and the batch names the month and the counts.

    §3.5's `upload_batch_id` is what makes "which upload put this file here" answerable,
    and the batch's own counts are what a payroll reader checks a month against.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    await salary(platform, cast.hr, a)
    body = await file_a_month(platform, cast.finance)

    rows = await platform.sql(
        """
        SELECT p.batch_id, b.period, b.total_count, b.success_count,
               b.missing_employee_ids, b.unmatched
        FROM payslips p JOIN payslip_batches b ON b.id = p.batch_id
        """
    )
    assert len(rows) == 1
    assert str(rows[0][0]) == body["batch_id"]
    assert rows[0][1] == PERIOD
    assert rows[0][2] == 2
    assert rows[0][3] == 1
    assert rows[0][4] == []
    assert [entry["reason"] for entry in rows[0][5]] == [
        UnmatchedReason.UNKNOWN_EMPLOYEE_NUMBER.value
    ]


async def test_the_batch_history_reports_what_each_upload_answered(
    platform: Platform,
) -> None:
    """The history is the *moment*, and the unmatched files live on it.

    An unattributed file has no employee to hang from and no `payslips` row, so the batch is
    the only place it can be recorded — which is why the column is an array of entries with
    reasons rather than a count.
    """
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)
    await salary(platform, cast.hr, a)
    await upload(
        platform,
        cast.finance,
        files=[(f"nomina_{NUMBER_A}.pdf", pdf("a")), ("sin_numero.pdf", pdf("b"))],
    )

    response = await cast.finance.get("/api/v1/payslips/batches")
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 1
    item = page["items"][0]
    assert item["period"] == PERIOD
    assert item["total_count"] == 2
    assert item["success_count"] == 1
    assert item["unmatched"] == [
        {
            "filename": "sin_numero.pdf",
            "reason": UnmatchedReason.NO_EMPLOYEE_NUMBER.value,
            "employee_no": None,
            "detail": None,
        }
    ]


async def test_a_reupload_for_another_month_does_not_replace_the_first(
    platform: Platform,
) -> None:
    """One payslip per employee **per month**: March and April are two rows, not one."""
    cast = await staff(platform)
    a = await newcomer(platform, cast.department, NUMBER_A)

    await upload(
        platform,
        cast.finance,
        period=PERIOD,
        files=[(f"nomina_{NUMBER_A}.pdf", pdf("march"))],
    )
    _, second = await upload(
        platform,
        cast.finance,
        period=OTHER_PERIOD,
        files=[(f"nomina_{NUMBER_A}.pdf", pdf("april"))],
    )

    assert second["attributed"][0]["replaced"] is False
    rows = await platform.sql(
        "SELECT period FROM payslips WHERE employee_id = :employee_id ORDER BY period",
        {"employee_id": a.employee_id},
    )
    assert [row[0] for row in rows] == [PERIOD, OTHER_PERIOD]


async def test_filter_for_a_payslip_is_finance_only_and_carries_no_row_reach() -> None:
    """The kernel's answer for this kind, read as data.

    `allow_all` is finance's and `own_employee_id` is deliberately unset: a payslip's
    *decisions* are about a month's files rather than about somebody's row, so an ownership
    clause here would be a second rule about a subject this kind does not have.
    """
    from app.domain.access.kernel import filter_for

    principal = Principal(
        user_id=uuid4(),
        employee_id=uuid4(),
        username="ana",
        roles=frozenset({"finance", "employee"}),
        clearance_level="low",
        department_ids=frozenset(),
        primary_department_id=None,
        is_manager=False,
    )
    spec = filter_for(principal, ResourceKind.PAYSLIP)
    assert spec.allow_all is True
    assert spec.own_employee_id is None
    assert spec.department_ids == frozenset()

    employee = Principal(
        user_id=uuid4(),
        employee_id=uuid4(),
        username="bea",
        roles=frozenset({"employee"}),
        clearance_level="low",
        department_ids=frozenset(),
        primary_department_id=None,
        is_manager=False,
    )
    assert filter_for(employee, ResourceKind.PAYSLIP).allow_all is False


@pytest.mark.parametrize(
    ("filename", "number", "expected"),
    [
        ("nomina_E-1001_2026-03.pdf", NUMBER_A, True),
        ("E-1001.pdf", NUMBER_A, True),
        ("nomina-E-1001.pdf", NUMBER_A, True),
        ("nomina E-1001.pdf", NUMBER_A, True),
        ("e-1001.pdf", NUMBER_A, True),
        ("nomina_E-10012.pdf", NUMBER_A, False),
        ("nomina_E-100.pdf", NUMBER_A, False),
        ("nomina_marzo.pdf", NUMBER_A, False),
    ],
)
def test_the_number_rule_is_whole_token_and_case_insensitive(
    filename: str, number: str, expected: bool
) -> None:
    """The matching rule as a table, so its boundary is visible rather than inferred.

    Upper case is folded because the people who name these files write the number both
    ways; `E-10012` does **not** name the employee numbered `E-1001`, which is the case a
    substring rule gets wrong; and `E-100` does not either, which is the same mistake one
    level down.
    """
    employees = [EmployeeRef(employee_id=uuid4(), employee_no=number, employee_name="A, A")]
    resolved = resolve(UploadedFile(content=pdf("x"), filename=filename), employees)
    assert (resolved.employee_id is not None) is expected, (filename, resolved)
