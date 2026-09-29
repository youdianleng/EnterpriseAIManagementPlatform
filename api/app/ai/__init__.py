"""The agent edge: LangGraph's graph, the nodes in it, and the tools it may call.

**Where this package is, and why it is not where DESIGN §1 draws it.**
`docs/architecture/codebase-design.md` §1 shows `ai/` as a top-level package beside `api/`,
`domain/` and `adapters/`. This repository deviated from that layout before this ticket:
every Python package lives under `api/app/` (`app/domain`, `app/repositories`, `app/jobs`,
`app/api`), which the README and every ticket record. Ticket 38 keeps the convention rather
than half-migrating, so the mapping between the design's tree and this one is:

    DESIGN §1            this tree
    ai/providers/   →    api/app/domain/answer/chat.py          (ticket 34)
    ai/rag/         →    api/app/domain/retrieval/ + document/  (tickets 31-35)
    ai/agents/      →    api/app/ai/agents/                     (ticket 38)
    ai/tools/       →    api/app/ai/tools/                      (ticket 39 registers §6.2's
                                                                read-only half; 40 the draft half)
    ai/observability/ →  api/app/ai/observability/              (ticket 42)

`providers/` and `rag/` are the two entries that are *already built* elsewhere, which is why
`app/ai` begins with `agents/` and `tools/` and re-implements neither: the graph calls the
answer path (`app.domain.answer.driver.AnswerService`) rather than owning a second
retrieval, prompt or citation list.

**The dependency direction is one way, and there is a test for it.**
`app/ai/**` may import `app/domain/**`; `app/domain/**` may never import `app/ai/**`. That is
constraint A of `codebase-design.md` §6 — 「`domain/` 绝不 import `ai/`」 — and
`tests/test_architecture_constraints.py` walks the import graph of every module under
`app/domain/` and fails if one reaches `app.ai`. Creating this package is what made the
constraint testable, which is why the test arrives with it.
"""

__all__: list[str] = []
