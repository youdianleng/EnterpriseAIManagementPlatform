"""Turning chunk text into the vectors `document_chunks.embedding` holds.

`docs/DESIGN.md` §10.3 fixes the dimension at 1536 and `core.constants` names the
model; neither is a parameter of anything below, and that is deliberate. What *is*
behind a seam here is the **transport**, and the distinction is the design's own:
`docs/architecture/codebase-design.md` §4 refuses a seam around the embedding model —
two models are not interchangeable, so an interface that looked like a choice would
manufacture the illusion of one — while noting that tests and the development stack
need an implementation that does not call OpenAI. So:

* **One protocol, two adapters, and the interface says only what every transport can
  honour**: give me N texts, give me N vectors of `EMBEDDING_DIMENSIONS`, and tell me
  which model you are, because that name is written on every row.
* **`OpenAIEmbedder` is the real implementation and it is present, not a stub.** It is
  the code that runs the moment an operator sets `OPENAI_API_KEY`; the only thing
  standing between this repository and a live call is a key.
* **`DeterministicEmbedder` is the development and test adapter**, and it is honest
  about being one: hashed bag-of-words vectors that make *lexical* similarity
  geometric, so retrieval, ranking, the HNSW index and an evaluation script all
  exercise real code paths and real SQL over vectors that are reproducible — while
  saying, in its own name and in `EMBEDDING_PROVIDER`, that this is not semantic
  retrieval. A fake that pretended to be semantic would make the evaluation script's
  numbers meaningless rather than modest.

The failure path is a first-class outcome rather than an exception to be caught
somewhere: `EmbeddingUnavailable` carries the catalogue code a client could be shown
and a sentence for the operator's log, and the pipeline turns it into a document that
is `ready` with chunks that are *not* embedded. That is a state retrieval can see and
a re-run can fix — the observable outcome the ticket asks for — and it is deliberately
not a mutation of the document's own status: `ready` is a claim about text, and the
text is there.
"""

import asyncio
import json
import math
import unicodedata
import urllib.error
import urllib.request
import zlib
from typing import Protocol

from app.core.constants import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL
from app.core.errors import ErrorCode

#: How many texts one HTTP request carries. The API accepts many; a batch keeps the
#: request under the body limit and gives a retry something small to retry, and 64
#: chunks of ~400 tokens is about the size of a large document's section.
EMBED_BATCH = 64

#: The OpenAI embeddings endpoint. A default rather than a setting because a *different*
#: endpoint is a different provider, which is a change to this module and not to a
#: deployment's environment.
DEFAULT_BASE_URL = "https://api.openai.com/v1"

#: Seconds before a call is abandoned. The pipeline runs in a job rather than in a
#: request, but a hung socket is still a document that never finishes parsing.
REQUEST_TIMEOUT_SECONDS = 30.0

#: What the fake's vectors are built from. Named so the row's `embedding_model` can
#: never be mistaken for a model that exists.
FAKE_MODEL = "deterministic-bag-of-words-v1"


class EmbeddingUnavailable(Exception):
    """The vectors could not be produced, for a reason an operator can act on.

    Carries the catalogue code rather than being one, because it is raised inside the
    transport and mapped to a response or to an audit record by the pipeline. An
    exception rather than a value here, and a value rather than an exception one level
    up: `ParsedDocument` is where "this document has no vectors" becomes a fact the
    caller can read without catching anything.
    """

    def __init__(self, detail: str, *, code: ErrorCode = ErrorCode.DOCUMENT_EMBEDDING_UNAVAILABLE):
        super().__init__(detail)
        self.detail = detail
        self.code = code


class Embedder(Protocol):
    """The seam. One verb, and the model's name.

    `name` is not decoration: vectors from two models are not comparable, so a row has
    to record which one produced it or a re-embed after a model change is a guess
    about which rows are stale. It is the same argument as `chunking_version`, applied
    to the other half of retrieval.
    """

    @property
    def name(self) -> str: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class DeterministicEmbedder:
    """Hashed bag-of-words vectors: reproducible, offline, and not semantic.

    **What it is.** Each token of the text — lower-cased, accent-folded, letters and
    digits only — sets one dimension of a `EMBEDDING_DIMENSIONS`-wide vector to ±1,
    chosen by a CRC32 of the token, and the vector is then L2-normalised. The cosine
    similarity of two such vectors is the cosine of their shared-token multiset, so
    documents that use the same words are near each other and documents that do not
    are orthogonal.

    **What that buys.** The whole retrieval path — the `vector(1536)` column, the HNSW
    index, `ORDER BY embedding <=> probe`, the access policy over the join to
    `documents` — runs against a real PostgreSQL and a real index with vectors that
    are byte-identical across runs and processes. A random-vector fake would exercise
    the same SQL while making every relevance number in the evaluation script noise;
    this one makes them *lexical*, which is a claim a reader can check.

    **What it does not buy.** It has no notion of meaning: "vacaciones" and "días
    libres" are unrelated to it, and it will never beat a lexical baseline. That is
    why `EMBEDDING_PROVIDER` has to be set to `fake` explicitly outside development,
    and why the evaluation script prints which provider produced its numbers.

    Determinism is not a nicety either: `reprocess` re-embeds a document, and a test
    that asserts the second run wrote the *same* vectors is a test that a fake
    returning `random.random()` would fail.
    """

    name = FAKE_MODEL

    def __init__(self, dimensions: int = EMBEDDING_DIMENSIONS) -> None:
        self._dimensions = dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions
        for token in _tokens(text):
            bucket = zlib.crc32(token.encode("utf-8")) % self._dimensions
            # The sign is the second half of the hash, so two tokens colliding in the
            # bucket cancel rather than reinforce: a collision costs a little signal
            # instead of inventing similarity between unrelated words.
            vector[bucket] += 1.0 if (zlib.crc32(token.encode("utf-8")) >> 16) & 1 else -1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            # The empty text, and a text whose tokens cancelled exactly. A zero vector
            # has no cosine with anything — pgvector returns NaN, which sorts last and
            # silently disappears from a result set — so it is placed on one axis.
            vector[0] = 1.0
            return vector
        return [value / norm for value in vector]


def _tokens(text: str) -> list[str]:
    """The fake's tokens: accent-folded words and numbers, lower-cased."""
    found: list[str] = []
    current: list[str] = []
    for char in fold_accents(text).lower():
        if char.isalnum():
            current.append(char)
        elif current:
            found.append("".join(current))
            current = []
    if current:
        found.append("".join(current))
    return found


def fold_accents(text: str) -> str:
    """`NFKD`, with the combining marks dropped: `vacación` and `vacacion` are one token.

    Spanish text arrives with and without accents from the same organisation — a PDF
    extraction, a hand-typed heading — and to a hashed bag of words those are two
    unrelated tokens unless they are folded. The real model handles this itself; the
    fake has to be told.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


class OpenAIEmbedder:
    """`POST /v1/embeddings`, with the dimension and the model from the constants.

    **The real implementation, behind the seam rather than absent from the repo.** It
    uses `urllib` from the standard library inside `asyncio.to_thread`: the API needs
    one POST, adding an HTTP client dependency to a container for one request buys
    nothing, and the thread is what keeps a job that runs inside the API process from
    blocking its event loop for the length of a network call.

    `dimensions` is sent explicitly even though 1536 is this model's native width:
    §10.3's decision is that the *column* fixes the dimension, and a request that left
    it implicit would be relying on the provider's default matching our schema.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = EMBEDDING_MODEL,
        dimensions: int = EMBEDDING_DIMENSIONS,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if not api_key:
            raise EmbeddingUnavailable(
                "no OPENAI_API_KEY: the real embedder cannot be built without one"
            )
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    @property
    def name(self) -> str:
        """The model, which is what goes on the row — not the class or the provider."""
        return self._model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return await asyncio.to_thread(self._embed_blocking, texts)

    def _embed_blocking(self, texts: list[str]) -> list[list[float]]:
        body = json.dumps(
            {
                "model": self._model,
                "input": texts,
                "dimensions": self._dimensions,
                # Float, not base64: the column stores what the API returns, and a
                # decode step on every write is a step that can be wrong.
                "encoding_format": "float",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/embeddings",
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise EmbeddingUnavailable(self._http_detail(error)) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise EmbeddingUnavailable(
                f"the embeddings endpoint could not be reached: {error}"
            ) from error
        except json.JSONDecodeError as error:
            raise EmbeddingUnavailable(
                f"the embeddings endpoint answered with something that is not JSON: {error}"
            ) from error

        return self._vectors_from(payload, texts)

    def _http_detail(self, error: urllib.error.HTTPError) -> str:
        """The provider's own message, shortened, plus what it means for the operator.

        A 401/403 is a key that is absent, revoked or unfunded, and the sentence says
        so: an operator reading the log should not have to know OpenAI's status codes
        to learn that they have to set one.
        """
        try:
            payload = json.loads(error.read().decode("utf-8"))
            message = payload.get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001 - a body that is not JSON tells us nothing
            message = ""
        if error.code in (401, 403):
            hint = "the API key is missing, revoked or not allowed for this model"
        elif error.code == 404:
            hint = f"the model {self._model!r} is not available to this key"
        elif error.code == 429:
            hint = "the account is rate limited or out of quota"
        else:
            hint = "the provider refused the request"
        return f"embeddings HTTP {error.code}: {hint}" + (f" ({message})" if message else "")

    def _vectors_from(self, payload: object, texts: list[str]) -> list[list[float]]:
        """The response as vectors, refusing anything that is not one vector per input.

        The check is exhaustive on purpose: a short response, a long one, a wrong
        dimension or a wrong order would all be *stored* rather than noticed, and a
        corpus whose vectors are silently misaligned is a corpus whose retrieval is
        wrong in a way no test would see.
        """
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise EmbeddingUnavailable(f"unexpected embeddings response: {str(payload)[:200]}")
        rows = payload["data"]
        if len(rows) != len(texts):
            raise EmbeddingUnavailable(
                f"asked for {len(texts)} embeddings and got {len(rows)}"
            )
        ordered = sorted(rows, key=lambda row: row.get("index", 0))
        vectors: list[list[float]] = []
        for row in ordered:
            vector = row.get("embedding")
            if not isinstance(vector, list) or len(vector) != self._dimensions:
                width = len(vector) if isinstance(vector, list) else "no"
                raise EmbeddingUnavailable(
                    f"an embedding came back with {width} dimensions, not {self._dimensions}; "
                    "the column's dimension is part of the schema (§10.3)"
                )
            vectors.append([float(value) for value in vector])
        return vectors


def build_embedder(
    provider: str,
    *,
    api_key: str | None = None,
    model: str = EMBEDDING_MODEL,
    dimensions: int = EMBEDDING_DIMENSIONS,
    base_url: str = DEFAULT_BASE_URL,
) -> Embedder | None:
    """The adapter a deployment asked for, or `None` when it asked for none.

    Three answers rather than two, and the third is the point: `none` is a supported
    configuration — a corpus that is chunked and searchable by full text while the
    embedding key is being arranged — and it must be *reachable without an exception*,
    or the only way to run without a key would be to run with a broken pipeline.

    `fake` is likewise explicit. A deployment that forgets to configure a provider
    does not get a silent fake: the derivation in `config.Settings` gives development
    and test the fake and everything else `openai`, so a production without a key is a
    document that fails to embed and says why.
    """
    if provider == "none":
        return None
    if provider == "fake":
        return DeterministicEmbedder(dimensions)
    if provider == "openai":
        return OpenAIEmbedder(
            api_key or "", model=model, dimensions=dimensions, base_url=base_url
        )
    raise ValueError(
        f"unknown embedding provider {provider!r}; expected one of none, fake, openai"
    )


__all__ = [
    "DEFAULT_BASE_URL",
    "EMBED_BATCH",
    "FAKE_MODEL",
    "REQUEST_TIMEOUT_SECONDS",
    "DeterministicEmbedder",
    "Embedder",
    "EmbeddingUnavailable",
    "OpenAIEmbedder",
    "build_embedder",
    "fold_accents",
]
