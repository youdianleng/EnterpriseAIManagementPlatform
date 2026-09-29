"""The state the graph carries between its nodes, and the context a node is given.

**Two objects, and the difference between them is the whole design of a resumable agent.**

`AgentState` is what the checkpointer writes to `langgraph` on every step, so it has to be
serialisable and it has to be worth keeping across a process restart: the question, which
branch was decided, the refusal, the summary of an answer, what is waiting for confirmation,
and the node records. It is the graph's memory.

`AgentContext` is what the *caller* brings to one run and what must **never** be written to
the checkpointer: the resolved `Principal`, the assembled `AnswerService` (which holds a
database session), the conversation this run belongs to, the session the read-only tools
read through, and the business date their periods default to. A `Principal` is a snapshot
of permissions, and a `AnswerService` or a session holds an open transaction —
checkpointing either would mean the state restored after a restart carries a session that
died with the process, and permissions that were resolved yesterday. So the graph reads
them from `Runtime.context` on every node execution, and the run resuming tomorrow is
executed with tomorrow's principal. That is also what makes the restart test meaningful:
the second graph instance is handed a *fresh* context object, and everything it knows
about the interrupted run has to come from Postgres.

**The tools are given a session, and it is the same shape of decision.** A read-only tool
has to read something (`app/ai/tools/services.py`), and a tool given no session could only
invent its answer. What keeps that session from becoming a write is not its absence but
the registry: every tool is one of a known set of implementations, and
`tests/test_agent_readonly_tools.py` walks their source and fails on a write. The session
is a field of the *context*, which is never checkpointed — so a run restored tomorrow
opens tomorrow's transaction, exactly as it resolves tomorrow's principal.

**A read-only answer's text *is* in the state, and the difference from the paragraph
above is the point.** A policy answer streams and is stored in `rag_messages`; a tool
answer is one short sentence about the caller's own figures, and ticket 41 has to be able
to persist it. Keeping it costs a sentence in the checkpoint, and keeping it *there* is
what lets the figures in it be the tool's own (`tool_result`) rather than a model's.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any, TypedDict
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access.principal import Principal
from app.domain.answer.driver import AnswerService


class AgentState(TypedDict, total=False):
    """The graph's memory. Every key here is checkpointed, so every key is kept honest.

    `question` is the one key that carries what a person typed, and it has to: an
    interrupted run that has forgotten what it was asked cannot be resumed. It is an input,
    not a record — `records.py` is what guarantees the *records* carry no text.
    """

    #: The incoming message. Set by the caller, never overwritten by a node.
    question: str
    #: The conversation this run belongs to, as a string. `None` until the answer path
    #: creates one, which is why `answer_policy` writes it back: a resumed run needs it.
    conversation_id: str | None
    #: The classifier's decision and the name of the rule that made it. See `intents.py`:
    #: both are constants, never text from the request.
    intent: str
    rule: str
    #: What a branch that has no implementation yet says instead of calling a tool.
    notice: str | None
    #: How many tools of the branch's kind the registry held. Ticket 39 registered the
    #: read-only half (ticket 40 the draft half), and the count is written to the state
    #: rather than only to the record so that a reader of a run — or of its checkpoint —
    #: can see *why* a branch stopped where it did.
    tools_registered: int
    #: The tool the read-only branch called, by registry name. Decided by the lexical
    #: selector today (`app.ai.tools.selection`) and by a model's function call in ticket
    #: 42; either way a key of the registry, never a string a person typed. A caller may
    #: set it (with `tool_arguments`) to name a tool directly, which is what a test does.
    tool: str | None
    #: The arguments the tool was called with: a period, a year, a status or a name.
    #: **Never an employee** — `app.ai.tools.models.ALLOWED_PARAMETERS` is the closed
    #: vocabulary a tool may declare, and a parameter outside it cannot be constructed.
    tool_arguments: dict[str, Any]
    #: The tool's structured result: the values the answer states, and the reason the
    #: figures in that answer cannot be the model's. **In the checkpoint and never in a
    #: record**: §10.1 keeps `tool_output` out of traces, and the checkpoint is the
    #: installation's own Postgres, which is where a caller's own data belongs.
    tool_result: dict[str, Any] | None
    #: The answer the result was rendered into — a message key, both sentences, and the
    #: block they form (`app.ai.tools.render.ToolAnswer`). Its figures are `tool_result`'s.
    tool_answer: dict[str, str] | None
    #: How the call ended: `ok`, `refused`, `invalid`, `failed` or `unknown`. Recorded, so
    #: a reader of a run can see why an answer states no figures at all.
    tool_outcome: str | None
    #: What came back from the human. `{"received": True, "value_type": "dict"}` — the type
    #: of the answer, never the answer.
    confirmation: dict[str, Any] | None
    #: **The draft the assistant proposed, in full** (ticket 40): every field the eventual
    #: submission will write, each with the value the validation accepted, its label in both
    #: languages, and the input a client should draw (`domain/agent/models.PrefillForm`).
    #: It is the same object `tool_result` carries and it is kept under its own name because
    #: it is what the pause shows and what a resumed run acts on — ticket 41 confirms *this*,
    #: and the interruption's payload is built from it.
    prefill_form: dict[str, Any] | None
    #: The `agent_actions` row this run wrote, or `None` when nothing was drafted. It is the
    #: draft's durable identity: the row survives the process, and the interface reads the
    #: same form back through it (`GET /answers/conversations/{id}` carries the conversation's
    #: newest draft). Recorded rather than derived because a resumed run cannot re-derive an
    #: id it never saw.
    agent_action_id: str | None
    #: The answer's summary — ids, model, counts, outcome. **No text, no citations.**
    answer: dict[str, Any] | None
    #: The bilingual refusal, verbatim, for a prohibited ask.
    refusal: dict[str, str] | None
    #: What the draft branch decided: `proposed` (a form exists and a human is being asked),
    #: `invalid` (the contents would be refused), `refused` (the kernel said no),
    #: `no_request` (nothing was named) or `no_draft`. Only `proposed` pauses.
    pending_action: dict[str, Any] | None
    #: D23's refusal, or the answer path's own D20 refusal. Read by callers and by ticket
    #: 42's trace filter.
    is_refusal: bool
    #: The node records, appended by `records.recorded`.
    records: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class AgentContext:
    """What one run is given, and what is never checkpointed. See the module docstring.

    Frozen because a node must not be able to swap the caller's principals mid-run, and
    because the same object is handed to every node of one execution: the principal a node
    acts on is the principal the caller resolved, for the whole run.
    """

    #: The resolved caller. The answer path re-filters retrieval by this on every call
    #: (ticket 35's `answer_filter_for`), and the read-only tools read *as* it: it is the
    #: only thing in this object that says whose data a tool may touch.
    principal: Principal
    #: The assembled answer pipeline. The graph *calls* it; it does not build a second
    #: retrieval, prompt or citation list of its own.
    answers: AnswerService
    #: The conversation to continue, or `None` for a new one.
    conversation_id: UUID | None = None
    #: The session the read-only tools read through (`app/ai/tools/services.py` builds the
    #: domain services on it). Optional because the refusal, small-talk and paused branches
    #: need no database at all, and a context that could not be built without one would
    #: make those branches need a connection they never open.
    session: AsyncSession | None = None
    #: The Madrid business date a tool's period defaults to. Passed rather than read from
    #: the clock so that a test can pin "this month" to a day it chose.
    today: date | None = None


__all__ = ["AgentContext", "AgentState"]
