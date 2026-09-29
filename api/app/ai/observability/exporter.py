"""The one place from which a trace leaves this process.

**The single export point, and how the tests prove it is single.** `TraceExporter.export`
is the only function in `app/` that hands a trace to anything outside the installation, and
`TRACING_CLIENT_PACKAGES` below names the third-party packages a tracing backend arrives as
— `langsmith`, `langfuse`, `opentelemetry` — so that `tests/test_trace_redaction.py` can walk
every module under `app/` and fail on any import of one from anywhere but here. That is the
same shape `tests/test_architecture_constraints.py` uses for constraint A: a structural
property asserted by walking source, not by trusting a reviewer.

**Why a sink and not a hard-coded LangSmith client.** §10.1 chose option (A) — LangSmith
Cloud with a PII filter — and left option (C) open 「未来若需完整 trace，切换至选项 (C)
Langfuse 自托管」. A module that imported `langsmith` directly would make that switch a
rewrite of the exporter; a module that emits a `TracePayload` makes it a second sink. It also
makes the filter testable without a network, which the verification standard requires: the
tests below hand `export` a sink that records what it was given, so what is asserted is the
bytes a backend would receive and not a mock's opinion of a dict.

**The order of the three steps is the interface.** Project (whitelist), then check the
serialised form (tripwire), then deliver. A version that delivered first and redacted later
would have the leak it exists to prevent; a version that checked only the dict's top level
would pass while a passage sat inside `counts`. Both of those are mutations the ticket names,
and both are caught by `tests/test_trace_redaction.py`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

from app.ai.observability.whitelist import (
    ALLOWED_TRACE_FIELDS,
    ForbiddenTraceField,
    forbid_oversized,
    project_record,
    violations_in,
)

#: The third-party roots a tracing backend arrives as. Named here so the test that walks the
#: repository has one list to read rather than a regex of its own, and so that adding option
#: (C) later is a change to this tuple and to the sink below — not a search for imports.
TRACING_CLIENT_PACKAGES: Final[tuple[str, ...]] = (
    "langsmith",
    "langfuse",
    "opentelemetry",
    "langchain.callbacks",
)

#: The key the node records live under in the graph's state (`records.RECORD_KEY`), spelled
#: here rather than imported: the observability layer reads a *shape* — a list of mappings
#: whose fields are the whitelist — and importing the graph would make this module depend on
#: the thing it observes.
RECORDS_KEY: Final[str] = "records"

#: The schema version of what leaves. A backend that decodes a payload later needs to know
#: which fields to expect, and a version in the payload is cheaper than inferring it from the
#: fields that happen to be present.
TRACE_SCHEMA_VERSION: Final[int] = 1


class TraceExportRefused(Exception):
    """The payload did not survive the filter, so nothing was delivered.

    Carries the reasons rather than a sentence, because the two callers want different
    things: an operator wants the field name in a log, and a test wants the set.
    """

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = tuple(reasons)
        super().__init__(
            "the trace was refused rather than exported: " + "; ".join(self.reasons)
        )


@dataclass(frozen=True, slots=True)
class TracePayload:
    """What may leave: a schema version, a run id and a list of whitelisted records.

    There is no `content` field and there never can be one without a line in
    `ALLOWED_TRACE_FIELDS`; see `whitelist.py` for why the direction of that list is the
    decision §10.1 makes.
    """

    run_id: str
    records: tuple[Mapping[str, Any], ...]
    schema_version: int = TRACE_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        """The payload as it is serialised and as a sink receives it."""
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "records": [dict(record) for record in self.records],
        }

    def serialised(self) -> str:
        """The exact character sequence a backend would store. **What the tests assert on.**"""
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True, default=str)


@runtime_checkable
class TraceSink(Protocol):
    """Where an exported payload goes. The seam a deployment fills with LangSmith Cloud.

    One verb, and it is synchronous on purpose: a trace is observability, and an export that
    could fail a request would make the assistant's availability depend on a SaaS. The
    exporter's caller decides what to do with a backend that is down, and the only sane
    choice is to log it — but that choice belongs to the caller and not to this protocol.
    """

    def send(self, payload: TracePayload) -> None: ...


@dataclass(slots=True)
class Delivered:
    """A sink that keeps what it was given. The module's own double, for tests and for a run
    without a backend: `export` still projects, still refuses, and still counts."""

    payloads: list[TracePayload] = field(default_factory=list)

    def send(self, payload: TracePayload) -> None:
        self.payloads.append(payload)


@dataclass(slots=True)
class TraceExporter:
    """Projects, checks, delivers. See the module docstring for the order and why.

    With `sink=None` this is a *filter* and not an exporter: the payload is built and
    validated and then dropped, which is the right behaviour for a deployment that has not
    configured a backend and for the test suite, which must not open a socket.
    """

    sink: TraceSink | None = None
    #: What has been delivered, so a caller can assert that an export happened without
    #: reaching into the sink. Cheap, and it makes "one export point" observable.
    delivered: int = 0

    def export(
        self, state: Mapping[str, Any], *, run_id: str, sink: TraceSink | None = None
    ) -> TracePayload | None:
        """The single export point. Returns the payload, or `None` when nothing was sent.

        **Every reason to refuse raises rather than dropping:** a forbidden field
        (`ForbiddenTraceField`), a payload past the ceilings or carrying a forbidden value
        (`TraceExportRefused`). A redaction that quietly dropped material would let the bug
        that produced it survive, and the ticket's failure mode is invisible leakage — so
        the failure mode here is a loud exception and no delivery at all.
        """
        target = sink if sink is not None else self.sink
        payload = build_payload(state, run_id=run_id)
        # The post-condition, on the serialised form. This is what a top-level key check
        # would miss: a passage inside `counts`, or a whole record smuggled under an
        # undocumented key, is *dropped by the projection* — and if a future edit made the
        # projection admit it, this is the line that refuses to send.
        found = violations_in(payload.as_dict())
        if found:
            raise TraceExportRefused([f"forbidden trace fields present: {sorted(found)}"])
        if target is not None:
            target.send(payload)
            self.delivered += 1
        return payload


#: The exporter a request path uses. Module-level rather than per-call because the sink is a
#: deployment's, and because one shared counter is how a test asserts that nothing else in
#: the process exported anything.
EXPORTER: TraceExporter = TraceExporter()


def build_payload(state: Mapping[str, Any], *, run_id: str) -> TracePayload:
    """The whitelisted trace for one run state.

    Reads three things and nothing else: the records, the run's `is_refusal` flag, and — when
    the caller supplies them under the whitelist's own names — the request's accounting. The
    question, the answer text, the citations, the tool results and the draft are all in the
    state around it and none of them is read, which is the point: this function would have to
    be edited to leak, and the edit would have to name a field that `ALLOWED_TRACE_FIELDS`
    already lists or fail the `assert` in `whitelist.py`.

    **The whole state is deliberately not tripwired, and the reason is that a state *is*
    allowed to hold conversation material.** `AgentState` carries the question, the tool's
    result, the draft and the answer — that is the installation's own Postgres, which is
    where a caller's data belongs (`state.py`'s own argument). What must be clean is the
    record, because the record is the thing shaped like a trace. So the tripwire runs on each
    record (`project_record`) and on nothing else: a state with ten passages in it exports a
    clean payload, and a record that somehow acquired a passage raises.
    """
    raw = state.get(RECORDS_KEY)
    records: list[dict[str, Any]] = []
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for item in raw:
            if isinstance(item, Mapping):
                records.append(project_record(item))
    refusal = state.get("is_refusal")
    payload = TracePayload(
        run_id=run_id,
        records=tuple(_with_run_scope(record, refusal) for record in records),
    )
    forbid_oversized(payload.as_dict())
    return payload


def _with_run_scope(record: Mapping[str, Any], refusal: object) -> dict[str, Any]:
    """One projected record, plus the run-level facts a node record does not carry.

    `is_refusal` is the run's, not the node's: `records.recorded` reads
    `result.get("is_refusal")`, which is `True` on the node that decided the refusal and
    absent everywhere else, so a payload built from records alone would say "this run was not
    a refusal" whenever the refusal was decided by a node whose update omitted the flag. It
    is also the one §10.1 field that is a *run* property, which is why it is added here
    rather than reconstructed per record.
    """
    scoped = dict(record)
    if isinstance(refusal, bool):
        scoped.setdefault("is_refusal", refusal)
    return scoped


__all__ = [
    "EXPORTER",
    "RECORDS_KEY",
    "TRACE_SCHEMA_VERSION",
    "TRACING_CLIENT_PACKAGES",
    "Delivered",
    "TraceExportRefused",
    "TraceExporter",
    "TracePayload",
    "TraceSink",
    "build_payload",
    "ForbiddenTraceField",
    "ALLOWED_TRACE_FIELDS",
]
