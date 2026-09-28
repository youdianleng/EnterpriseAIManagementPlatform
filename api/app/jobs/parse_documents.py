"""Parse the documents that are waiting for it.

    python -m app.jobs.parse_documents            # everything in `processing`
    python -m app.jobs.parse_documents <uuid>     # one document, whatever its status

Run it from cron, from the worker container, or on demand. One pass, one exit code,
and the same function is what the upload endpoint's contract points at: the request
writes a row in `processing` and returns, and this is the pass that takes it to `ready`
or to `failed`.

**One document, one transaction**, which is the property the ticket asks for two
different ways. A file that cannot be read fails *that document* and leaves every
document already parsed in this pass committed; a crash half way through leaves the
earlier ones done rather than rolling the batch back. The parser itself is pure — bytes
in, text and chunks out — so the transaction is short and holds no lock while thinking.

**Idempotent and safe to run twice**, and the guarantee is structural rather than
careful:

* the job's candidate query is "status is `processing`", and a document leaves that
  status in the same transaction that writes its chunks — so a second pass finds
  nothing to do rather than re-parsing everything;
* `replace_chunks` deletes before it inserts, so even a run that *did* re-parse a
  document cannot append a second set of chunks; the unique
  `(document_id, chunk_index)` is the guarantee underneath it;
* two workers may run at once. They will not agree about who parses what — the
  candidate query takes no lock, deliberately, because parsing the same document twice
  is a waste rather than a corruption, and a `SELECT ... FOR UPDATE SKIP LOCKED` here
  would hold a row lock for the length of a PDF parse.

**A failed parse is not an error of this job's.** A scanned file is the ordinary
outcome the ticket names — no OCR, `failed`, with the message the client shows — and it
is recorded, audited and counted rather than raised. Any *other* exception is a bug in
this pipeline, and it propagates: a document quietly marked `failed` because a parser
raised would hide the defect behind a status the operators are told to expect.

The exit code is 0 even when documents failed to parse, for the reason
`scan_attendance_anomalies` gives: one unparseable file must not turn the scheduler
into a nightly failure that stops being read. It is non-zero only when the pass itself
could not run.
"""

import asyncio
import sys
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import dispose_engine, get_session_factory
from app.domain.document.service import (
    DEFAULT_BATCH,
    DocumentService,
    publish_system_context,
)
from app.domain.document.storage import LocalFileStore
from app.logging import configure_logging, get_logger
from app.repositories.document import PostgresDocumentRepository

logger = get_logger(__name__)


@asynccontextmanager
async def system_session():  # noqa: ANN201 - an async context manager
    """A session that has declared itself the pipeline's.

    **This is not ceremony.** The row-level policies on `documents` admit a caller's own
    rows, their department's, and — with `app.current_system` published — the system's.
    The job has no user context at all: it read *zero* documents until this flag was
    published, and `parse_pending` reported "0 parsed, 0 failed" while looking healthy,
    which is precisely the silent failure the backstop is designed to produce for a
    query that forgot its filter. The job is not such a query; it is the pass the
    pipeline is made of, and it says so here, once, in the one place that does.
    """
    factory = get_session_factory()
    async with factory() as session:
        await publish_system_context(session)
        yield session


async def service_for(session: AsyncSession) -> DocumentService:
    """The module as the job needs it: a storage root, and no principal.

    `principal=None` is the honest shape here rather than a fabricated one. The job is
    not acting for a user — parsing a document is the system's own pass — and the
    reads it makes are by id. What bounds it is the database's own policy, which the
    connection is subject to like every other: the reach the pipeline needs is the
    `app.current_system` flag `system_session` publishes, and nothing else.

    That is why this constructor takes the storage and the batch size and nothing
    else: a job that could *answer a request* would need a principal, and one that does
    not have one cannot.
    """
    settings = get_settings()
    return DocumentService(
        PostgresDocumentRepository(session),
        session,
        principal=None,  # type: ignore[arg-type]
        storage=LocalFileStore(settings.document_storage_path),
        max_upload_bytes=settings.document_max_upload_bytes,
    )


async def parse_pending(*, limit: int = DEFAULT_BATCH) -> dict[str, int]:
    """One pass over what is waiting. Returns the counts the caller logs.

    Every document gets its own session and its own commit, so the failure of one is
    not the failure of the batch — which is what "one document per transaction" buys.
    """
    counted = {"parsed": 0, "failed": 0, "missing": 0}

    async with system_session() as session:
        pending = await (await service_for(session)).pending_documents(limit=limit)

    for document_id in pending:
        outcome = await parse_one(document_id)
        if outcome is None:
            counted["missing"] += 1
        elif outcome.succeeded:
            counted["parsed"] += 1
        else:
            counted["failed"] += 1
            logger.warning(
                "document_parse_failed",
                document_id=str(document_id),
                reason=outcome.failure,
            )
    return counted


async def parse_one(document_id: UUID):  # noqa: ANN201 - ParsedDocument | None
    """One document, one transaction.

    `None` when the document has gone — deleted between the candidate query and this
    call, which is a race this loses harmlessly rather than an error. A document whose
    status moved in the meantime is parsed anyway: the work is idempotent, so a second
    pass over one that is already ready rewrites the same rows with the same text, and
    that is what makes "just run it again" a safe answer for an operator.
    """
    async with system_session() as session:
        service = await service_for(session)
        if await PostgresDocumentRepository(session).get(document_id) is None:
            return None
        outcome = await service.parse_document(document_id)
        await session.commit()
    return outcome


async def run_forever(interval_seconds: int) -> None:
    """The optional in-process runner, off unless a setting turns it on.

    The same shape and the same reasoning as `apply_personnel_changes.run_forever`: the
    command above is the supported way to run this — from cron, a systemd timer or the
    worker container — and this exists because a development stack with no scheduler is
    common, and without it an uploaded document sits in `processing` for ever while the
    screen honestly says so. Two processes running it are safe: parsing is idempotent,
    and the candidate query takes no lock precisely because doing one document twice is
    a waste rather than a corruption.

    A failure inside a pass is logged and the loop carries on. A runner that stopped on
    the first unreadable file would be a runner that stops.
    """
    while True:
        try:
            counted = await parse_pending()
            if counted["parsed"] or counted["failed"]:
                logger.info("documents_parsed", **counted)
        except Exception as error:  # noqa: BLE001 - the loop must not die
            logger.error("document_parse_pass_failed", error=str(error))
        await asyncio.sleep(interval_seconds)


async def main(argv: list[str] | None = None) -> int:
    configure_logging(get_settings())
    arguments = list(sys.argv[1:] if argv is None else argv)

    try:
        if arguments:
            # An explicit id: parse this one whatever its status, which is what a
            # retry after a deploy looks like. A malformed id raises, and it should —
            # the only person who types one is somebody deliberately re-running it.
            document_id = UUID(arguments[0])
            outcome = await parse_one(document_id)
            if outcome is None:
                print(f"{document_id}: no such document")
                return 0
            print(
                f"{document_id}: {outcome.status} "
                f"chars={outcome.extracted_chars} chunks={outcome.chunk_count}"
                + (f" reason={outcome.failure}" if outcome.failure else "")
            )
            return 0

        counted = await parse_pending()
        logger.info("documents_parsed", **counted)
        print(
            f"{counted['parsed']} parsed, {counted['failed']} failed, "
            f"{counted['missing']} gone before parsing"
        )
    finally:
        await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
