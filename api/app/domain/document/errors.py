"""Document error codes.

Aliases of the catalogue rather than a second list, for the reason
`domain/timesheet/errors.py` records: an enumerated copy drifts, and the drift only
shows up in the branch that raises the missing code.
"""

from app.core.errors import ErrorCode


class DocumentErrorCode:
    """Every code this module raises, plus the lookups it depends on."""

    DOCUMENT_NOT_FOUND = ErrorCode.DOCUMENT_NOT_FOUND
    DOCUMENT_UPLOAD_TYPE_UNSUPPORTED = ErrorCode.DOCUMENT_UPLOAD_TYPE_UNSUPPORTED
    DOCUMENT_UPLOAD_TOO_LARGE = ErrorCode.DOCUMENT_UPLOAD_TOO_LARGE
    DOCUMENT_UPLOAD_EMPTY = ErrorCode.DOCUMENT_UPLOAD_EMPTY
    DOCUMENT_NOT_READY = ErrorCode.DOCUMENT_NOT_READY
    DOCUMENT_DUPLICATE = ErrorCode.DOCUMENT_DUPLICATE
    DOCUMENT_REPROCESS_UNSUPPORTED = ErrorCode.DOCUMENT_REPROCESS_UNSUPPORTED
    DOCUMENT_FILE_MISSING = ErrorCode.DOCUMENT_FILE_MISSING
    #: Grouped here because a caller of this module reasons about "why was this
    #: upload refused", not about which enum a code happens to live in.
    INVALID_REQUEST = ErrorCode.INVALID_REQUEST
    FORBIDDEN = ErrorCode.FORBIDDEN


__all__ = ["DocumentErrorCode"]
