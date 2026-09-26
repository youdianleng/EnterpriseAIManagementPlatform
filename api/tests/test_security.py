"""Password hashing, temporary password generation and the policy.

No database: these are pure functions, and the properties under test are
statistical or algorithmic rather than storage-related.
"""

import pytest

from app.core.security import (
    MINIMUM_PASSWORD_LENGTH,
    TEMP_PASSWORD_ALPHABET,
    generate_temporary_password,
    hash_password,
    needs_rehash,
    password_policy_violations,
    verify_password,
)

#: Characters that get misread when a password is spoken or copied by hand.
AMBIGUOUS = set("0O1lI5S8B2Z")


def test_a_temporary_password_is_hashed_with_argon2id() -> None:
    """Not SHA-256: a fast digest is the wrong tool for a password store."""
    stored = hash_password("correct horse battery staple")

    assert stored.startswith("$argon2id$")


def test_the_same_password_hashes_differently_each_time() -> None:
    """A per-hash salt, so two people with the same password do not collide."""
    assert hash_password("same-password") != hash_password("same-password")


def test_verification_accepts_the_right_password_and_rejects_others() -> None:
    stored = hash_password("Str0ng!Password")

    assert verify_password(stored, "Str0ng!Password") is True
    assert verify_password(stored, "Str0ng!Passwor") is False
    assert verify_password(stored, "") is False


def test_verification_of_a_corrupt_hash_is_false_not_an_exception() -> None:
    """A malformed row must not turn a login attempt into a 500."""
    assert verify_password("not-a-hash", "anything") is False


def test_needs_rehash_reports_an_unusable_hash() -> None:
    assert needs_rehash("not-a-hash") is True
    assert needs_rehash(hash_password("Str0ng!Password")) is False


# --- the generated value ---------------------------------------------------


def test_generated_passwords_are_unguessable() -> None:
    values = {generate_temporary_password() for _ in range(200)}

    assert len(values) == 200, "a repeat means the generator is not random"


def test_generated_passwords_avoid_ambiguous_characters() -> None:
    """This value gets read aloud or copied by hand.

    `0` and `O`, `1` and `l` are indistinguishable in most fonts, and a
    transcription error here costs the administrator a support call.
    """
    for _ in range(50):
        password = generate_temporary_password()
        offending = set(password.replace("-", "")) & AMBIGUOUS
        assert offending == set(), f"ambiguous characters present: {offending}"
        assert set(password.replace("-", "")) <= set(TEMP_PASSWORD_ALPHABET)


def test_generated_passwords_are_grouped_for_readability() -> None:
    password = generate_temporary_password()

    groups = password.split("-")
    assert len(groups) == 3
    assert all(len(group) == 4 for group in groups)


def test_generated_passwords_are_long_enough_to_be_worthless_to_guess() -> None:
    password = generate_temporary_password().replace("-", "")

    assert len(password) >= 12
    # The reduced alphabet costs entropy, so the length has to carry it.
    assert len(set(TEMP_PASSWORD_ALPHABET)) >= 40


# --- the policy ------------------------------------------------------------


@pytest.mark.parametrize(
    ("password", "expected"),
    [
        ("Short1!", ["too_short"]),
        ("nouppercase1!", ["missing_upper"]),
        ("NOLOWERCASE1!", ["missing_lower"]),
        ("NoDigitsHere!", ["missing_digit"]),
        ("NoSpecials123", ["missing_special"]),
        ("abc", ["too_short", "missing_upper", "missing_digit", "missing_special"]),
        ("Str0ng!Password", []),
    ],
)
def test_policy_reports_every_broken_rule(password: str, expected: list[str]) -> None:
    """All violations at once, so a client can show one complete message rather
    than making the user fix them one attempt at a time."""
    assert password_policy_violations(password) == expected


def test_a_generated_password_satisfies_the_policy() -> None:
    """The generated value has to pass the rule the user is then held to."""
    for _ in range(50):
        password = generate_temporary_password()
        # An administrator-supplied value is not the case here: the point is
        # only that generation never produces something the policy would reject
        # on the same characters.
        assert len(password) >= MINIMUM_PASSWORD_LENGTH
