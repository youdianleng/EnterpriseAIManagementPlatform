"""Which employee a file belongs to — one rule, and the precedence question answered.

The ticket's checklist line is 「财务可…完成归属匹配」 by 文件名中的员工编号 **或** 界面选择,
so there are two routes and the module has to say what happens when they disagree. That
question is settled here, once, and the answer is in `resolve`:

    **The explicit selection wins. Where the filename named a *different* employee, the
    file is refused rather than attributed to the selection.**

That is a two-part rule and the second half is the one worth reading. Precedence alone
would be enough to attribute a file — but the selection is a click and the filename is
what the payroll bureau actually wrote, so a file whose name says `E-0007` and which was
filed against `E-0011` is much more likely to be a mistake than an override. Attributing
it would hand one employee's payslip to another and leave the first looking *missing* —
and this screen's entire value is that the missing list is truthful. So the disagreement
is reported (`DUPLICATE_FOR_EMPLOYEE`, naming both numbers) and the uploader decides.

The rest of the rule is in the same three functions:

* **`tokens` is a whole-token match, never a substring one.** A filename is cut at every
  character that is not a letter or a digit, and a staff number matches only when it is
  *equal* to one of the pieces. `nomina_E-0007_2026-03.pdf` matches `E-0007`; a file for
  `E-0007-BIS` does not, and — the case that matters — a filename carrying `E-00071`
  does not match an employee numbered `E-0007` merely because the digits are in there. A
  substring rule would attribute payslips to whichever number happened to be a prefix of
  another, which is exactly the kind of silent mis-filing this screen exists to catch.
* **Two tokens naming two different employees is a refusal**, not "the first one wins".
  There is no tie-break that is not a guess, and a guess sends somebody else's pay to the
  wrong mailbox.
* **The file type is decided by the extension *and* the first bytes.** `document.files.
  accept`'s rule is that the extension is the whole answer, and that is right for a
  knowledge base; a payslip is a file handed to a person, so a `.pdf` whose bytes are not
  a PDF is refused as `NOT_A_PDF` rather than stored and handed over as one that will not
  open.

**Every path returns a reason.** There is no `None` and no default: `resolve` answers
either "this employee" or "not attributed, and here is why", which is what makes
`UnmatchedReason` a closed vocabulary and 「不静默丢弃」 a property of the function rather
than of a caller's care.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from app.domain.payslip.models import (
    MAX_PAYSLIP_BYTES,
    PDF_MAGIC,
    EmployeeRef,
    UnmatchedReason,
    UploadedFile,
)

#: Where a name is cut into pieces, for the *reporting* rules that need pieces rather than
#: a match: the extension test and the "which number-shaped thing did you write" detail.
#: Everything that is not a letter, a digit or a `#`.
_TOKEN_SEPARATORS = re.compile(r"[^A-Za-z0-9#]+")

#: What a staff number is folded to before it is looked for: upper case, and every
#: character that is not a letter or a digit removed.
#:
#: **This is what makes `E-0007` findable in `nomina_E-0007.pdf` and in
#: `nomina-E-0007.pdf` without either spelling being the rule.** A staff number is free
#: text in this system (`employee_private.employee_no` is a `String(32)` with no format),
#: so the rule cannot assume its shape — it assumes the *name* is a run of characters with
#: separators in it, folds the number to its letters and digits, and looks for that run
#: with a boundary on either side.
#:
#: The characters a staff number may keep. Everything else in a stored number is dropped
#: by `_comparable`, so a number written `E.0007` on one row and `E-0007` in a filename are
#: the same number.
_ALNUM = re.compile(r"[^A-Za-z0-9]")

#: The extension a payslip must have. Lower-cased before the comparison, so `.PDF` is the
#: same file as `.pdf` — which is what a client that upper-cases a name expects, and what
#: a scanner's default naming produces.
PAYSLIP_EXTENSION = ".pdf"


@dataclass(frozen=True, slots=True)
class FileVerdict:
    """What a file *is*, before any question about whose it is.

    Two fields and a reason, rather than a boolean: `filename` is the normalised name the
    result reports (the raw one can carry a path or a control character, and it travels
    into a `Content-Disposition` header once ticket 45 hands the file back), and `reason`
    is non-`None` exactly when the file cannot be stored at all.

    `detail` is the size in bytes for `OVERSIZED_FILE` and `EMPTY_FILE`, and `None`
    otherwise. It is here rather than in the reason so that the *reason* stays a closed
    enum a client switches on while the sentence a person reads still says how big the
    file was.
    """

    filename: str
    reason: UnmatchedReason | None = None
    detail: str | None = None

    @property
    def acceptable(self) -> bool:
        return self.reason is None


def tokens(raw: str | None) -> tuple[str, ...]:
    """The name cut into upper-cased pieces. **Reporting, not matching.**

    Everything that is not a letter, a digit or a `#` cuts, so
    `nomina_E-0007_2026-03.pdf` yields `("NOMINA", "E", "0007", "2026", "03", "PDF")`. Those
    pieces are *not* how the number is found — `_named_employees` folds the whole name and
    searches it, which is the rule that survives a hyphen — and this function exists for the
    two things that genuinely want pieces: the extension test, which reads the last one, and
    `_number_in`, which reports "a token that looks like a staff number and is not on file"
    so finance can see which number it could not place.

    **This reads the name as it arrived, not the normalised one.** `document.files.
    safe_filename` is what the *row* stores and what a file is handed back as, and it
    replaces every character outside its own allowed set with `_` and caps the stem at 120
    characters — so a name it has been through has lost separators and may have lost the
    number itself. Matching is a read of what the payroll bureau wrote; `_matching_name` is
    the little of that normalisation which matters here.
    """
    if not raw:
        return ()
    return tuple(piece for piece in _TOKEN_SEPARATORS.split(raw.upper()) if piece)


def _matching_name(raw: str | None) -> str:
    """The name as the matching rule reads it: no directory, no control characters.

    Deliberately *not* `safe_filename` (see `tokens`): the two do different jobs. This one
    keeps every separator a payroll bureau might use and drops the two things that are not
    part of a name at all — a path, and a character that would split a log line.
    """
    if not raw:
        return ""
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    return _CONTROL.sub("", name)


#: The C0 and C1 control ranges, which include the NUL and the newlines. Written here as
#: well as in `document.files` because the two normalisations are independent: this module
#: must not start depending on the shape of a name that was built to be *stored*.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _comparable(value: str) -> str:
    """A name or a staff number as the two are compared: upper case, separators dropped.

    **One function for both sides**, which is the point: a rule that folded the number but
    not the name would look for `E-0007` inside a name that has `E`, `0007` and a hyphen in
    it, match nothing, and appear to work — every file would simply be reported as naming
    nobody. `E-0007`, `E.0007`, `e_0007` and `E 0007` all fold to `E0007` here, and the
    lookup is a boundary-anchored search for that run (see `_named_employees`).
    """
    return _ALNUM.sub("", value).upper()


def _shape(value: str) -> str:
    """The *shape* of a staff number: `A` for each letter, `0` for each digit, separators kept.

    `E-0007` is `A-0000`. Comparing shapes is how `_number_in` decides that a run of a
    filename looks like one of this company's staff numbers *at all* — which is a question
    the company can answer for itself, and a better answer than any pattern this module
    could invent. `2026-03` is `0000-00`, which is not the shape of anything on file, so a
    name carrying a date and no number reports "no staff number" rather than sending finance
    after an employee numbered `2026`.
    """
    return "".join(
        "A" if character.isalpha() else "0" if character.isdigit() else character
        for character in value
    )


def _names_employee(name: str, number: str) -> bool:
    """Whether `name` carries `number`, allowing the separators a name puts between things.

    The number's letters and digits have to appear in order, with **only** non-alphanumeric
    characters between them, and the whole run has to be bounded on each side by something
    that is not a letter or a digit. That last condition is what makes the rule a
    whole-name one rather than a substring one: `E-10012` does not name the employee
    numbered `E-1001`, and neither does `XE1001` — a substring rule matches both, and the
    consequence is not a missed payslip but a mis-filed one, somebody's pay handed to the
    wrong mailbox.

    The separator allowance is what makes a hyphen unremarkable without its being special:
    `E-1001`, `E1001` and `E_1001` are one number because the pattern is built from the
    *number's* characters and lets anything non-alphanumeric sit between them. `E--1001`
    matches too, which is the lenient reading to take: the number is unmistakable and
    refusing it would report a payslip as unattributable over a doubled hyphen.

    An empty number names nobody, which is the answer for an employee with no staff number
    at all (the column is nullable, and matching by filename cannot reach them).
    """
    alphanumeric = [character for character in number if character.isalnum()]
    if not alphanumeric:
        return False
    between = r"[^0-9A-Za-z]*"
    body = between.join(re.escape(character.upper()) for character in alphanumeric)
    pattern = rf"(?<![0-9A-Za-z]){body}(?![0-9A-Za-z])"
    return re.search(pattern, name) is not None


def classify(file: UploadedFile) -> FileVerdict:
    """The file as this module will store it, or the reason it will not.

    See the module docstring for why both the extension and the magic are checked, and
    `UnmatchedReason` for what each refusal means. The bare `except` is deliberate: a
    filename with a NUL in it raises rather than returns, and "the name could not be
    normalised" is a `NOT_A_PDF` — there is no extension to read, so there is no PDF.
    """
    from app.domain.document.files import safe_filename

    try:
        name = safe_filename(file.filename)
    except Exception:  # noqa: BLE001 - any unusable name is a file we will not store
        return FileVerdict(filename="", reason=UnmatchedReason.NOT_A_PDF)

    # Zero bytes first, because "you sent an empty part" is a more useful answer than
    # "that is not a PDF" for a file that was never anything at all. It is also the one
    # refusal the two tests below would otherwise swallow.
    if not file.content:
        return FileVerdict(filename=name, reason=UnmatchedReason.EMPTY_FILE, detail="0")
    if not name.lower().endswith(PAYSLIP_EXTENSION) or not file.content.startswith(PDF_MAGIC):
        return FileVerdict(filename=name, reason=UnmatchedReason.NOT_A_PDF)
    if len(file.content) > MAX_PAYSLIP_BYTES:
        return FileVerdict(
            filename=name,
            reason=UnmatchedReason.OVERSIZED_FILE,
            detail=f"{len(file.content)} > {MAX_PAYSLIP_BYTES}",
        )
    return FileVerdict(filename=name)


@dataclass(frozen=True, slots=True)
class Resolution:
    """One file's verdict, as `resolve` answers it.

    `employee_id` is `None` exactly when `reason` is set, and `employee_no` is the number
    the file named — which for an unknown number is the number itself rather than an
    employee's, since there is no employee to take it from.

    `detail` is a fact about *this* file that the reason alone cannot carry — the byte
    count of an oversized one — and `None` where the reason says everything.
    """

    employee_id: UUID | None
    reason: UnmatchedReason | None
    matched_by: str
    employee_no: str | None = None
    detail: str | None = None


def resolve(
    file: UploadedFile,
    employees: Sequence[EmployeeRef],
    *,
    selected: EmployeeRef | None = None,
) -> Resolution:
    """Whose payslip this file is, or why it is nobody's.

    The precedence and the refusal are the module docstring's; what is here is the order
    they are asked in, which matters:

    1. **the file has to be a PDF**, because a refusal about the file's type is a refusal
       whatever the filename says — and reporting "unknown employee number" for a JPEG
       named `notas.pdf` would send finance looking for a person instead of a file;
    2. **the filename is read**, once, into tokens;
    3. **the selection is consulted**, and the four cases of the precedence rule follow.

    `matched_by` records which route answered — `selection`, `filename` or `none` — because
    the batch's answer states it and a reviewer reading "why is this file against that
    employee" should not have to guess.
    """
    verdict = classify(file)
    if not verdict.acceptable:
        return Resolution(
            employee_id=None,
            reason=verdict.reason,
            matched_by="none",
            employee_no=_number_in(file.filename, employees),
            detail=verdict.detail,
        )

    named = _named_employees(file.filename, employees)
    named_numbers = {employee.employee_no: employee for employee in named if employee.employee_no}

    if selected is not None:
        if len(named) == 1 and named[0].employee_id != selected.employee_id:
            # The filename and the selection disagree. See the module docstring: refused,
            # because attributing it would hand one person's payslip to another and leave
            # the first looking missing.
            return Resolution(
                employee_id=None,
                reason=UnmatchedReason.DUPLICATE_FOR_EMPLOYEE,
                matched_by="none",
                employee_no=named[0].employee_no,
            )
        if len(named) > 1:
            # The filename is ambiguous *and* a selection was made. The selection is what
            # it was made for, so it settles this one — which is the ticket's 「或界面选择
            # 完成归属匹配」 read as "the selection is the answer for a file the filename
            # could not answer for".
            return Resolution(
                employee_id=selected.employee_id,
                reason=None,
                matched_by="selection",
                employee_no=selected.employee_no,
            )
        return Resolution(
            employee_id=selected.employee_id,
            reason=None,
            matched_by="selection",
            employee_no=selected.employee_no,
        )

    numbers = tuple(named_numbers)
    if len(numbers) > 1:
        return Resolution(
            employee_id=None,
            reason=UnmatchedReason.AMBIGUOUS_EMPLOYEE_NUMBER,
            matched_by="none",
            employee_no=", ".join(sorted(numbers)),
        )
    if len(numbers) == 1:
        employee = named_numbers[numbers[0]]
        return Resolution(
            employee_id=employee.employee_id,
            reason=None,
            matched_by="filename",
            employee_no=employee.employee_no,
        )

    # No employee was named. Two different facts, and the result says which: a filename
    # that carries no number at all, or one that carries a number nobody holds.
    numbered = _number_in(file.filename, employees)
    if numbered is None:
        return Resolution(
            employee_id=None,
            reason=UnmatchedReason.NO_EMPLOYEE_NUMBER,
            matched_by="none",
            employee_no=None,
        )
    return Resolution(
        employee_id=None,
        reason=UnmatchedReason.UNKNOWN_EMPLOYEE_NUMBER,
        matched_by="none",
        employee_no=numbered,
    )


def _named_employees(raw: str | None, employees: Sequence[EmployeeRef]) -> list[EmployeeRef]:
    """Every employee whose staff number this name carries as a whole run of characters.

    **One entry per employee**, not one per matching number: an employee whose two numbers
    both appear is still one person, and counting them twice would turn a file that can be
    attributed into an "ambiguous" refusal.

    The search runs over the *name as it arrived*, upper-cased: the separators are part of
    the rule (`_names_employee` lets anything non-alphanumeric sit between the number's
    characters), so folding them away first would turn `nomina_E-1001_2026-03.pdf` into one
    long run and put a digit against the number's last one.
    """
    name = _matching_name(raw).upper()
    if not name:
        return []
    found: dict[UUID, EmployeeRef] = {}
    for employee in employees:
        if _names_employee(name, _comparable(employee.employee_no or "")):
            found.setdefault(employee.employee_id, employee)
    return list(found.values())


def _number_in(raw: str | None, employees: Sequence[EmployeeRef]) -> str | None:
    """A staff-number-shaped run of the filename that no employee holds, if there is one.

    Used only for the *reason*'s detail: a filename with `E-9999` in it should tell finance
    which number it could not place. "Shaped like a staff number" is answered by the company
    itself — the run's `_shape` has to be the shape of a number that is actually on file —
    which is a better test than any pattern this module could invent, and it is what keeps a
    date out of the answer: `nomina_2026-03.pdf` has no run shaped like `A-0000`, so it
    reports "no staff number" rather than sending finance after an employee numbered `2026`.

    A number that *is* on file never reaches the caller of this function: those files were
    either attributed or refused as ambiguous, and both of those paths say so with a reason
    of their own.
    """
    name = _matching_name(raw).upper()
    known = {_comparable(employee.employee_no or "") for employee in employees}
    shapes = {
        _shape(value)
        for value in (employee.employee_no or "" for employee in employees)
        if _comparable(value)
    }
    for candidate in re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", name):
        folded = _comparable(candidate)
        if not folded or folded in known or len(folded) < 2:
            continue
        if not any(character.isdigit() for character in folded):
            continue
        if not any(character.isalpha() for character in folded):
            continue
        if _shape(candidate) not in shapes:
            continue
        return candidate
    return None


def duplicate_positions(digests: Iterable[str]) -> set[int]:
    """Which positions in a batch hold bytes that appeared earlier in the same batch.

    A file sent twice is a mistake worth naming rather than an upsert: the second copy
    would overwrite the first, so the row would carry the second's checksum while the
    first file's name is the one the uploader saw. The first occurrence is kept and every
    later one is refused, which is deterministic — the alternative, keeping the last, would
    make the answer depend on the order a client happened to send.
    """
    seen: set[str] = set()
    duplicates: set[int] = set()
    for index, digest in enumerate(digests):
        if digest in seen:
            duplicates.add(index)
        seen.add(digest)
    return duplicates


__all__ = [
    "PAYSLIP_EXTENSION",
    "FileVerdict",
    "Resolution",
    "classify",
    "duplicate_positions",
    "resolve",
    "tokens",
]
