"""Password hashing and temporary password generation.

Argon2id, not SHA-256.

The originating answer for this ticket said the temporary password should be
"generated as a SHA-256 password". SHA-256 is a fast general-purpose digest: it
is designed to be cheap, which is exactly the wrong property for a password
store, because it lets an attacker test billions of candidates per second against
a leaked hash. The design baseline (`docs/DESIGN.md`, password policy) specifies
Argon2id, so that is what is implemented here.

The phrase was probably about *generating* the temporary value rather than
storing it; the generation is random and the storage is Argon2id.
"""

import secrets
import string

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from argon2.low_level import Type

#: Deliberately excludes characters that are misread when a password is spoken
#: aloud or copied by hand: 0/O, 1/l/I, 5/S, 8/B, 2/Z.
TEMP_PASSWORD_ALPHABET = "ACDEFGHJKLMNPQRTUVWXYacdefghijkmnpqrtuvwxy34679"

TEMP_PASSWORD_LENGTH = 12

# Separators make a spoken password unambiguous and stop a transcription error
# from being invisible. The alphabet excludes the separator character.
TEMP_PASSWORD_GROUP = 4

_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,  # 64 MiB
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)

#: The password policy, in one place. Ticket 10 enforces it on change; keeping
#: it here means the rule cannot drift between creation and change.
MINIMUM_PASSWORD_LENGTH = 8
REQUIRED_CHARACTER_CLASSES = ("lower", "upper", "digit", "special")
_SPECIAL_CHARACTERS = set(string.punctuation)


def generate_temporary_password() -> str:
    """A one-time password that is readable over the phone and unambiguous.

    Grouped and drawn from a reduced alphabet so a mistyped character is noticed
    rather than silently wrong.
    """
    raw = "".join(secrets.choice(TEMP_PASSWORD_ALPHABET) for _ in range(TEMP_PASSWORD_LENGTH))
    return "-".join(
        raw[index : index + TEMP_PASSWORD_GROUP]
        for index in range(0, len(raw), TEMP_PASSWORD_GROUP)
    )


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, candidate: str) -> bool:
    """Constant-time verification; never raises for a wrong password."""
    try:
        return _hasher.verify(stored_hash, candidate)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True when the stored hash predates the current cost parameters."""
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def password_policy_violations(password: str) -> list[str]:
    """Which policy rules a candidate password breaks, for a readable message.

    Returns rule names, not sentences: the transport layer renders them in the
    caller's language, and an empty list means the password is acceptable.
    """
    violations: list[str] = []
    if len(password) < MINIMUM_PASSWORD_LENGTH:
        violations.append("too_short")
    if not any(character.islower() for character in password):
        violations.append("missing_lower")
    if not any(character.isupper() for character in password):
        violations.append("missing_upper")
    if not any(character.isdigit() for character in password):
        violations.append("missing_digit")
    if not any(character in _SPECIAL_CHARACTERS for character in password):
        violations.append("missing_special")
    return violations
