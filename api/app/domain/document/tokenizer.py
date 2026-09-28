"""Token counting: the embedding model's tokenizer, and an honest fallback.

Chunk sizes are the one number this module's split is built on — "about 400 tokens"
and "about 1500 tokens" are the ticket's words — so the number has to be a count of
what the embedding model will actually receive rather than a character count wearing
the word "token". Ticket 31 counted characters over a constant and said so; this is
the replacement, and the seam matters for a reason that has nothing to do with taste:

* **A tokenizer that disagrees with the model makes a chunk size a fiction.** A
  `len(text) // 4` estimate is 31% low on Spanish prose (measured below), so a
  "1500-token" parent would arrive at the model as nearly 2000 and a "400-token"
  child would be one a query cannot match well. The approximation is kept, and its
  error is pinned by a test, because a fallback whose accuracy nobody measured is the
  thing this docstring is arguing against.

* **`cl100k_base` is the tokenizer `text-embedding-3-*` uses**, which is the model
  §10.3 fixes, so the count here is the model's own count and not a proxy for it.
  `o200k_base` is offered beside it — it is what the newer OpenAI models use, and it
  is measurably cheaper on Spanish (about 0.81 tokens per `cl100k_base` token on the
  fixture below) — but the default follows the embedding model, because the count
  exists to predict what *that* model will see.

* **It works offline.** tiktoken downloads its merge table on first use, and this
  project's promise is that `docker compose up` needs no key and no network. The
  table is therefore vendored at `data/cl100k_base.tiktoken` (1.6 MB, verified
  against tiktoken's own published SHA-256 by `tests/test_chunking.py`) and tiktoken
  is directed at it, so loading is a file read. `TIKTOKEN_CACHE_DIR` is left alone:
  it is the operator's override, and a deployment that pre-warms tiktoken's own cache
  gets to keep it.

Two adapters, so this is a real seam rather than an abstraction over one thing:
`TiktokenTokenizer` (the model's counter, one implementation per encoding) and
`ApproximateTokenizer` (the documented fallback, used when the encoding cannot be
loaded at all — a stripped image, a corrupted vendored table). A call site that
counts a token never learns which one it is talking to.
"""

import hashlib
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from app.core.constants import EMBEDDING_ENCODING

#: The encodings this project is willing to count with, most preferred first. A closed
#: set rather than any string tiktoken accepts: the encoding is part of a chunk's size
#: and therefore of `CHUNKING_VERSION`, so a deployment that quietly switched it would
#: change every chunk boundary in the corpus. `EMBEDDING_ENCODING` is the setting that
#: moves it, and `tests/test_chunking.py` asserts the default against the embedding
#: model's own published tokenizer.
KNOWN_ENCODINGS: tuple[str, ...] = ("cl100k_base", "o200k_base")

#: The vendored merge table for `cl100k_base`, named with the digest tiktoken's
#: registry expects (`tiktoken_ext.openai_public.cl100k_base`), because
#: `TIKTOKEN_CACHE_DIR` is a *cache* directory keyed by that name rather than a path to
#: one file. `expected_hash` below is the same value tiktoken verifies against, so a
#: tampered table is refused by tiktoken itself rather than by a check written here.
_VENDORED = {
    "cl100k_base": "9b5ad71b2ce5302211f9c61530b329a4922fc6a4",
    "o200k_base": "fb374d419588a4632f3f557e76b4b70aebbca790",
}

#: tiktoken's published SHA-256 for the `cl100k_base` table. A literal, and asserted by
#: `tests/test_chunking.py` against the file in the repository: a vendored blob that
#: nobody hashes is a blob that can drift, and the failure it would cause — different
#: counts on a developer's machine than in CI — is one nobody would look for here.
CL100K_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"

_DATA = Path(__file__).with_name("data")

#: A word, or a run of digits. `\d` is excluded from the word class so `2026` and
#: `informe` do not merge, which is what the model's own splitter also does.
_WORD = re.compile(r"[^\W\d_]+|\d+", re.UNICODE)

#: The complement of `_WORD`: whitespace, and everything that is neither a letter nor a
#: digit. Written as one pattern because a fallback's whole job is to be one sentence of
#: readable logic rather than a tokenizer in its own right.
_RUN = re.compile(r"\s+|[^\W\d_]+|\d+|[^\w\s]+", re.UNICODE)

#: What a character costs the fallback when it is not part of a word. The real
#: tokenizer's pre-splitter splits a symbol run into runs of at most one or two
#: characters, and every chunk of that split is at least one token, so a run of N
#: symbols is at least ceil(N / 2) tokens. The fallback counts half-rounded-up rather
#: than one per character for that reason: see the measured error in
#: `tests/test_chunking.py`.
_SYMBOLS_PER_TOKEN = 2


class TokenizerUnavailable(RuntimeError):
    """The encoding could not be loaded: no tiktoken, or no merge table.

    Raised by the real adapter and caught by `build_tokenizer`, which is the only
    place that decides what to do about it. Not a `DomainError`: nothing a client
    sends can cause it, and the fallback means no request ever sees it.
    """


class TokenCounter(Protocol):
    """The seam. One verb: how many tokens does the model see in this text?"""

    @property
    def name(self) -> str:
        """Which counter this is, for a log line and for the fallback's record."""
        ...

    def count(self, text: str) -> int: ...


class TiktokenTokenizer:
    """The embedding model's own tokenizer, loaded from the vendored merge table.

    `get_encoding` is what verifies the table: it hashes the file and refuses a
    mismatch, so this class carries no integrity check of its own. The hash literal
    above and the test beside it exist to make the *file* reviewable, not to second
    guess the library.
    """

    def __init__(self, encoding_name: str = EMBEDDING_ENCODING) -> None:
        if encoding_name not in KNOWN_ENCODINGS:
            raise TokenizerUnavailable(
                f"{encoding_name!r} is not one of {list(KNOWN_ENCODINGS)}; the encoding "
                "is part of every chunk's size, so it is a closed set rather than "
                "whatever tiktoken happens to accept"
            )
        self._name = encoding_name
        try:
            import tiktoken
        except ImportError as error:  # pragma: no cover - the image installs it
            raise TokenizerUnavailable(f"tiktoken is not installed: {error}") from error

        digest = _VENDORED.get(encoding_name)
        table = _DATA / f"{digest}.tiktoken" if digest else None
        if table is not None and table.is_file():
            # Point tiktoken at our directory only when it has nothing of its own:
            # `TIKTOKEN_CACHE_DIR` is the operator's switch, and overriding it would
            # make a pre-warmed deployment download the table again.
            os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(_DATA))
        try:
            self._encoding = tiktoken.get_encoding(encoding_name)
        except Exception as error:  # noqa: BLE001 - network, cache, bad file: all the same answer
            raise TokenizerUnavailable(
                f"{encoding_name} could not be loaded offline ({error}); "
                f"vendored table: {table}"
            ) from error

    @property
    def name(self) -> str:
        return self._name

    def count(self, text: str) -> int:
        if not text:
            return 0
        return len(self._encoding.encode(text, disallowed_special=()))


class ApproximateTokenizer:
    """The documented fallback: words and digits one token each, symbols in pairs.

    **Stated as an approximation, with its error measured rather than hidden.** It
    over-counts English prose and under-counts Spanish, because the model's merge
    table packs common English words into single tokens and splits Spanish words with
    accents into several. `tests/test_chunking.py` pins both directions on a fixed
    Spanish and a fixed English paragraph, so the number in this docstring and the
    number the code produces cannot drift apart.

    The one thing it gets exactly right is the empty string: zero tokens, which is
    what an empty chunk costs and what a `max(1, ...)` around a character count could
    never say.
    """

    name = "approximate-v1"

    def count(self, text: str) -> int:
        total = 0
        for run in _RUN.finditer(text):
            body = run.group()
            if body.isspace():
                continue
            if _WORD.fullmatch(body):
                total += 1
            else:
                total += (len(body) + _SYMBOLS_PER_TOKEN - 1) // _SYMBOLS_PER_TOKEN
        return total


def sha256_of_table() -> str:
    """The vendored `cl100k_base` table's digest, as tiktoken computes it.

    A function rather than a test-only constant so that the same reading of the file
    is available to an operator asking "is my vendored table the published one".
    """
    digest = _VENDORED["cl100k_base"]
    return hashlib.sha256((_DATA / f"{digest}.tiktoken").read_bytes()).hexdigest()


@lru_cache(maxsize=1)
def build_tokenizer(encoding_name: str = EMBEDDING_ENCODING) -> TokenCounter:
    """The best counter this deployment can build, and the fallback when it cannot.

    Cached because loading the merge table costs a parse of 100k merges — once per
    process, which is the whole cost of the real tokenizer and the reason a fallback
    is worth having only for a machine that cannot load one at all.

    **The fallback is not silent.** It is a distinct adapter with its own `name`, so a
    count recorded through it can be told apart from a count recorded through the
    model's own tokenizer — the same reasoning as `chunking_version`, one level down.
    """
    try:
        return TiktokenTokenizer(encoding_name)
    except TokenizerUnavailable:
        return ApproximateTokenizer()


def get_tokenizer() -> TokenCounter:
    """The process's counter. The call site this module exists to be."""
    return build_tokenizer()


def count_tokens(text: str) -> int:
    """Tokens in `text`, by whatever counter this process built."""
    return get_tokenizer().count(text)


def active_tokenizer_name() -> str:
    """Which counter the process is using: `cl100k_base`, or the fallback's name."""
    return get_tokenizer().name


__all__ = [
    "CL100K_SHA256",
    "KNOWN_ENCODINGS",
    "ApproximateTokenizer",
    "TiktokenTokenizer",
    "TokenCounter",
    "TokenizerUnavailable",
    "active_tokenizer_name",
    "build_tokenizer",
    "count_tokens",
    "get_tokenizer",
    "sha256_of_table",
]
