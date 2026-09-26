"""Position error codes.

Aliases over the shared catalogue, so this module refers to its own errors
without reaching for HTTP-shaped constants.
"""

from app.core.errors import ErrorCode


class PositionErrorCode:
    POSITION_NOT_FOUND = ErrorCode.POSITION_NOT_FOUND
    POSITION_CODE_TAKEN = ErrorCode.POSITION_CODE_TAKEN
    POSITION_DEPARTMENT_INVALID = ErrorCode.POSITION_DEPARTMENT_INVALID
    POSITION_IN_USE = ErrorCode.POSITION_IN_USE


__all__ = ["PositionErrorCode"]
