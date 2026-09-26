"""Organisation error codes.

The alias name is the catalogue name. `OrgErrorCode.ORG_DEPARTMENT_NOT_FOUND` and
`ErrorCode.ORG_DEPARTMENT_NOT_FOUND` are the same member, so a grep for either
finds both — the class only decides which vocabulary a module prefers to write in.

An earlier version shortened the names (`DEPARTMENT_NOT_FOUND` inside
`OrgErrorCode`), which meant reading a raise site required knowing which of three
domains you were in. `tests/test_error_aliases.py` enforces the convention.
"""

from app.core.errors import ErrorCode


class OrgErrorCode:
    ORG_DEPARTMENT_NOT_FOUND = ErrorCode.ORG_DEPARTMENT_NOT_FOUND
    ORG_DEPARTMENT_CODE_TAKEN = ErrorCode.ORG_DEPARTMENT_CODE_TAKEN
    ORG_DEPARTMENT_NOT_EMPTY = ErrorCode.ORG_DEPARTMENT_NOT_EMPTY
    ORG_DEPARTMENT_HAS_CHILDREN = ErrorCode.ORG_DEPARTMENT_HAS_CHILDREN
    ORG_DEPARTMENT_PARENT_INVALID = ErrorCode.ORG_DEPARTMENT_PARENT_INVALID
    ORG_DEPARTMENT_MOVE_INTO_DESCENDANT = ErrorCode.ORG_DEPARTMENT_MOVE_INTO_DESCENDANT
    ORG_DEPARTMENT_DEPTH_EXCEEDED = ErrorCode.ORG_DEPARTMENT_DEPTH_EXCEEDED
    ORG_MANAGER_NOT_IN_DEPARTMENT = ErrorCode.ORG_MANAGER_NOT_IN_DEPARTMENT


__all__ = ["OrgErrorCode"]
