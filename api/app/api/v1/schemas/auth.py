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
    #: When true the client must route to the change-password screen; every other
    #: endpoint answers 403 with `errors.password_change_required` until it is done.
    must_change_password: bool
