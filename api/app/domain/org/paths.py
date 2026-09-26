"""Department path rules.

An ltree label may contain only `[A-Za-z0-9_]`. Department codes are written by
humans and tend to contain hyphens, so a code is sanitised into a path segment
rather than being rejected. The mapping is injective for the cases that matter
here: `-` becomes `_`, and a literal `_` is escaped as `__`.
"""

import re

# Matches anything outside the allowed label alphabet (after underscore escaping).
_INVALID_LABEL = re.compile(r"[^A-Za-z0-9_]")


def to_label(code: str) -> str:
    """Convert a department code into a single ltree label.

    Underscores are doubled first, then anything outside the alphabet becomes an
    underscore. Doubling is what keeps the mapping reversible: `r-d` maps to
    `r_d` while `r_d` maps to `r__d`.

    Doubling is done by scanning rather than with a regex: a lookaround pattern
    keeps matching inside its own replacement (`r_and_d` grew a new match each
    pass), and a plain `replace('_', '__')` is not idempotent either.
    """
    doubled = "".join("__" if character == "_" else character for character in code)
    cleaned = _INVALID_LABEL.sub("_", doubled)
    return cleaned or "unnamed"


def child_path(parent_path: str | None, code: str) -> str:
    """Full path for a department, given its parent's path (None for a root)."""
    label = to_label(code)
    return label if parent_path is None else f"{parent_path}.{label}"


def depth_of(path: str) -> int:
    """Depth of a path, counting from 0 for a root label."""
    return 0 if not path else path.count(".")


def is_descendant_path(candidate: str, ancestor: str) -> bool:
    """True when `candidate` is `ancestor` itself or sits underneath it.

    Mirrors the SQL `<@` operator so the rule can be unit-tested without a
    database, and so the query and the guard cannot drift apart.
    """
    return candidate == ancestor or candidate.startswith(f"{ancestor}.")


def replace_prefix(path: str, old_prefix: str, new_prefix: str) -> str:
    """Rewrite a path when its ancestor is moved."""
    if path == old_prefix:
        return new_prefix
    suffix = path[len(old_prefix) + 1 :]
    return f"{new_prefix}.{suffix}"
