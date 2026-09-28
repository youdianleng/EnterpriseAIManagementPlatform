"""The state the graph carries between its nodes, and the context a node is given.

**Two objects, and the difference between them is the whole design of a resumable agent.**

`AgentState` is what the checkpointer writes to `langgraph` on every step, so it has to be
serialisable and it has to be worth keeping across a process restart: the question, which
branch was decided, the refusal, the summary of an answer, what is waiting for confirmation,
and the node records. It is the graph's memory.

`AgentContext` is what the *caller* brings to one run and what must **never** be written to
the checkpointer: the resolved `Principal`, the assembled `AnswerService` (which holds a
database session), and the conversation this run belongs to. A `Principal` is a snapshot of
permissions, and a `AnswerService` holds an open transaction — checkpointing either would
mean the state restored after a restart carries a session that died with the process, and
permissions that were resolved yesterday. So the graph reads them from `Runtime.context` on
every node execution, and the run resuming tomorrow is executed with tomorrow's principal.
That is also what makes the restart test meaningful: the second graph instance is handed a
*fresh* context object, and everything it knows about the interrupted run has to come from
Postgres.

**The answer's text is not in the state, and that is a requirement rather than an
optimisation.** §5.2 puts the first token on screen within 2.5 seconds, which a node cannot
do if it collects the whole answer before returning; so `answer_policy` forwards each event
to `Runtime.stream_writer` as it arrives (the caller relays them to the client, unchanged)
and leaves only a summary behind — ids, the model, counts, whether the answer was a refusal.
A buffered answer would also mean the checkpoint held a second copy of text that
`rag_messages` already owns, retention and all.
"""

from dataclasses import dataclass
from typing import Any, TypedDict
from uuid import UUID

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
    #: How many tools of the branch's kind the registry held. Zero today (tickets 39-40),
    #: and written to the state rather than only to the record so that a reader of a run —
    #: or of its checkpoint — can see *why* the branch stopped: the registry was empty.
    tools_registered: int
    #: The answer's summary — ids, model, counts, outcome. **No text, no citations.**
    answer: dict[str, Any] | None
    #: The bilingual refusal, verbatim, for a prohibited ask.
    refusal: dict[str, str] | None
    #: What the draft branch would have produced. A placeholder until ticket 40.
    pending_action: dict[str, Any] | None
    #: What came back from the human. `{"received": True, "value_type": "dict"}` — the type
    #: of the answer, never the answer.
    confirmation: dict[str, Any] | None
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
    #: (ticket 35's `answer_filter_for`), and any future tool reads it too.
    principal: Principal
    #: The assembled answer pipeline. The graph *calls* it; it does not build a second
    #: retrieval, prompt or citation list of its own.
    answers: AnswerService
    #: The conversation to continue, or `None` for a new one.
    conversation_id: UUID | None = None


__all__ = ["AgentContext", "AgentState"]
