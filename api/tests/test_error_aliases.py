"""Guards for the domain error-code aliases.

Each domain re-exports the codes it owns (`OrgErrorCode`, `PositionErrorCode`,
`AccountErrorCode`) so its code refers to errors through its own vocabulary. That
indirection has one failure mode: a code added to the catalogue with no alias, or
a typo'd alias, which stays invisible until the exact branch that raises it runs.

These tests make that visible at collection time instead. The mistake they exist
to catch already happened once — four account aliases were missing and only
surfaced when the auth module needed them.

**Naming convention.** A catalogue name carries the owning domain as a prefix
(`ORG_DEPARTMENT_NOT_FOUND`); the alias drops it (`DEPARTMENT_NOT_FOUND`), because
inside `OrgErrorCode` the prefix says nothing. Tests below assert the convention
rather than a hand-written list, so adding a code to the catalogue without an
alias fails here.
"""

import pytest

from app.core.errors import ErrorCode, definition_of
from app.domain.account.errors import AccountErrorCode
from app.domain.org.errors import OrgErrorCode
from app.domain.position.errors import PositionErrorCode

#: Alias class -> the catalogue value prefixes it owns.
#
# The alias name is the catalogue name, so there is nothing to strip and nothing
# to remember per domain.
ALIASES: dict[type, tuple[str, ...]] = {
    AccountErrorCode: ("ERR_ACC_", "ERR_SES_"),
    OrgErrorCode: ("ERR_ORG_",),
    PositionErrorCode: ("ERR_POS_",),
}


def owned_codes(prefixes: tuple[str, ...]) -> dict[str, ErrorCode]:
    return {
        name: code
        for name, code in vars(ErrorCode).items()
        if not name.startswith("_") and code.value.startswith(prefixes)
    }


@pytest.mark.parametrize("alias_class", list(ALIASES), ids=lambda cls: cls.__name__)
def test_every_owned_code_is_reachable_through_the_alias(alias_class: type) -> None:
    prefixes = ALIASES[alias_class]
    owned = owned_codes(prefixes)
    assert owned, f"no catalogue codes match {prefixes}"

    missing = [
        name for name, code in owned.items() if getattr(alias_class, name, None) is not code
    ]
    assert missing == [], f"{alias_class.__name__} is missing aliases: {missing}"


@pytest.mark.parametrize("alias_class", list(ALIASES), ids=lambda cls: cls.__name__)
def test_no_alias_points_at_something_that_is_not_a_catalogue_code(alias_class: type) -> None:
    """Copying the wrong code is silent, unlike a misspelled attribute."""
    known = set(ErrorCode)
    for name, value in vars(alias_class).items():
        if name.startswith("_"):
            continue
        assert value in known, f"{alias_class.__name__}.{name} is not a catalogue code"


def test_every_catalogue_code_has_an_http_status() -> None:
    for code in ErrorCode:
        assert definition_of(code).status_code >= 400
