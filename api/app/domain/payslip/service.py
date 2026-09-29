"""The payslip module's service: one upload, one derived list, one file.

Three operations, and the shape of all three follows from the ticket's central
requirement — that the screen be *trustworthy*, because a file that vanishes is a payslip
somebody never receives:

    upload(files, period)      -> BatchOutcome
    missing(period)            -> MissingList
    export_missing(period)     -> ExportFile

**The upload's answer partitions the input, and this class is where that is arranged.**
`BatchOutcome.partitioned()` is asserted on every batch the tests build, and what makes it
true is that the loop in `upload` has exactly two exits: a file is either upserted against
an employee, or it is appended to `unmatched` with a reason. There is no `continue` that
drops one, no exception that skips one, and no third list for it to fall into —
「不静默丢弃」 as a property of the control flow rather than of a reviewer's attention.

**The missing list is derived from the payroll archive and from employment, never from the
upload.** That is the ticket's 「依据在职状态与薪酬档案判定」, and it is what makes the list
worth reading: it answers "who should have had one" rather than "which files did I forget
to attach". The derivation goes through **`SalaryService.read(...)`** and not around it:
salary rows travel only inside a `SalaryReading`, whose constructor refuses anything the
archive's own service did not issue, so the only way to ask "was a salary in force in this
month" is the method that also writes the audit entry for asking. A batch therefore leaves
one `salary.record_read` entry per expected employee — which is the correct trail for this
act: "finance looked at every expected employee's archive when it filed March" is exactly
the access a compliance reader wants to see, and it is a fact no route-level `record()`
call would have captured.

**A payslip never becomes retrievable content, and that is a decision about *storage*
rather than a flag.** The file goes to the payroll storage root through the same
`FileStore` seam documents use, and the row is written to `payslips`. Nothing is added to
`documents` and no chunk is ever produced, so the retrieval path — which reads
`document_chunks` joined to `documents`, filtered by §4.2 — cannot reach a payslip at all,
for anybody. The alternative (ticket 36's `is_company_kb`) does not work and the reason is
worth writing down: `repositories/retrieval.py` clause 1 is "a personal document the
caller owns", so a payslip filed as a personal document *would* be retrievable by its
owner, and `is_company_kb` moves a document to a wider rule, never a narrower one.
`tests/test_payslips.py` proves it through the retrieval path: a payslip's text is
searched for by its owner and by finance, and `documents` is empty afterwards.

**Nothing here reads a payslip's contents.** The file is hashed (for the replacement
check), sized, and stored. There is no parsing, no amount, no total and no tax — D9 and
§8.3 — and this module has no code path that could produce one.
"""

from dataclasses import dataclass
from datetime import date
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction, record
from app.domain.access.kernel import apply_rls_context
from app.domain.access.principal import Principal
from app.domain.document.storage import FileStore, content_digest
from app.domain.errors import DomainError
from app.domain.notification.models import NotificationDraft, NotificationType
from app.domain.notification.service import NotificationService
from app.domain.payroll import RecordQuery, SalaryService
from app.domain.payslip import export as export_module
from app.domain.payslip.errors import PayslipErrorCode
from app.domain.payslip.matching import duplicate_positions, resolve
from app.domain.payslip.models import (
    BATCH_ENTITY,
    MAX_BATCH_FILES,
    AttributedPayslip,
    BatchOutcome,
    BatchPage,
    BatchRecord,
    EmployeeRef,
    ExportFile,
    MissingEmployee,
    MissingList,
    Payslip,
    PayslipStatus,
    UnmatchedFile,
    UnmatchedReason,
    UploadedFile,
    parse_period,
    period_bounds,
)
from app.repositories.payroll import PostgresSalaryRepository
from app.repositories.payslip import PostgresPayslipRepository


class PayslipService:
    """The module, as the api layer uses it.

    `principal` is construction state rather than a parameter of every method, for the
    reason `SalaryService` records: the reach is a *property of the caller*, and a method
    that took it as an argument would make "whose files are these" a question a call site
    could answer wrongly. It is used for two things: the audit entries' actor, and the
    salary reads — which go through `SalaryService`, whose own `read` applies the reach.
    """

    def __init__(
        self,
        repository: PostgresPayslipRepository,
        session: AsyncSession,
        *,
        principal: Principal,
        storage: FileStore,
        notifications: NotificationService | None = None,
    ) -> None:
        self._repository = repository
        self._session = session
        self._principal = principal
        self._storage = storage
        self._notifications = notifications

    # --- the one upload -----------------------------------------------------

    async def upload(
        self, files: list[UploadedFile], period: str, *, confirm: bool = True
    ) -> BatchOutcome:
        """File a month's payslips, and answer with what happened to every one of them.

        The order of the work, and each step is a decision:

        1. **the month, and the files.** A batch with no files is refused rather than
           answered with an empty result, and a batch over `MAX_BATCH_FILES` is refused
           rather than quietly truncated — a truncated batch is the silent loss the ticket
           forbids.
        2. **the explicit selections are resolved first**, because a selection naming
           nobody is the *request's* mistake (a 404) rather than a file's, and answering it
           as "that file matched nobody" would hide a broken client behind a payslip's
           absence.
        3. **every file is classified and matched**, and the two exits are the partition:
           attributed, or unmatched-with-a-reason.
        4. **the month's expected list is derived** — after the attributions, so the people
           this very upload satisfied are not reported missing.
        5. **the rows are written**, then the batch row, then the notifications, then the
           audit entries; one commit at the end, so a month finance believes it filed is a
           month that is actually there.
        """
        period = parse_period(period)
        if not files:
            raise DomainError(
                PayslipErrorCode.BATCH_EMPTY,
                detail="an upload with no files would record a batch that filed nothing",
            )
        if len(files) > MAX_BATCH_FILES:
            raise DomainError(
                PayslipErrorCode.BATCH_EMPTY,
                detail=(
                    f"{len(files)} files is more than the {MAX_BATCH_FILES} one batch holds; "
                    "send the month in two uploads rather than having this one truncated"
                ),
            )

        period_start, period_end = period_bounds(period)
        candidate_ids = await self._repository.candidates(period_start, period_end)
        everything = await self._repository.employee_refs(candidate_ids)
        by_id = {ref.employee_id: ref for ref in everything}

        selected: dict[int, EmployeeRef] = {}
        for index, file in enumerate(files):
            chosen = file.selected_employee_id
            if chosen is None:
                continue
            ref = by_id.get(chosen)
            if ref is None:
                # The selection may name somebody this month's candidate list does not
                # hold — an employee hired after the month, or one who has left. Ask the
                # repository for the person rather than the month: a 404 is about the
                # *employee*, not about the period.
                ref = await self._repository.employee_ref(chosen)
            if ref is None:
                raise DomainError(
                    PayslipErrorCode.EMPLOYEE_NOT_FOUND,
                    detail=f"no employee {chosen}",
                )
            selected[index] = ref

        digests = [content_digest(file.content) for file in files]
        repeated = duplicate_positions(digests)

        # **The batch row is created *after* the dry run, and that ordering is the point of
        # the dry run.** `confirm=False` is the screen's first half: §6.3's third rule says
        # the overwrite has to be confirmed by naming how many employees and which month,
        # and neither the client nor the server knows that until the files have been matched.
        # So the matching happens first, and a request that has not been confirmed writes
        # nothing at all — no batch row, no file, no notification.
        prepared = self._prepare(files, everything, selected, repeated)
        if not confirm:
            return await self._preview(files, period, prepared)

        batch = await self._repository.create_batch(
            period=period,
            uploaded_by_user_id=self._principal.user_id,
            total_count=0,
            success_count=0,
            missing_employee_ids=[],
            unmatched=[],
        )
        # The batch row is committed (see the repository), which ends the row-level context;
        # every write below needs it back.
        await self._recontextualise()

        attributed: list[AttributedPayslip] = []
        unmatched: list[UnmatchedFile] = prepared.unmatched
        for claim in prepared.attributed:
            attributed.append(
                await self._store(files[claim.index], claim.employee_id, period, batch.id)
            )

        # **What the commit replaced, reported with the same meaning the dry run gave it.**
        # `reserved` is "the payslips that were already there for these employees and this
        # month, when this upload read them" — a fact about the *month* rather than about
        # which half of the flow is answering. `_store` already read each of them to decide
        # `replaced`, so this is those rows and not a second question.
        #
        # The first version reported it only on the dry run, which gave one field two
        # meanings: "overwrites awaiting confirmation" in one response and "overwrites
        # performed" in the other. A client that confirmed a batch and then read
        # `reserved_count: 0` beside `replaced_count: 1` could not tell whether the
        # replacement had happened. Found by comparing a dry run and a commit of the same
        # upload against each other — which is what the test does, and why it does it.
        reserved = tuple(entry.previous for entry in attributed if entry.previous is not None)

        # The month's expectation, derived *after* the attributions so the people this
        # upload satisfied are not reported missing.
        expected = await self._expected(period, period_start, everything)
        # The derivation commits once per employee (see `_expected`), so the context the
        # batch row and the notifications are written under has to be re-established.
        await self._recontextualise()
        filed = await self._repository.published_employee_ids(period)
        claimed_ids = {claim.employee_id for claim in prepared.attributed}
        missing = tuple(
            entry
            for entry in expected
            if entry.employee.employee_id not in filed
            and entry.employee.employee_id not in claimed_ids
        )

        batch = await self._repository.finalize_batch(
            batch.id,
            total_count=len(files),
            success_count=len(attributed),
            missing_employee_ids=[entry.employee.employee_id for entry in missing],
            unmatched=[
                {
                    "filename": entry.filename,
                    "reason": str(entry.reason),
                    "employee_no": entry.employee_no,
                    "detail": entry.detail,
                }
                for entry in unmatched
            ],
        )

        await record(
            self._session,
            action=AuditAction.PAYSLIP_UPLOADED,
            entity_type=BATCH_ENTITY,
            entity_id=batch.id,
            after={
                "period": period,
                "files": len(files),
                "attributed": len(attributed),
                "replaced": sum(1 for entry in attributed if entry.replaced),
                "unmatched": len(unmatched),
                "missing": len(missing),
            },
            reason=f"the {period} payslips were uploaded",
        )
        await self._publish(attributed, batch)
        await self._repository.commit()

        return BatchOutcome(
            batch_id=batch.id,
            period=period,
            uploaded_by_user_id=batch.uploaded_by_user_id,
            total_count=batch.total_count,
            attributed=tuple(attributed),
            reserved=reserved,
            unmatched=tuple(unmatched),
            missing=missing,
            created_at=batch.created_at,
        )

    # --- the matching half, which writes nothing -----------------------------

    def _prepare(
        self,
        files: list[UploadedFile],
        everyone: list[EmployeeRef],
        selected: dict[int, EmployeeRef],
        repeated: set[int],
    ) -> "_Prepared":
        """Match every file, with the two exits that make the partition true.

        A file is either attributed to an employee the upload *claimed*, or appended to
        `unmatched` with a reason — and there is no third branch, no `continue` that drops
        one and no exception that skips one. That is 「不静默丢弃」 as a property of the
        control flow: the claim set and the attribution list are built here and the commit
        phase walks the same lists, so the two cannot disagree about which file went where.
        """
        attributed: list[_Claim] = []
        unmatched: list[UnmatchedFile] = []
        claimed: dict[UUID, int] = {}

        for index, file in enumerate(files):
            filename = _display_name(file)
            if index in repeated:
                unmatched.append(
                    UnmatchedFile(
                        filename=filename,
                        reason=UnmatchedReason.DUPLICATE_FILE,
                        detail="the same bytes appeared earlier in this upload",
                    )
                )
                continue

            resolution = resolve(file, everyone, selected=selected.get(index))
            if resolution.employee_id is None:
                unmatched.append(
                    UnmatchedFile(
                        filename=filename,
                        reason=resolution.reason or UnmatchedReason.NO_EMPLOYEE_NUMBER,
                        employee_no=resolution.employee_no,
                        detail=resolution.detail,
                    )
                )
                continue

            if resolution.employee_id in claimed:
                first = claimed[resolution.employee_id]
                unmatched.append(
                    UnmatchedFile(
                        filename=filename,
                        reason=UnmatchedReason.DUPLICATE_FOR_EMPLOYEE,
                        employee_no=resolution.employee_no,
                        detail=(
                            "this employee already has a file in this upload "
                            f"(the first was {_display_name(files[first])!r}); one payslip "
                            "per employee and month, so the second would replace the first"
                        ),
                    )
                )
                continue

            claimed[resolution.employee_id] = index
            attributed.append(_Claim(index=index, employee_id=resolution.employee_id))

        return _Prepared(attributed=attributed, unmatched=unmatched)

    async def _preview(
        self, files: list[UploadedFile], period: str, prepared: "_Prepared"
    ) -> BatchOutcome:
        """The dry run's answer: the two lists, and what the upload *would* replace.

        Nothing is written. `replaced` and `previous_sha256` are read from the rows that are
        already there, so the confirmation the screen shows is computed from the database
        rather than guessed — and the figures the commit will produce are the ones the
        uploader agreed to.
        """
        claimed_ids = [entry.employee_id for entry in prepared.attributed]
        reserved = await self._repository.existing_by_period(period, claimed_ids)
        by_id = {ref.employee_id: ref for ref in await self._repository.employee_refs(claimed_ids)}
        rows = []
        for entry in prepared.attributed:
            existing = reserved.get(entry.employee_id)
            rows.append(
                AttributedPayslip(
                    payslip=Payslip(
                        # No row exists yet, and there will not be one until this upload is
                        # confirmed. A fresh id rather than a null: the response schema is
                        # the same shape in both halves of the flow, so a client does not
                        # have to branch on which half it is reading.
                        id=uuid4(),
                        employee_id=entry.employee_id,
                        period=period,
                        storage_path="",
                        file_size=len(files[entry.index].content),
                        content_sha256=content_digest(files[entry.index].content),
                        original_filename=_display_name(files[entry.index]),
                        uploaded_by_user_id=self._principal.user_id,
                        batch_id=uuid4(),
                        status=str(PayslipStatus.PUBLISHED),
                    ),
                    employee=by_id[entry.employee_id],
                    replaced=existing is not None,
                    previous_sha256=existing.content_sha256 if existing else None,
                    previous_file_size=existing.file_size if existing else None,
                    previous=existing,
                )
            )
        return BatchOutcome(
            batch_id=uuid4(),
            period=period,
            uploaded_by_user_id=self._principal.user_id,
            total_count=len(files),
            attributed=tuple(rows),
            unmatched=tuple(prepared.unmatched),
            missing=(),
            confirmed=False,
            # The dry run's own reading, which is the same set the commit will report: the
            # rows that were there before this upload. See `upload`'s comment on the two
            # halves of the flow sharing one meaning for this field.
            reserved=tuple(reserved.values()),
        )

    async def employee_ids_for(self, period_start: date, period_end: date) -> list[EmployeeRef]:
        """The people on the books in this window, for the screen's per-file selector.

        The same candidate rule the derivation uses, exposed as a read: the selector must
        offer exactly the people a payslip could be *for*, and a list built any other way
        would let the screen name somebody the derivation does not consider — or, worse,
        hide somebody it does.
        """
        ids = await self._repository.candidates(period_start, period_end)
        return await self._repository.employee_refs(ids)

    async def _store(
        self, file: UploadedFile, employee_id: UUID, period: str, batch_id: UUID
    ) -> AttributedPayslip:
        """Write one file's row, reporting whether it replaced one.

        The previous row is read first (see `existing_by_period`'s reasoning) — both for
        the answer the uploader needs and because its checksum is what makes the
        replacement checkable afterwards rather than merely announced.
        """
        ref = await self._repository.employee_ref(employee_id)
        if ref is None:  # pragma: no cover - the candidate list held this id
            raise DomainError(
                PayslipErrorCode.EMPLOYEE_NOT_FOUND, detail=f"no employee {employee_id}"
            )

        digest = content_digest(file.content)
        from app.domain.document.files import safe_filename

        name = safe_filename(file.filename)
        existing = await self._repository.existing_by_period(period, [employee_id])
        previous = existing.get(employee_id)

        key = self._storage.put(file.content, extension=".pdf")
        payslip = await self._repository.upsert(
            employee_id=employee_id,
            period=period,
            storage_path=key,
            file_size=len(file.content),
            content_sha256=digest,
            original_filename=name,
            uploaded_by_user_id=self._principal.user_id,
            batch_id=batch_id,
        )
        return AttributedPayslip(
            payslip=payslip,
            employee=ref,
            replaced=previous is not None,
            previous_sha256=previous.content_sha256 if previous is not None else None,
            previous_file_size=previous.file_size if previous is not None else None,
            # The row that was there, kept whole so the commit's own answer can report the
            # same `reserved` set the dry run reported. See `upload`.
            previous=previous,
        )

    # --- the missing list ---------------------------------------------------

    async def missing(self, period: str) -> MissingList:
        """Who should have a payslip for this month and has none, derived live.

        See the module docstring: the expectation comes from employment and the payroll
        archive, and the "has none" half counts only a **published** row — a withdrawn
        payslip is one the employee cannot see, so its subject is missing one again.
        """
        period = parse_period(period)
        period_start, period_end = period_bounds(period)
        candidate_ids = await self._repository.candidates(period_start, period_end)
        everything = await self._repository.employee_refs(candidate_ids)
        expected = await self._expected(period, period_start, everything)
        filed = await self._repository.published_employee_ids(period)
        items = tuple(entry for entry in expected if entry.employee.employee_id not in filed)
        return MissingList(period=period, items=items, expected=len(expected))

    async def export_missing(self, period: str) -> ExportFile:
        """The missing list as finance's CSV, with one audit entry for the act.

        `data.exported` rather than a code of its own: the catalogue names one action for
        "a file left the building", and what this writes is the month, the row count and
        the fact that it carried no amounts — which is the audit the ticket asks for.
        """
        period = parse_period(period)
        listing = await self.missing(period)
        file = export_module.file_for(period, listing.items)
        await record(
            self._session,
            action=AuditAction.DATA_EXPORTED,
            entity_type=export_module.EXPORT_ENTITY,
            entity_id=None,
            after={
                "report": "payslip_missing",
                "period": period,
                "rows": len(listing.items),
                "expected": listing.expected,
                # Stated in the trail because it is the file's most important property and
                # the one a reader cannot check afterwards: the export carries no amount.
                "carries_amounts": False,
            },
            reason=f"the {period} missing-payslip list was exported",
        )
        await self._repository.commit()
        return file

    async def batches(self, *, limit: int = 20, offset: int = 0) -> BatchPage:
        """Every upload, newest first. The history, not the month."""
        return await self._repository.batches(limit=limit, offset=offset)

    async def latest_batch(self, period: str | None = None) -> BatchRecord | None:
        """The most recent upload — this month's when a period is given."""
        return await self._repository.latest_batch(parse_period(period) if period else None)

    # --- internals ----------------------------------------------------------

    async def _expected(
        self, period: str, period_start: date, everyone: list[EmployeeRef]
    ) -> list[MissingEmployee]:
        """The people a payslip was owed to this month, and the archive's reason.

        One `SalaryService.read(...)` per candidate, with `as_of` set to the month's first
        day: that is the archive's own way of asking 「was something in force then」, it is
        a range predicate in SQL rather than a fold over rows, and it is the only method
        that returns salary rows — so every one of these looks is recorded. A person with
        no record in force is *not* expected (`read` answers an empty chain, which is a
        look and not a zero), and neither is somebody the caller's reach excludes.

        The record's window is carried into the answer because it is why the person is
        expected; the figure is not, and never will be: what somebody earns is the
        archive's business and this list's job is to say who is missing a payslip.
        """
        payroll = SalaryService(
            PostgresSalaryRepository(self._session), self._session, principal=self._principal
        )
        expected: list[MissingEmployee] = []
        for ref in everyone:
            reading = await payroll.read(
                RecordQuery(employee_id=ref.employee_id, as_of=period_start, limit=1)
            )
            # **The context is re-published after every read, and this is not tidiness.**
            # `SalaryService.read` commits — that is how "every read carries its own audit
            # entry" is implemented — and the row-level context is published with
            # `set_config(..., is_local => true)`, which is *transaction*-scoped: a commit
            # ends it. Without this line the second employee's read runs with no
            # `app.current_roles`, and the policy on `salary_records` answers
            # 「no rows」 to a finance officer — silently, as an empty archive, so the missing
            # list comes back empty and looks like an answer. The same is true of every
            # write this module makes after the derivation, which is why the re-publish is a
            # method rather than a line here: see `_recontextualise`.
            await self._recontextualise()
            if not reading.rows:
                continue
            record_row = reading.rows[0].record
            expected.append(
                MissingEmployee(
                    employee=ref,
                    salary_effective_from=record_row.effective_from,
                    salary_effective_to=record_row.effective_to,
                )
            )
        return expected

    async def _recontextualise(self) -> None:
        """Publish the caller back to the database, after a commit ended the last context.

        `access.kernel.apply_rls_context` writes the principal with
        `set_config(..., is_local => true)`, which scopes it to the *transaction*. That is
        the right scope for a request that reads and writes once — it cannot leak to
        whatever borrows the pooled connection next — and it is the wrong one for this
        module, whose derivation commits once per expected employee (because the only path
        that serves a salary row is the one that writes its own audit entry).

        So the context is re-established whenever this module knows a commit happened. What
        it prevents is not a crash but a *wrong answer*: without it the second employee's
        salary read runs with no `app.current_roles`, the policy returns no rows to a finance
        officer, and the missing list comes back short — which reads exactly like good news.
        """
        await apply_rls_context(self._session, self._principal)

    async def _publish(self, attributed: list[AttributedPayslip], batch: BatchRecord) -> None:
        """Tell the people whose payslips were just filed.

        One notification per person, keyed on the batch: the dedupe key is
        `(recipient, type, entity, event)`, and the events are one batch each — so a
        re-upload of the same month is a *new* notification (something did change for that
        person: their payslip was replaced) while a retry of one request is suppressed.

        A person who was filed twice in one batch cannot be, by construction: the upload
        refuses the second file rather than replacing within itself.

        The notification is the ticket's 「上传完成自动通知相关员工」. What it carries is the
        month and no figure — the payload is the month and the batch, and the file is what
        the employee opens.
        """
        if self._notifications is None:
            return
        for entry in attributed:
            await self._notifications.notify(
                NotificationDraft(
                    recipient_employee_id=entry.payslip.employee_id,
                    type=NotificationType.PAYSLIP_PUBLISHED,
                    payload={"period": entry.payslip.period},
                    entity_type=BATCH_ENTITY,
                    entity_id=batch.id,
                    event=str(entry.payslip.employee_id),
                )
            )
            # `notify` commits — one notification per transaction, which is how it records
            # a suppression — so the next one is raised against a fresh transaction whose
            # context this re-publishes. Without it the second recipient's insert runs with
            # no roles and the notification policy refuses it.
            await self._recontextualise()

    @property
    def storage(self) -> FileStore:
        """The store the files were written to. Exposed so ticket 45 can read one back."""
        return self._storage


@dataclass(frozen=True, slots=True)
class _Claim:
    """One file this upload will attribute, and the employee it belongs to.

    An index rather than the `UploadedFile` itself: the commit phase reads the bytes out of
    the list the request handed over, and carrying the object through the matching phase
    would put a copy of every upload in the claim.
    """

    index: int
    employee_id: UUID


@dataclass(frozen=True, slots=True)
class _Prepared:
    """The matching phase's whole output: who gets a file, and which files get nobody.

    These two lists are the partition, and the commit phase walks exactly them — which is
    what makes 「不静默丢弃」 structural rather than something a second loop has to remember.
    """

    attributed: list[_Claim]
    unmatched: list[UnmatchedFile]


def _display_name(file: UploadedFile) -> str:
    """The name a result reports for this file: normalised, never the raw one.

    The raw name can carry a path or a control character and travels into a
    `Content-Disposition` header once ticket 45 hands the file back, so
    `document.files.safe_filename` is applied here too — the same rule, applied to the
    name that will be shown rather than only to the name that is stored.
    """
    from app.domain.document.files import safe_filename

    try:
        return safe_filename(file.filename)
    except Exception:  # noqa: BLE001 - an unusable name is shown as the fallback
        return "document"


__all__ = ["PayslipService"]
