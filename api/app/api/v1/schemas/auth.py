"""Authentication schemas."""

from uuid import UUID

from pydantic import BaseModel, Field

from app.api.v1.schemas.base import StrictModel


class LoginRequest(StrictModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class PasswordChangeRequest(StrictModel):
    current_password: str = Field(min_length=1, max_length=200)
    # Only "non-empty": the length and character-class rules live in
    # `core.security`, and restating them here would answer a weak password with
    # a generic validation error instead of the catalogued policy error.
    new_password: str = Field(min_length=1, max_length=200)


class PasswordPolicyRead(BaseModel):
    minimum_length: int
    required_classes: list[str]


class SessionRead(BaseModel):
    user_id: UUID
    username: str
    employee_id: UUID
    #: The name to greet somebody by. Carried here so the shell that renders the
    #: signed-in header needs one request rather than two: the client already has
    #: a session, and asking it for its own name should not be a second round trip
    #: with its own failure mode.
    employee_full_name: str = ""
    #: When true the client must route to the change-password screen; every other
    #: endpoint answers 403 with `errors.password_change_required` until it is done.
    must_change_password: bool
    #: The roles this account holds, so a shell can decide what to *advertise* without a
    #: second request — ticket 44's payslip screen is Finance's alone, and §4.1's rule that
    #: an interface shows no entry its reader cannot open is the reason the list travels
    #: here. It is not an authority: every route still asks the kernel, and a client that
    #: ignored this field would simply be refused by the API it called. Sorted, so two
    #: responses for one account are one string.
    roles: list[str] = []
