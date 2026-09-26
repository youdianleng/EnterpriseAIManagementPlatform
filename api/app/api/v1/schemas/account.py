"""Account schemas.

`AccountCreate` takes no password: the system generates one. `AccountCreated`
is the only response type that carries a plaintext temporary password, and it is
returned exactly once.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.api.v1.schemas.base import StrictModel


class AccountCreate(StrictModel):
    employee_id: UUID
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")


class AccountRead(BaseModel):
    id: UUID
    employee_id: UUID
    username: str
    is_active: bool
    must_change_password: bool
    session_epoch: int
    last_login_at: datetime | None = None
    created_at: datetime
    employee_full_name: str = ""
    employee_email: str = ""
    #: Present only in the response that issues a new one-time password. Shown
    #: once: the hash is stored, this value is not written anywhere, so there is
    #: no second chance to read it.
    #:
    #: Flat rather than nested so every account response has the same shape —
    #: a client reads `is_active` the same way whether or not a password came
    #: with it.
    temporary_password: str | None = None


class AccountPasswordReset(StrictModel):
    reason: str | None = Field(default=None, max_length=500)


class AccountStateChange(StrictModel):
    reason: str | None = Field(default=None, max_length=500)


class PasswordChange(StrictModel):
    current_password: str = Field(min_length=1)
    # Only "non-empty" here: the length and character-class rules live in
    # `core.security`, and duplicating them as schema bounds would reject a weak
    # password with a generic validation error instead of the catalogued
    # password-policy error the client is meant to render.
    new_password: str = Field(min_length=1, max_length=200)


class PasswordPolicyViolation(BaseModel):
    """Returned inside the error envelope so a client can render a fix."""

    message_key: str


class PasswordPolicyRead(BaseModel):
    """Published so the UI can state the rule without duplicating it."""

    minimum_length: int
    required_classes: list[str]
