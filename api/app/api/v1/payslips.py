"""The payslip endpoints: file a month, read the two lists, export the shortfall.

Four routes, and three of them are the screen's:

* **`POST /payslips/batches` is the ticket's one request.** A month, and the files. Its
  answer is the *two lists together* — what was attributed and who is missing one —
  because §6.3's first rule is that both are presented at once, and a client that had to
  make a second call for the missing list would be able to show the half that looks like
  success while the half that matters was still in flight. The unmatched files travel in
  the same answer, with their reasons: `docs/architecture/frontend-design-system.md` §6.3
  is explicit that they are listed rather than dropped.
* **`GET /payslips/missing` is the same list without an upload**, so the screen can show
  who is short *before* finance attaches anything — which is how somebody notices a month
  was never filed at all.
* **`GET /payslips/missing/export` is the file** the checklist asks for.
* **`GET /payslips/batches` is the history**: which months were filed, by whom, and with
  what result. Finance re-runs a month as a matter of course, and "who uploaded March, and
  did it attribute everything" is a question the batch rows answer.

**The guard is the catalogue and not a role test.** Every route depends on
`payslip.manage` or `payslip.export` — `require(...)` against `ResourceKind.PAYSLIP` — so
「只有财务角色能上传」 is a line of `domain/access/permissions.py` rather than an `if` in a
handler, and `hr` or `admin` reaching any of these is a recorded refusal with the
catalogue's own 403. The module never inspects `principal.roles` itself.

**A payslip is never served as content here.** There is no route that returns a file's
bytes in this ticket — ticket 45 is the self-service download — and, more importantly,
there is nothing in this module that could put one in the corpus: the files are written to
the payroll storage root and the rows to `payslips`, which the retrieval path does not
read. `tests/test_payslips.py` proves that through the retrieval service rather than by
asserting an absence in this file.

**Multipart, and the selections are optional and positional.** A client that wants to
settle what a filename could not sends `employee_id` once per file, in the same order;
a client that does not sends none. The rule for a file that carries both and where they
disagree is `domain/payslip/matching.py`'s, and it is stated there rather than here: the
selection wins for a filename that names nobody, and a *disagreement* is reported rather
than resolved.
"""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import current_principal, db_session, require
from app.api.v1.schemas.base import StrictModel
from app.core.errors import AppError, ErrorCode
from app.domain.access import Action, Principal
from app.domain.access.kernel import ResourceKind
from app.domain.notification.service import NotificationService
from app.domain.payslip.models import (
    AttributedPayslip,
    BatchOutcome,
    BatchPage,
    BatchRecord,
    EmployeeRef,
    MissingEmployee,
    MissingList,
    UploadedFile,
    parse_period,
    period_bounds,
)
from app.domain.payslip.service import PayslipService
from app.repositories.payslip import PostgresPayslipRepository

router = APIRouter(prefix="/payslips", tags=["payslips"])

#: The two authorities, and both are the catalogue's. `payslip.manage` is filing a month —
#: and reading the lists that go with it — and `payslip.export` is handing the follow-up
#: file over. `ResourceKind.PAYSLIP` is the kind the kernel's own branch decides, which is
#: one role wide: a manager and a colleague share a department, and this rule does not
#: consult one.
file_payslips = require(Action.PAYSLIP_MANAGE, ResourceKind.PAYSLIP)
export_missing = require(Action.PAYSLIP_EXPORT, ResourceKind.PAYSLIP)

#: How many files one request may carry. The service refuses more, and stating it here as
#: well means the framework refuses a body nobody should have sent before it is buffered.
MAX_FILES_PER_REQUEST = 400


class EmployeeRead(BaseModel):
    """The employee a file was attributed to, and the number it was matched on.

    `employee_no` is nullable because it is a withheld field rather than a required one.
    It reaches this surface — and the export — because the surface is `finance`'s: the
    staff number is what a payslip's filename carries, so a list that withheld it could not
    be reconciled against the files at all.
    """

    employee_id: UUID
    employee_no: str | None = None
    employee_name: str
    department_name: str | None = None


class PayslipRead(BaseModel):
    """One filed payslip. **No amount, and never the file's contents.**

    `file_size` and `content_sha256` are the ticket's 「事后核对」 pair: the size as a
    string (JSON has one number type and it is a float, the reasoning ticket 43's amounts
    follow), and the digest so a replacement can be checked afterwards. `replaced` says
    whether this upload overwrote one, and `previous_sha256` is what the row held before —
    which is what makes the answer checkable rather than merely reassuring.
    """

    id: UUID
    period: str
    employee_id: UUID
    employee_name: str
    employee_no: str | None = None
    filename: str
    file_size: str
    content_sha256: str
    status: str
    replaced: bool
    previous_sha256: str | None = None
    previous_file_size: str | None = None
    created_at: str | None = None


class UnmatchedRead(BaseModel):
    """A file that was not attributed, and why. The half of the answer 不静默丢弃 is about.

    `reason` is a token from `UnmatchedReason` rather than a sentence: the screen renders it
    in the reader's language, and the token is what a test asserts on. `detail` carries the
    fact the token cannot — which number was not recognised, how big the file was — so the
    sentence a person reads can be specific without the vocabulary growing.
    """

    filename: str
    reason: str
    employee_no: str | None = None
    detail: str | None = None


class MissingRead(BaseModel):
    """Somebody who should have a payslip for this month and has none.

    The two dates are the *salary record's window* — why this person is expected — and they
    are dates, never an amount. What somebody earns is the archive's business; this list's
    job is to say who is short a payslip.
    """

    employee_id: UUID
    employee_no: str | None = None
    employee_name: str
    department_name: str | None = None
    salary_effective_from: date
    salary_effective_to: date | None = None


class BatchRead(BaseModel):
    """One upload's whole answer: **the two lists together**, plus the files that matched
    nobody.

    `total_count`, `attributed_count`, `unmatched_count` and `missing_count` are stated
    rather than left to the client's arithmetic, because the property the screen rests on —
    every file is in exactly one of the two lists — should be checkable from the response
    itself. `partitioned` is that check, served rather than inferred.
    """

    batch_id: UUID
    period: str
    total_count: int
    attributed_count: int
    replaced_count: int
    unmatched_count: int
    missing_count: int
    attributed: list[PayslipRead]
    unmatched: list[UnmatchedRead]
    missing: list[MissingRead]
    partitioned: bool
    created_at: str | None = None
    #: False when this is the **dry run** — the matching has been done, the two lists are
    #: complete, and nothing has been written. §6.3's third rule is that the overwrite is
    #: confirmed by naming how many employees and which month, and neither side knows that
    #: before the files have been matched, so the screen's first request is unanswered by
    #: writing anything: it is answered by these two lists and `reserved_count`.
    confirmed: bool = True
    #: How many of the attributed files replace a payslip that is already there — the count
    #: §6.3's confirmation names, read from the server's own rows rather than counted by the
    #: client. **The same number on both halves of the flow**: the dry run reads those rows
    #: before writing and the commit reads them as it writes, so a client that confirms a
    #: batch and compares the two answers sees this unchanged while `replaced_count` tells it
    #: what happened. It is deliberately *not* "overwrites still awaiting confirmation" —
    #: that reading makes the field mean one thing on the dry run and another after the
    #: commit, which is what its first version did and what a test comparing the two
    #: responses caught.
    reserved_count: int = 0


class MissingListRead(BaseModel):
    """The month's missing list, on its own.

    `expected` is how many people the derivation looked at, which is not the same number as
    `missing_count`: a screen that showed only the misses could not tell "nobody is missing"
    from "nobody was expected".
    """

    period: str
    expected: int
    missing_count: int
    items: list[MissingRead]


class BatchHistoryRead(BaseModel):
    """One stored batch, as the history lists it."""

    id: UUID
    period: str
    total_count: int
    success_count: int
    missing_employee_ids: list[UUID]
    unmatched: list[UnmatchedRead]
    created_at: str | None = None


class BatchPageRead(BaseModel):
    items: list[BatchHistoryRead]
    total: int
    limit: int
    offset: int


class PeriodWrite(StrictModel):
    """The month, for a caller that would rather not repeat it in a query string.

    Deliberately not used by the upload: that route takes its period as a form field
    alongside the files, because a multipart request has no JSON body to put it in.
    """

    period: str = Field(min_length=7, max_length=7, description="The month, YYYY-MM")


def _service(session: AsyncSession, principal: Principal) -> PayslipService:
    """The module, wired to its repository, its storage root and the notification centre.

    The storage root is the *payroll* module's, and it is a different path from
    `settings.document_storage_path` on purpose: a payslip is not a document, and keeping
    the two filesystems apart is the physical half of the decision `app/models/payslip.py`
    states — nothing that walks the document root can reach a payslip.
    """
    from app.config import get_settings
    from app.domain.document.storage import LocalFileStore
    from app.repositories.notification import PostgresNotificationRepository

    return PayslipService(
        PostgresPayslipRepository(session),
        session,
        principal=principal,
        storage=LocalFileStore(get_settings().payslip_storage_path),
        notifications=NotificationService(PostgresNotificationRepository(session), session),
    )


def _employee(ref: EmployeeRef) -> EmployeeRead:
    return EmployeeRead(
        employee_id=ref.employee_id,
        employee_no=ref.employee_no,
        employee_name=ref.employee_name,
        department_name=ref.department_name,
    )


def _payslip(entry: AttributedPayslip) -> PayslipRead:
    return PayslipRead(
        id=entry.payslip.id,
        period=entry.payslip.period,
        employee_id=entry.employee.employee_id,
        employee_name=entry.employee.employee_name,
        employee_no=entry.employee.employee_no,
        filename=entry.payslip.original_filename,
        file_size=entry.payslip.stored_size,
        content_sha256=entry.payslip.content_sha256,
        status=entry.payslip.status,
        replaced=entry.replaced,
        previous_sha256=entry.previous_sha256,
        previous_file_size=(
            str(entry.previous_file_size) if entry.previous_file_size is not None else None
        ),
        created_at=entry.payslip.created_at.isoformat() if entry.payslip.created_at else None,
    )


def _missing(entry: MissingEmployee) -> MissingRead:
    return MissingRead(
        employee_id=entry.employee.employee_id,
        employee_no=entry.employee.employee_no,
        employee_name=entry.employee.employee_name,
        department_name=entry.employee.department_name,
        salary_effective_from=entry.salary_effective_from,
        salary_effective_to=entry.salary_effective_to,
    )


def _outcome(outcome: BatchOutcome) -> BatchRead:
    return BatchRead(
        batch_id=outcome.batch_id,
        period=outcome.period,
        total_count=outcome.total_count,
        attributed_count=outcome.success_count,
        replaced_count=outcome.replaced_count,
        unmatched_count=len(outcome.unmatched),
        missing_count=len(outcome.missing),
        attributed=[_payslip(entry) for entry in outcome.attributed],
        unmatched=[
            UnmatchedRead(
                filename=entry.filename,
                reason=str(entry.reason),
                employee_no=entry.employee_no,
                detail=entry.detail,
            )
            for entry in outcome.unmatched
        ],
        missing=[_missing(entry) for entry in outcome.missing],
        # The ticket's 「不静默丢弃」, served as a boolean: every file is in exactly one of
        # the two lists, and a client — or a test — can check it without counting. The dry
        # run partitions too: it is the same matching, and a screen that showed a partial
        # answer before asking about the overwrite would be asking about the wrong upload.
        partitioned=outcome.partitioned(),
        created_at=outcome.created_at.isoformat() if outcome.created_at else None,
        confirmed=outcome.confirmed,
        reserved_count=len(outcome.reserved),
    )


def _listing(listing: MissingList) -> MissingListRead:
    return MissingListRead(
        period=listing.period,
        expected=listing.expected,
        missing_count=listing.missing,
        items=[_missing(entry) for entry in listing.items],
    )


def _batch(record: BatchRecord) -> BatchHistoryRead:
    return BatchHistoryRead(
        id=record.id,
        period=record.period,
        total_count=record.total_count,
        success_count=record.success_count,
        missing_employee_ids=list(record.missing_employee_ids),
        unmatched=[
            UnmatchedRead(
                filename=entry.filename,
                reason=str(entry.reason),
                employee_no=entry.employee_no,
                detail=entry.detail,
            )
            for entry in record.unmatched
        ],
        created_at=record.created_at.isoformat() if record.created_at else None,
    )


async def _read(file: UploadFile) -> bytes:
    """The part's bytes.

    Read whole, and no early ceiling: the size is the *result's* business rather than a
    refusal here, because a file that is too big has to appear in the answer with its
    reason (「不静默丢弃」) — which means it has to be read before it can be described.
    `MAX_PAYSLIP_BYTES` bounds what a single stored payslip may be; the request itself is
    bounded by the number of files and by whatever the deployment's proxy allows, and a
    batch of four hundred payslips is a few megabytes.
    """
    return await file.read()


@router.get(
    "/employees",
    response_model=list[EmployeeRead],
    summary="Who can be named for a file in this month",
    dependencies=[Depends(file_payslips)],
)
async def read_employees(
    period: str = Query(description="The month, YYYY-MM"),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> list[EmployeeRead]:
    """The people on the books in this month, with their staff numbers.

    Its own small read rather than a use of the employee directory, and the reason is the
    *number*: a payslip's filename is matched on `employee_no`, so the selector the screen
    offers for 「界面选择」 has to name people the way the files name them. The directory
    (`employee.directory`) is a contact list projected by §4.1 and does not carry the staff
    number at all; `/employees` is administration's. This is finance's, scoped to a month,
    and it is guarded by the same action as the upload it feeds.
    """
    period = parse_period(period)
    period_start, period_end = period_bounds(period)
    service = _service(session, principal)
    refs = await service.employee_ids_for(period_start, period_end)
    return [_employee(ref) for ref in refs]


@router.post(
    "/batches",
    response_model=BatchRead,
    status_code=201,
    summary="File a month's payslips, and answer with who is still missing one",
    dependencies=[Depends(file_payslips)],
)
async def upload_batch(
    files: Annotated[
        list[UploadFile], File(description="The month's payslips, one PDF per employee")
    ],
    period: Annotated[str, Form(description="The month, YYYY-MM")],
    employee_id: Annotated[
        list[UUID] | None,
        Form(
            description=(
                "Optional: the employee for each file, in the same order as `files`. "
                "Settles a file whose name carries no staff number; a selection that "
                "disagrees with a filename that names somebody else is reported as an "
                "unmatched file rather than attributed."
            )
        ),
    ] = None,
    confirm: Annotated[
        bool,
        Form(
            description=(
                "False for the dry run: the files are matched and answered with, and "
                "nothing is written. The screen sends False first so it can state what the "
                "overwrite would replace, and then True to commit the same upload."
            )
        ),
    ] = True,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> BatchRead:
    """One request, many files, for one month (the checklist's first line).

    The body is multipart: `period`, one or more `files`, and — when the filenames cannot
    answer for themselves — one `employee_id` per file, in the same order. The answer is
    the batch: what was attributed, what was refused and why, and who is missing a payslip
    altogether. **The missing list is derived from the payroll archive and from employment,
    not from the uploaded set** — see `domain/payslip/service.py` — so it answers "who
    should have had one" rather than "which files did I remember to attach".

    **`confirm=false` is the same request with nothing written.** The design system's §6.3
    says an overwrite is a dangerous operation that has to be confirmed 「明确写出将覆盖 X 名员工
    的 Y 月工资单」, and the count cannot be known before the files have been matched against
    the month's rows. So the first half does the matching and answers with the two lists plus
    `reserved_count`; the second half, with `confirm=true`, does exactly the same matching
    and writes it. Both halves answer the same shape, so a client does not branch.

    A 403 here is the design's separation of duties: finance files the month, HR keeps the
    archive, and an administrator sees neither. The refusal is the catalogue's
    (`payslip.manage`), recorded, and bilingual.
    """
    if employee_id is not None and len(employee_id) > len(files):
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            detail=(
                f"{len(employee_id)} employee selections for {len(files)} files; the "
                "selections are read in the order the files arrive"
            ),
        )
    if employee_id is not None and 0 < len(employee_id) < len(files):
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            detail=(
                f"{len(employee_id)} employee selections for {len(files)} files; send one "
                "per file in the same order, or none at all"
            ),
        )

    selections = list(employee_id or [])
    uploads = [
        UploadedFile(
            content=await _read(file),
            filename=file.filename,
            selected_employee_id=(selections[index] if index < len(selections) else None),
        )
        for index, file in enumerate(files)
    ]

    service = _service(session, principal)
    return _outcome(await service.upload(uploads, period, confirm=confirm))


@router.get(
    "/missing",
    response_model=MissingListRead,
    summary="Who should have a payslip this month and has none",
    dependencies=[Depends(file_payslips)],
)
async def read_missing(
    period: str = Query(description="The month, YYYY-MM"),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> MissingListRead:
    """The missing list, derived live, with no upload involved.

    Derived on every read rather than served from the last batch, and that is the point: a
    salary record entered after the upload, or a termination applied this morning, moves
    the list, and a screen that showed the month's *stored* answer would be telling finance
    about last week. The batch row keeps what the uploader was told at the time
    (`GET /payslips/batches`), which is the other question.

    Reading this writes one `salary.record_read` entry per expected employee: the module asks
    the archive through `SalaryService.read(...)`, which is the only method that serves
    those rows and the only one that records the look.
    """
    service = _service(session, principal)
    return _listing(await service.missing(period))


@router.get(
    "/missing/export",
    summary="The missing list as a CSV for finance to work from",
    dependencies=[Depends(export_missing)],
)
async def export_missing_list(
    period: str = Query(description="The month, YYYY-MM"),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> Response:
    """The same rows as the screen, as a file, with one audit entry for the act.

    Its own action (`payslip.export`), because the file carries every missing person's
    staff number: an installation may want the reading of the screen and the handing over
    of a payroll follow-up file to be separable decisions — the distinction ticket 26's
    overtime export draws for the same field. What the file states is in
    `domain/payslip/export.py`, including the list of things it refuses to carry: there is
    no amount in it anywhere.
    """
    service = _service(session, principal)
    export = await service.export_missing(period)
    return Response(
        content=export.content,
        media_type=export.content_type,
        headers={"Content-Disposition": f'attachment; filename="{export.filename}"'},
    )


@router.get(
    "/batches",
    response_model=BatchPageRead,
    summary="Which months were filed, by whom, and with what result",
    dependencies=[Depends(file_payslips)],
)
async def read_batches(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> BatchPageRead:
    """The upload history, newest first.

    Each row is what the upload *answered*: how many files it carried, how many it
    attributed, which files it refused and who was missing that month. It is stored rather
    than recomputed for the reason the columns exist — the live missing list moves as the
    archive and the staff change, and "what did finance see when they filed it" is a
    question about a moment.
    """
    service = _service(session, principal)
    page: BatchPage = await service.batches(limit=limit, offset=offset)
    return BatchPageRead(
        items=[_batch(record) for record in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


__all__ = ["router"]
