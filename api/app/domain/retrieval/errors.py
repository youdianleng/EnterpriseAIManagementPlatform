"""The retrieval module's refusal, and the one thing it refuses.

Almost nothing here is an error. A search that finds nothing above the threshold is
the *ordinary* "no basis" answer and travels as a value (`SearchOutcome.
insufficient_evidence`), because §5.2's rule is a successful search whose best score
is too low. A search whose embedding provider cannot be reached is likewise a value:
the vector leg is skipped and `legs_used` says so, which is exactly what
`EMBEDDING_PROVIDER=none` does on purpose.

What is left is a caller that wrote the query wrong — the one case where there is
nothing to answer with and the client has to change something. It gets its own
catalogue code rather than a bare `INVALID_REQUEST` so a client can route on it and a
log line says *which* argument was wrong.
"""

from app.core.errors import ErrorCode
from app.domain.errors import DomainError


class RetrievalErrorCode:
    """Retrieval's own codes, named here and catalogued in `core.errors`.

    A class rather than an enum for the reason `document.errors.DocumentErrorCode`
    gives: the catalogue lives in one place, and this module holds only the names it
    uses. Attribute access beats a bare string at the raise site — a typo is an
    `AttributeError` rather than an uncatalogued code at runtime.
    """

    RETRIEVAL_QUERY_INVALID = ErrorCode.RETRIEVAL_QUERY_INVALID


def require_query(query: str) -> str:
    """The query, trimmed, or a refusal naming what was wrong with it.

    An empty query is refused rather than answered with the whole corpus ranked by
    nothing: `websearch_to_tsquery('')` matches no rows and a zero vector has no
    cosine with anything, so the honest shape of "no query" is a refusal and not a
    confident empty answer that reads like "nothing in the knowledge base matches".
    A ceiling on the length is the second half — the query is embedded and sent to
    the full-text parser, and both are paid for by the caller of a chat endpoint.
    """
    cleaned = query.strip()
    if not cleaned:
        raise DomainError(
            RetrievalErrorCode.RETRIEVAL_QUERY_INVALID,
            detail="a search needs a query; an empty one is not 'nothing matched'",
        )
    if len(cleaned) > MAX_QUERY_CHARS:
        raise DomainError(
            RetrievalErrorCode.RETRIEVAL_QUERY_INVALID,
            detail=(
                f"{len(cleaned)} characters is longer than the {MAX_QUERY_CHARS} a "
                "search accepts; a question, not a document"
            ),
        )
    return cleaned


def require_limit(limit: int) -> int:
    """A result count within what one context window can hold.

    Refused rather than clamped, for the reason the upload ceiling is: a caller that
    asked for two hundred and silently got fifty has a client bug that only shows up
    as a wrong answer much later.
    """
    from app.domain.retrieval.models import MAX_LIMIT

    if limit < 1 or limit > MAX_LIMIT:
        raise DomainError(
            RetrievalErrorCode.RETRIEVAL_QUERY_INVALID,
            detail=f"limit must be between 1 and {MAX_LIMIT}; got {limit}",
        )
    return limit


#: How long a query may be. A question is a sentence or three; 2000 characters is
#: generous for a pasted paragraph and still bounds what the embedder is asked to
#: send and what the tsquery parser is asked to parse.
MAX_QUERY_CHARS = 2000


__all__ = ["MAX_QUERY_CHARS", "RetrievalErrorCode", "require_limit", "require_query"]
