"""The salary archive's endpoints: the chain, the day, and the one append.

Two reads and one write, and the shape of all three follows from the ticket's central
requirement:

* **Every read writes an audit entry, and no route here can serve a figure without one.**
  The rows travel inside a `SalaryReading`, which only `SalaryService.read` can issue,
  and that method is what writes the trail entry. A route that talked to the repository
  directly would have nothing to return; a route that forgot to call `read` would be a
  route with no payload. That is the design answer to 「薪酬数据的每一次读取都写审计日志」
  being easy to satisfy accidentally — the guarantee is a type, not a habit.
* **Nothing is computed.** `base_salary` and the allowance lines travel exactly as they
  are stored; there is no total, no annual figure and no conversion anywhere in this
  module (DESIGN D9, §8.3). The one derived-looking field a client might expect — the
  sum of the allowances — deliberately does not exist, and `tests/test_salary_records.py`
  asserts the response's numeric fields are exactly the stored ones.
* **A missing record is not a 403 and not a zero.** Somebody with no salary record — the
  ordinary case for a person in their first week — gets an empty chain and an `as_of`
  answer of `null`. "0.00 EUR" is a number a person could act on, and this module will
  not serve one that nobody entered. What *is* a 403 is asking about somebody else's
  archive without the reach for it, and the refusal is recorded by the kernel's own
  `audit_refusal`.

The disclaimer the checklist's last line asks for is served *with* the data rather than
composed by a client: `notice` carries a catalogue key and the three texts, so a
rendering client renders the reader's language and a client with no dictionary still has
the sentence. No screen is owed by this ticket (§8.1's landing table assigns the salary
surfaces to other tickets); making the sentence available to one is what is owed.
"""

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import current_principal, db_session, require, require_own
from app.api.v1.schemas.base import StrictModel
from app.domain.access import Action, Principal, ResourceKind
from app.domain.payroll.models import (
    ChangeReasonType,
    PayPeriod,
    RecordQuery,
    RecordView,
    SalaryReading,
    notice_payload,
)
from app.domain.payroll.service import SalaryService
from app.repositories.payroll import PostgresSalaryRepository

router = APIRouter(prefix="/salary", tags=["salary"])

#: Reading somebody else's archive, and entering a record. Route-level guards because
#: neither is about *which* record: the company read is a remit over the whole table and
#: the append names its subject in the body.
read_everyone = require(Action.SALARY_READ_ALL, ResourceKind.SALARY_RECORD)
write_records = require(Action.SALARY_WRITE, ResourceKind.SALARY_RECORD)

#: The `as_of` route is the one read that may be about *either* the caller or somebody
#: else, and the subject arrives in the query string — so, exactly as for attendance and
#: leave, the role-level guard is "signed in" and the kernel is asked inside the handler
#: against the subject the request named. A route-level `salary.read_all` would refuse an
#: employee their own figure, and a route-level `salary.read_own` would refuse HR the
#: company's: neither action is the route's, because the *subject* decides which applies.
signed_in = require(Action.SESSION_READ_OWN, ResourceKind.ACCOUNT)


class ComponentWrite(StrictModel):
    """One allowance line. `amount` is a decimal *string*, deliberately.

    JSON has one number type and it is a float; `0.1 + 0.2` is already
    `0.30000000000000004` there, and more than fifteen significant digits are rounded on
    the way in. A string is the one representation of money that survives the round trip
    unchanged, and the field is documented as a string so a client sends one.
    """

    code: str = Field(min_length=1, max_length=40, description="What payroll matches on")
    label: str = Field(
        min_length=1, max_length=120, description="What a person reads, e.g. 'Transporte'"
    )
    amount: str = Field(
        min_length=1,
        max_length=20,
        description="Exact decimal, at most two places, e.g. \"120.00\"",
    )


class RecordWrite(StrictModel):
    """A new record. There is no update and no delete: the archive is append-only.

    `effective_to` is optional — the record in force has no stated end — and the same
    employee's next record may start the day after this one ends. A window another record
    already covers is refused with the date that collided, so the caller can move the
    start rather than guess.
    """

    employee_id: UUID
    effective_from: date
    effective_to: date | None = None
    base_salary: str = Field(
        min_length=1,
        max_length=20,
        description="Exact decimal, at most two places, e.g. \"33000.00\"",
    )
    currency: str = Field(default="EUR", min_length=3, max_length=3)
    pay_period: PayPeriod
    components: list[ComponentWrite] = Field(
        default_factory=list, description="The structured allowance breakdown"
    )
    change_reason_type: ChangeReasonType
    change_reason: str = Field(min_length=1, max_length=500)


class ComponentRead(BaseModel):
    code: str
    label: str
    amount: str


class NoticeRead(BaseModel):
    """The archive disclaimer, as data rather than as a client's own sentence.

    `message_key` is the contract — a catalogue key the interface looks its own copy up
    by — and `text` carries the same statement in the languages the interface ships, so a
    client with no dictionary still renders it. Ticket 36's `source_notice` is the same
    shape for the same reason; `tests/test_salary_records.py` asserts this key is the one
    the catalogue holds.
    """

    message_key: str
    text: dict[str, str] = Field(default_factory=dict)


class RecordRead(BaseModel):
    """One record as a response carries it.

    Everything here is *stored*: the window, the base figure, the currency, the period,
    the allowance lines, why the record exists and who entered it. No field is computed,
    and none may be added that is (D9), which is why the contract is stated in one schema
    rather than assembled per route.
    """

    id: UUID
    employee_id: UUID
    employee_name: str
    effective_from: date
    effective_to: date | None = None
    base_salary: str
    currency: str
    pay_period: str
    components: list[ComponentRead]
    change_reason_type: str
    change_reason: str
    created_by_user_id: UUID | None = None
    created_at: str | None = None


class ChainRead(BaseModel):
    """One person's chain, oldest first, with the notice that goes with it.

    `notice` travels with the *page* rather than inside each record: it is a fact about
    the surface, and repeating it per row would be the same sentence N times. The one
    place it is also served with a record is the `POST`'s answer, because that response
    is what a screen renders right after an entry and it should not have to hold two
    shapes.
    """

    employee_id: UUID
    as_of: date | None = None
    items: list[RecordRead]
    total: int
    limit: int
    offset: int
    notice: NoticeRead
    #: True when the subject has no record in force *on the day asked about*. Present
    #: rather than inferred from `items == []` so a client does not have to guess which
    #: of the two empty answers it received.
    empty: bool = False


def _service(session: AsyncSession, principal: Principal) -> SalaryService:
    return SalaryService(
        PostgresSalaryRepository(session), session, principal=principal
    )


def _record(view: RecordView) -> RecordRead:
    return RecordRead(
        id=view.record.id,
        employee_id=view.record.employee_id,
        employee_name=view.employee_name,
        effective_from=view.record.effective_from,
        effective_to=view.record.effective_to,
        # The stored figure, exactly: `stored_amount` is the one place the wire form is
        # stated, so no route can serialise an amount a second way.
        base_salary=view.record.stored_amount,
        currency=view.record.currency,
        pay_period=view.record.pay_period,
        components=[
            ComponentRead(code=line.code, label=line.label, amount=line.amount)
            for line in view.record.components
        ],
        change_reason_type=view.record.change_reason_type,
        change_reason=view.record.change_reason,
        created_by_user_id=view.record.created_by_user_id,
        created_at=view.record.created_at.isoformat() if view.record.created_at else None,
    )


def _page(reading: SalaryReading, *, limit: int, offset: int) -> ChainRead:
    """The page, with the *count* the read reported rather than the page's own length.

    `reading.total` is the same filter the rows were read with, asked as a count, so a
    client paginating a long chain is told how long it is. `empty` is stated rather than
    left to `items == []` because the two empty answers — "no records at all" and "none
    in force on the day you asked about" — are different facts a client shows
    differently.
    """
    return ChainRead(
        employee_id=reading.subject,
        as_of=reading.as_of,
        items=[_record(view) for view in reading.rows],
        total=reading.total,
        limit=limit,
        offset=offset,
        notice=NoticeRead(**notice_payload()),
        empty=len(reading.rows) == 0,
    )


async def _require_reading(request: Request, principal: Principal, subject: UUID) -> None:
    """Whether the caller may read this person's archive, and as what.

    Two catalogued actions answer it, and which one applies is a fact about the caller and
    the subject rather than about the route: your own (`salary.read_own`, self-only) or
    the company's (`salary.read_all`, HR and finance). The kernel is asked, and the
    refusal is recorded against the action that was actually attempted — which is what
    makes 员工只能看自己的 and 访问他人薪酬返回 403 并写审计 the same sentence read twice.

    Rather than a route-level `require(...)`, for the reason the attendance and leave
    surfaces give: the subject arrives in the query string, and a dependency runs before
    the route has parsed it.
    """
    action = (
        Action.SALARY_READ_OWN
        if subject == principal.employee_id
        else Action.SALARY_READ_ALL
    )
    await require_own(request, principal, action, ResourceKind.SALARY_RECORD, subject)


@router.get(
    "/records/me",
    response_model=ChainRead,
    summary="Read your own salary archive",
    dependencies=[Depends(require(Action.SALARY_READ_OWN, ResourceKind.SALARY_RECORD))],
)
async def read_own_records(
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ChainRead:
    """Your own chain, oldest first — and an audit entry that says you looked.

    This is the self-read the checklist names, and it is deliberately *not* free: the
    same person reading their own figure through this endpoint writes
    `salary.record_read` exactly as HR does when they read it for them.
    """
    service = _service(session, principal)
    query = RecordQuery(employee_id=principal.employee_id, limit=limit, offset=offset)
    reading = await service.read(query)
    return _page(reading, limit=limit, offset=offset)


@router.get(
    "/records",
    response_model=ChainRead,
    summary="Read somebody's salary archive",
    dependencies=[Depends(read_everyone)],
)
async def read_records(
    employee_id: UUID = Query(description="Whose archive"),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ChainRead:
    """The chain for one employee, oldest first.

    HR's and finance's reach (`salary.read_all`), and the one list read in this module:
    it is a read, so it writes its own audit entry — one entry naming the subject and the
    number of rows, not one per row. A chain with no rows is an empty page and still a
    recorded look: somebody in their first week has no record, and that is an answer.
    """
    service = _service(session, principal)
    reading = await service.read(
        RecordQuery(employee_id=employee_id, limit=limit, offset=offset)
    )
    return _page(reading, limit=limit, offset=offset)


@router.get(
    "/records/as-of",
    response_model=ChainRead,
    summary="Read what was in force on a date",
    dependencies=[Depends(signed_in)],
)
async def read_as_of(
    request: Request,
    as_of: date = Query(description="The day to ask about, YYYY-MM-DD"),
    employee_id: UUID | None = Query(default=None, description="Whose; defaults to your own"),
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> ChainRead:
    """「任一时点可查出当时有效的值」 — the one record whose window covers `as_of`.

    A separate route from the chain because it answers a different question: the chain is
    "everything that ever applied", this is "what applied on this day". The fold is a
    range predicate over the indexed window — never a loop over rows and never a
    comparison of amounts — so it is the same rule the exclusion constraint enforces,
    asked the other way round.

    `employee_id` defaults to the caller's own, and naming anybody else requires
    `salary.read_all`: a manager asking about a report is refused here exactly as they are
    on the chain. When nothing covers the day, the answer is `items: []` and `empty:
    true` — **not** a record whose figure is zero, which a person could act on.
    """
    subject = employee_id or principal.employee_id
    await _require_reading(request, principal, subject)
    service = _service(session, principal)
    reading = await service.read(RecordQuery(employee_id=subject, as_of=as_of, limit=1))
    return _page(reading, limit=1, offset=0)


@router.post(
    "/records",
    response_model=RecordRead,
    status_code=201,
    summary="Enter a salary record",
    dependencies=[Depends(write_records)],
)
async def append_record(
    payload: RecordWrite,
    principal: Principal = Depends(current_principal),
    session: AsyncSession = Depends(db_session),
) -> RecordRead:
    """Enter one record. Nothing existing is rewritten.

    HR's (`salary.write`). The body carries the facts only — the actor comes from the
    session, never from the request — so "who entered it" is a recorded fact rather than a
    claim. A window another record already covers is a 409 naming the date, and the same
    overlap is refused by the database's exclusion constraint whether or not this service
    noticed it.
    """
    service = _service(session, principal)
    view = await service.append(payload.model_dump())
    return _record(view)


__all__ = ["router"]
