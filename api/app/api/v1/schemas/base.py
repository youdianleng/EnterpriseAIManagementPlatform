"""Shared schema base classes.

`StrictModel` refuses unknown fields. That matters here because the employee
payload deliberately has a closed set of columns: silently dropping a field the
client sent would let a caller believe an ID number or bank detail had been
stored. A 422 says plainly that this system does not hold that data.
"""

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Request body: unknown fields are an error, not something to ignore."""

    model_config = ConfigDict(extra="forbid")


__all__ = ["StrictModel"]
