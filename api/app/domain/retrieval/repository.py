"""Persistence contract for hybrid retrieval.

One method, and its signature is the whole argument for the module boundary: the two
legs are *one call*, so "the vector half ran and the text half did not" is not a state
a caller can produce by forgetting a line. `docs/DESIGN.md` §5.2 says the two run in
parallel (「pgvector 向量 top 20 ∥ Postgres 全文检索 top 20」); see the PostgreSQL
implementation for what that is in one database and why it is one round trip rather
than two coroutines.

**The filter is a parameter, and the repository translates it — it does not invent
one.** That is `access.kernel.filter_for`'s design: the kernel states what a principal
may reach as data, and each store renders it in its own query language. A repository
that took a `Principal` and derived departments and clearance itself would be the
second implementation of §4.2, which is the defect `docs/architecture/
codebase-design.md` §3 refuses a separate "document access" module for. `None` means
*no filter was given* — see `RetrievalService.search` for why that is visible in the
result rather than hidden here.
"""

from typing import Protocol

from app.domain.access.kernel import FilterSpec
from app.domain.retrieval.models import RankedCandidate


class ChunkSearchRepository(Protocol):
    async def search_legs(
        self,
        query: str,
        *,
        embedding: list[float] | None,
        leg_limit: int,
        filter_spec: FilterSpec | None = None,
    ) -> tuple[list[RankedCandidate], list[RankedCandidate]]:
        """Both legs' ranked candidates for one query: `(vector, text)`.

        Each list is ordered best-first by its own measure and each candidate carries
        its 1-based `rank` within that list, so the fusion does not have to re-derive
        what the leg already decided.

        **`embedding=None` means the vector leg does not run**, and the first list comes
        back empty. That is the `EMBEDDING_PROVIDER=none` deployment, whose corpus is
        full-text searchable while the key is being arranged; the honest answer is a
        text-only result, not an exception and not a hybrid claiming half its recall.

        **Both legs read committed state.** A document that is `failed` or `archived`
        is not retrievable even though its chunks may still exist: `status = 'ready'`
        is the pipeline's own claim that its text is complete, and citing a document
        that nobody can open is worse than not citing it.
        """
        ...


__all__ = ["ChunkSearchRepository"]
