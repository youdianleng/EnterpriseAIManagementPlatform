"""What this module accepts, and what it does to a filename before storing it.

Three decisions, each of them about a boundary the rest of the pipeline leans on:

* **The accepted set is a closed table, not a prefix test.** `SUPPORTED` names one
  media type per extension and the extension is the *only* thing consulted. A
  content-type header is what the client claims, and trusting it would mean a file
  called `payroll.exe` with `application/pdf` in its header reaching the PDF parser.
  An extension the table does not name is refused with
  `DOCUMENT_UPLOAD_TYPE_UNSUPPORTED`, whose message names the five formats.

* **`safe_filename` is where path traversal is removed, before anything else looks
  at the name.** The stored path is content-addressed (`storage.py`), so a crafted
  name could not reach it today — but the name is kept, returned and shown, and a
  defence that only exists because of how a *second* function happens to build its
  path is a defence that disappears the day somebody adds a name-based layout. So the
  name is reduced to a single harmless path segment here, once:

  - everything up to and including the last `/` or `\\` is dropped, which is what
    turns `../../etc/passwd` into `passwd` and `C:\\Windows\\evil.pdf` into
    `evil.pdf`;
  - `..` as a whole name becomes `document`, because "the parent directory" is not a
    filename and an empty result would be worse;
  - control characters (including NUL, `\\r`, `\\n`) are removed, since a name travels
    into a `Content-Disposition` header and a log line;
  - leading dots and spaces are stripped, so a name cannot be a hidden file or one
    that a shell trims;
  - anything outside letters, digits, space and `. _ ( ) + - [ ]` becomes `_`. The
    allowed set is written out rather than the forbidden one, so a character nobody
    thought about is removed instead of admitted;
  - what is left is capped at 120 characters with the extension preserved, because a
    4 000-character name is a name no filesystem or header should have to carry.

  A name that is empty after all that becomes `document`. The extension is taken
  *after* normalisation, so it is the extension of the safe name and not of the
  crafted one.
"""

import re
from dataclasses import dataclass

#: The ceiling the ticket names, in bytes. Refused rather than truncated.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

#: The extension → media type table. One media type per extension, and the extension
#: is what decides: `SUPPORTED` is the whole answer to "may this be uploaded".
SUPPORTED: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
    ".md": "text/markdown",
    # Markdown's older extension, and the one most editors still write. Mapping it to
    # the same reader is the whole change: there is no second Markdown format.
    ".markdown": "text/markdown",
}

#: What a file with no usable name is called.
FALLBACK_NAME = "document"

#: The longest stored filename. Long enough for a real title, short enough that no
#: header or filesystem has to think about it.
MAX_FILENAME_LENGTH = 120

#: Characters removed from a name: the C0 and C1 control ranges, which include the
#: NUL and the newlines that would split a header or a log line.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

#: What a name may contain. Everything else becomes `_`.
_UNSAFE = re.compile(r"[^A-Za-z0-9._ ()+\-\[\]]")


@dataclass(frozen=True, slots=True)
class AcceptedFile:
    """A filename this module is willing to store, and what it is.

    `media_type` is the table's answer, never the client's: the parsers dispatch on
    it, so a request cannot choose its own reader.
    """

    filename: str
    extension: str
    media_type: str


def safe_filename(raw: str | None) -> str:
    """One harmless path segment, derived from whatever the client sent.

    See the module docstring for what each step removes and why. Returns
    `FALLBACK_NAME` when nothing usable is left, so callers never have to handle an
    empty name.
    """
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _CONTROL.sub("", name)
    name = name.strip().lstrip(".")
    if name in {"", ".", ".."}:
        return FALLBACK_NAME
    stem, extension = _split_extension(name)
    stem = _UNSAFE.sub("_", stem).strip(" .") or FALLBACK_NAME
    # The extension survived normalisation only if it is in the table's alphabet, so
    # it is re-attached unchanged after the stem has been capped.
    return _cap(stem, extension)


def accept(raw: str | None) -> AcceptedFile | None:
    """The accepted file, or `None` when the extension is not in the table.

    `None` rather than an exception: the caller has an error code and a document title
    to build from it, and a failure that is a value is easier to test than one that is a
    `raise` at the bottom of a call chain. The extension is lowercased, so `.PDF` and
    `.pdf` are the same format — which is what a client that uppercases a name expects,
    and what a table keyed by one spelling requires.
    """
    filename = safe_filename(raw)
    stem, extension = _split_extension(filename)
    media_type = SUPPORTED.get(extension.lower())
    if not stem or media_type is None:
        return None
    return AcceptedFile(filename=filename, extension=extension.lower(), media_type=media_type)


def _split_extension(name: str) -> tuple[str, str]:
    """`("informe", ".pdf")` for `informe.pdf`; `(name, "")` when there is no dot.

    A leading dot does not start an extension, so `.bashrc` has no extension rather
    than being a file "of type bashrc".
    """
    dot = name.rfind(".")
    if dot <= 0:
        return name, ""
    return name[:dot], name[dot:].lower()


def _cap(stem: str, extension: str) -> str:
    """`stem + extension`, at most `MAX_FILENAME_LENGTH` characters overall."""
    room = MAX_FILENAME_LENGTH - len(extension)
    if room <= 0:  # pragma: no cover - no extension in the table is that long
        return (stem + extension)[:MAX_FILENAME_LENGTH]
    return stem[:room] + extension


__all__ = [
    "FALLBACK_NAME",
    "MAX_FILENAME_LENGTH",
    "MAX_UPLOAD_BYTES",
    "SUPPORTED",
    "AcceptedFile",
    "accept",
    "safe_filename",
]
