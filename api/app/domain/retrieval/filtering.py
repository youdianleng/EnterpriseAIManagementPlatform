"""Which documents a question may retrieve from — the one place that answers it.

`docs/DESIGN.md` §4.3 is a structural constraint rather than a feature: the permission
condition **must** be part of the retrieval SQL, as a pre-filter, so a chunk of a
document the caller may not open is never ranked, never fetched and never counted. The
constraint exists because the alternative is a leak: a passage that reached the ranking
has already reached the answer, and a citation built from it names a file the caller was
never allowed to read.

Three things live here, and the reason they are one module is that ticket 35 has to pin
all three at once — "the filter reaches the search" is only true if the *spec*, the
*rendering* and the *disjunction* agree:

* **`answer_filter_for(principal)`** — the principal → `FilterSpec` translation for
  retrieval. It is a thin, named wrapper over `app.domain.access.kernel.filter_for`
  rather than a second implementation, because §4.3's second checklist line is exactly
  「过滤逻辑复用权限内核中的同一处实现，不复制一份 RAG 专用版本」: a RAG-specific copy of §4.2 is
  the copy that drifts from the kernel the moment somebody changes a clause. What the
  wrapper adds is a name that says *which call site this is* and a docstring that says
  what it is for, so a route cannot quietly pass `None` — ticket 33's
  `/retrieval/search` does pass `None` today, on purpose, and this module is how the
  answer path stops doing that.

* **`retrieval_filter_explanation(spec)`** — the predicate as the sentence a reviewer
  reads, for the debug view and for the message row. It renders through the
  repository's own translation rather than describing the spec a second time, so what a
  reviewer sees is *the statement the database ran*.

* **`unfiltered()`** — an explicitly named `None`, so the difference between "this call
  site forgot" and "this call site means it" is visible in the source rather than
  invisible in a default. Only the offline evaluation and a test may want it.
"""

from app.domain.access.kernel import (
    Action,
    FilterSpec,
    Principal,
    ResourceKind,
    can,
    filter_for,
)
from app.domain.document.errors import DocumentErrorCode
from app.domain.errors import DomainError

#: The action a question needs. `document.read` and not a fourth action: §4.2 decides
#: a document once, whatever the verb, and a grounded answer is a read of the documents
#: it cites. Its own constant so the route guard and this module cannot name two
#: different actions for one question.
ASK_ACTION = Action.DOCUMENT_READ


def answer_filter_for(principal: Principal) -> FilterSpec:
    """What this caller's answers may be grounded in. **The shared helper.**

    Ticket 34's answer path calls this, and ticket 35 pushes the same spec into
    `/retrieval/search` and pins it with its escalation suite. It is deliberately one
    function with one call shape rather than an extra parameter on the service,
    because the failure it prevents is a route that *could* have passed a filter and
    did not: the answer path's call reads `filter_spec=answer_filter_for(principal)`,
    and there is no spelling of it that leaves the filter out.

    `ResourceKind.DOCUMENT` is the kind, and the kernel's document branch is the whole
    answer — `allow_all` is never true for documents (it would include everybody's
    personal uploads), and the clauses of §4.2 arrive as data for the retrieval
    repository to render.

    **One field of the spec is cleared here, and it is this ticket's whole point.**
    The kernel's document spec describes §4.2 in full, which includes a personal
    document its owner published to a department — a colleague may open that document,
    and the document list shows it to them. A *question* is narrower, because the
    ticket states the rule for the pool: 「个人文档不进入公司知识库的检索池」, 「只有提问者
    本人的个人文档可被召回」. Clearing `personal_documents_via_department` is what makes
    the retrieval predicate recall a personal document for its owner and for nobody
    else, and it is done here — at the one place a question's reach is produced —
    rather than by a second clause in the repository, so the list and the search
    cannot drift into two different readings of §4.2.

    It is a *narrowing* of the kernel's own answer and never a widening: the field can
    only be cleared, never set, and `filter_for` is the only thing that produces the
    spec this replaces.

    The role check happens *here* rather than only at the route, so a caller reaching
    the retrieval path from anywhere — a job, a test, a future agent tool — is refused
    the same way. It is the action the route guard already applies, asked again at the
    point where the reach is produced; a second ask is not a second rule.
    """
    decision = can(principal, ASK_ACTION)
    if decision.denied:
        raise DomainError(
            DocumentErrorCode.FORBIDDEN,
            detail=(
                f"{ASK_ACTION} refused for a grounded answer: {decision.primary_reason} "
                f"({decision.detail})"
            ),
        )
    return filter_for(principal, ResourceKind.DOCUMENT).only_my_personal_documents()


def retrieval_filter_explanation(spec: FilterSpec) -> str:
    """The spec as one sentence: the predicate the database ran, and its bindings.

    Shared with ticket 35's debug view, which §4.3 asks for by name (「检索调试视图中显示本次
    生效的权限条件，便于人工复核」), and with the `retrieval_filter` column of
    `rag_messages`, so a message can be reviewed long after the request that produced it
    has gone. `domain/retrieval/service.py` renders the same thing for the trace; this is
    the entry point for callers that have a spec and no trace.
    """
    from app.repositories.retrieval import visible_document_predicate

    predicate, parameters = visible_document_predicate(spec)
    binds = ", ".join(f"{name}={value!r}" for name, value in sorted(parameters.items()))
    return f"{predicate}  --  {binds}" if binds else predicate


def unfiltered() -> None:
    """The deliberate absence of a filter, named so it cannot be a default.

    A retrieval with no `FilterSpec` searches the whole corpus. That is right for
    `tests/tools/eval_retrieval.py`, which measures the pipeline rather than a person,
    and wrong for every request. Returning `None` *and saying so* is the difference
    between a call site that decided and one that forgot, and it is the reason this
    function exists rather than a bare `None` at the two places that mean it.
    """
    return None


__all__ = [
    "ASK_ACTION",
    "answer_filter_for",
    "retrieval_filter_explanation",
    "unfiltered",
]
