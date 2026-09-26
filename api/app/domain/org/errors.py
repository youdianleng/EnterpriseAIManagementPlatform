"""Organisation error codes.

An alias over the shared catalogue rather than a second definition: the wire
codes stay in `app.core.errors`, but code that belongs to the organisation
module refers to them through this name, which keeps the dependency pointing at
its own domain instead of at HTTP-shaped constants.
"""

from app.core.errors import ErrorCode


class OrgErrorCode:
    DEPARTMENT_NOT_FOUND = ErrorCode.ORG_DEPARTMENT_NOT_FOUND
    DEPARTMENT_CODE_TAKEN = ErrorCode.ORG_DEPARTMENT_CODE_TAKEN
    DEPARTMENT_NOT_EMPTY = ErrorCode.ORG_DEPARTMENT_NOT_EMPTY
    DEPARTMENT_HAS_CHILDREN = ErrorCode.ORG_DEPARTMENT_HAS_CHILDREN
    DEPARTMENT_PARENT_INVALID = ErrorCode.ORG_DEPARTMENT_PARENT_INVALID
    DEPARTMENT_MOVE_INTO_DESCENDANT = ErrorCode.ORG_DEPARTMENT_MOVE_INTO_DESCENDANT
    DEPARTMENT_DEPTH_EXCEEDED = ErrorCode.ORG_DEPARTMENT_DEPTH_EXCEEDED


__all__ = ["OrgErrorCode"]
