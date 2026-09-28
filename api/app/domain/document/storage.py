"""Where an uploaded original is kept, behind a seam.

`docs/architecture/codebase-design.md` §4 lists file storage as a real seam — a
volume in production, an in-memory double in tests — and this is that interface. One
implementation ships (`LocalFileStore`); the second is the recording double in
`tests/support/documents.py`, which is what makes it a seam rather than an
abstraction over one thing.

**The path is content-addressed**, and the rule is worth stating because it is what
makes the rest of the module simple:

    <root>/<sha256[0:2]>/<sha256>.<extension>        e.g. ab/ab12…9f.pdf

Three consequences, all of them wanted:

* **The same bytes are one file.** Storing twice is writing the same path twice, so
  a retried request, a re-run of the job and two people uploading the same PDF all
  converge on one object on disk without coordination. The fan-out on the first two
  hex characters keeps any one directory from holding a hundred thousand entries,
  which is a filesystem fact rather than a design preference.
* **The name cannot influence the path.** A crafted filename is a name, never a
  directory: `files.safe_filename` reduces it to one harmless segment, and the
  physical layout does not consult it at all. Traversal is impossible twice over.
* **The row stores a key, not an absolute path.** `documents.storage_path` is
  `<sha256[0:2]>/<sha256>.<ext>` relative to the configured root, so moving the root
  between environments — a container volume, a bind mount, a test's temporary
  directory — changes no row.

Deletion is deliberately absent. A document's original is the thing the design keeps
for reference (`保留原始文件`), and nothing in this ticket removes a document; a
`delete` on the interface would be a method written before the rule that governs it.
"""

import hashlib
from pathlib import Path
from typing import Protocol

#: What a chunk of bytes is called on disk when the extension is unknown. Unreachable
#: through the upload path — the accepted table always has one — and stated so the
#: path builder has no branch that silently produces a name with no extension.
DEFAULT_EXTENSION = ".bin"

#: How many hex characters become the fan-out directory.
_PREFIX_LENGTH = 2


class FileStore(Protocol):
    """The seam. Three verbs: keep it, hand it back, say whether it is there."""

    def put(self, content: bytes, *, extension: str) -> str:
        """Keep these bytes and return the key they are stored under.

        Idempotent by construction: the key is derived from the content, so storing
        the same bytes twice is storing one object.
        """
        ...

    def get(self, key: str) -> bytes:
        """The stored bytes. Raises `FileNotFoundError` when the key is not there."""
        ...

    def exists(self, key: str) -> bool: ...


def storage_key(content: bytes, extension: str) -> str:
    """The key these bytes belong at, derived from the content hash.

    The hash is computed **here and in `LocalFileStore.put` from the same bytes**, so
    the row's `content_sha256` and the file's path cannot disagree: they are two
    readings of one digest, not two computations that must match.
    """
    return key_for_digest(content_digest(content), extension)


def key_for_digest(digest: str, extension: str) -> str:
    """`<first two hex>/<digest><extension>`, with the extension normalised."""
    suffix = extension if extension.startswith(".") else f".{extension}" if extension else ""
    return f"{digest[:_PREFIX_LENGTH]}/{digest}{suffix or DEFAULT_EXTENSION}"


def content_digest(content: bytes) -> str:
    """`sha256` of the bytes, lowercase hex — the value `content_sha256` stores."""
    return hashlib.sha256(content).hexdigest()


class LocalFileStore:
    """The volume-backed implementation.

    Directories are created on demand rather than at startup: a deployment that
    receives no documents needs no storage root, and creating one is the first
    upload's business rather than the process's.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def put(self, content: bytes, *, extension: str) -> str:
        key = storage_key(content, extension)
        path = self._path(key)
        if path.exists():
            # The bytes are the key, so a file that is already there is already
            # these bytes. Rewriting it would be a write with nothing to change.
            return key
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written to a sibling and moved into place, so a reader never observes a
        # half-written file: `os.replace` is atomic on one filesystem, which is the
        # guarantee a concurrent download needs while an upload is finishing.
        temporary = path.with_name(f".{path.name}.part")
        temporary.write_bytes(content)
        temporary.replace(path)
        return key

    def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return path.read_bytes()
        except FileNotFoundError as error:
            raise FileNotFoundError(f"no stored document at {key!r}") from error

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def _path(self, key: str) -> Path:
        """Resolve a key under the root, refusing anything that leaves it.

        The keys this module writes cannot escape — `key_for_digest` builds them from
        a hex digest — so this is the defence for a *row* somebody edited by hand, or
        a future key format that forgets. `Path.resolve` gives the real location with
        every `..` and symlink already applied, and it is compared against the
        resolved root, which is the only form of the check that cannot be fooled by
        either.
        """
        candidate = (self._root / key).resolve()
        root = self._root.resolve()
        if candidate != root and not candidate.is_relative_to(root):
            raise ValueError(f"storage key {key!r} resolves outside the storage root")
        return candidate


__all__ = [
    "DEFAULT_EXTENSION",
    "FileStore",
    "LocalFileStore",
    "content_digest",
    "key_for_digest",
    "storage_key",
]
