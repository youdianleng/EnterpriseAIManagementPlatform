"""The values a draft is made of: the form, and the row that remembers it.

DESIGN §6.3 is the requirement this module shapes, and it has five parts. Four of them
are here and the fifth (the explicit confirmation) is ticket 41's:

* 「`PrefillForm` 必须是**完整、可编辑的表单**，展示所有将写入的字段值」 — `PrefillForm`
  below carries one `PrefillField` per field the *eventual submission* writes, with the
  value the assistant proposed, a bilingual label read from the catalogue, and the shape
  of the input the client should draw. `submit_path` names the endpoint the confirmed
  values will be posted to, so "the fields the submission will write" is not a claim made
  in a docstring: `tests/test_agent_draft_tools.py` compares the form's field names with
  that endpoint's own request model, minus the identity fields.
* 「草稿有**过期时间**（默认 24h），过期后 `status=expired`」 — `DraftStatus`, and
  `AgentAction.expires_at` as the row's own fact. The instant is the *database's* clock
  (`now()` on insert), never the process's, and the transition is recorded by the reader
  that observes it (`service.AgentActionService.latest_draft`).
* 「`agent_actions` 表全程留痕」 — `AgentAction` is that row, with §3.6's columns and two
  the design leaves to the implementation: `created_at`, and `expires_at` (the 24 hours
  §6.3 asks for have to live somewhere, and a value derived on read could not survive a
  changed setting).

**The identity fields are deliberately absent from the form.** The request models that
carry out a confirmation have an `employee_id` (`app/api/v1/leave.py::RequestCreate`,
`app/api/v1/attendance.py::CorrectionCreate`); the form does not, and no draft tool can
declare one — `app/ai/tools/models.py::ALLOWED_PARAMETERS` is a closed vocabulary and
`ai/tools/draft.py` never reads a subject from an argument. Whose leave it is comes from
the principal at confirmation time, which is what makes "the drafted entity is the
caller's" a property of the types rather than of a prompt. `IDENTITY_FIELDS` names them
so the test that compares a form with its endpoint can subtract exactly the fields the
platform — and never the model — supplies.

**A field's value is a JSON-native value, and nothing is a sentence.** Dates are ISO
strings, a time is `HH:MM` in Madrid's local time, minutes are integers, a project is a
uuid. The client draws an input per `FieldKind` and posts the edited values back; the
labels are the server's, so a screen does not have to hold a second copy of the wording.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

#: The request fields a *confirmation* supplies from the session rather than from the form.
#: See the module docstring: a form that carried one of these would be a form the model
#: could fill in with somebody else's name.
IDENTITY_FIELDS: frozenset[str] = frozenset({"employee_id"})


class DraftStatus(StrEnum):
    """Where a recorded draft stands. §3.6's four values, and no fifth.

    `proposed` is what the agent's draft branch writes; `confirmed`, `rejected` and
    `expired` are the three ways it stops being actionable. **`expired` is a status of the
    row and not a comparison made on read** — §6.3 says 「过期后 `status=expired`」, and a
    reader that derived it would leave the audit table saying a stale draft was still
    waiting for somebody who will never come back to it.
    """

    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    EXPIRED = "expired"


class DraftEntity(StrEnum):
    """What a draft will become if it is confirmed: the three documents §6.2 drafts.

    The value travels in `prefill_form.entity` and is what a client uses to know which
    screen (or which card) the form belongs to; it is also the vocabulary a test compares
    with the submission endpoint's own request model.
    """

    LEAVE_REQUEST = "leave_request"
    ATTENDANCE_CORRECTION = "attendance_correction"
    TIMESHEET_ENTRY = "timesheet_entry"


class FieldKind(StrEnum):
    """The input a field asks for. A closed set, because a client draws one control each.

    `TIME` is a wall-clock time on the *business date* of the same form (a correction's
    `corrected_at` is an instant built from the day and this time at confirmation). It is
    deliberately not a datetime: the endpoint wants an instant with an offset, and
    converting "the 15th at 16:10" into one is the platform's job, not an employee's.
    """

    DATE = "date"
    TIME = "time"
    TEXT = "text"
    TEXTAREA = "textarea"
    NUMBER = "number"
    SELECT = "select"


@dataclass(frozen=True, slots=True)
class FieldOption:
    """One choice of a `SELECT`, named in both languages.

    The labels are the *rows'* own names (a leave type's `name_es`/`name_en`, a project's
    and task's), which is why they are here rather than looked up from a catalogue key:
    they are data the database holds, and a client that received only codes would have to
    query the catalogue again to draw a select.
    """

    value: str
    label_es: str
    label_en: str

    def as_dict(self) -> dict[str, str]:
        return {"value": self.value, "label_es": self.label_es, "label_en": self.label_en}


@dataclass(frozen=True, slots=True)
class PrefillField:
    """One field of the eventual submission, filled in and editable.

    `name` is the submission's own field name — `start_date`, `business_date`,
    `minutes` — so the form and the endpoint cannot drift about what a value is called.
    `label_key` is the catalogue key the two sentences were read from, kept so a test can
    assert every label exists in both catalogues (and so a reader can find the wording).

    `value` may be `None`: a leave type that requires an attachment has a
    `attachment_reference` field with nothing in it yet, and a field the assistant could
    not fill is exactly what an editable form is for. `required` says whether the
    submission will refuse an empty one, and it is the *request model's* answer rather
    than a second opinion (`EntryWrite.note` is optional; `CorrectionCreate.reason` is
    not).
    """

    name: str
    label_key: str
    kind: FieldKind
    label_es: str
    label_en: str
    value: str | int | None = None
    required: bool = True
    options: tuple[FieldOption, ...] = ()
    hint_es: str | None = None
    hint_en: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON-native, because this travels into a `JSONB` column and back out of it."""
        return {
            "name": self.name,
            "label_key": self.label_key,
            "kind": str(self.kind),
            "label_es": self.label_es,
            "label_en": self.label_en,
            "value": self.value,
            "required": self.required,
            "options": [option.as_dict() for option in self.options],
            "hint_es": self.hint_es,
            "hint_en": self.hint_en,
        }


@dataclass(frozen=True, slots=True)
class PrefillForm:
    """A complete, editable draft: every field the submission will write, and its values.

    `facts` is what the *validation* answered — the working days a leave costs, the
    remaining balance, the week's window — and it is read-only by construction: it is not
    a list of `PrefillField`s, so no client can edit it into the submission. It is kept
    because the reason a draft was accepted is worth showing beside the form ("this costs
    three working days, you have eleven left"), and because it is the same material the
    refusal would have named had the draft been refused.
    """

    tool: str
    entity: DraftEntity
    title_key: str
    title_es: str
    title_en: str
    submit_path: str
    fields: tuple[PrefillField, ...]
    facts: Mapping[str, Any] = field(default_factory=dict)

    @property
    def field_names(self) -> tuple[str, ...]:
        """The submission's field names, in the order the form presents them."""
        return tuple(item.name for item in self.fields)

    def field(self, name: str) -> PrefillField | None:
        """One field by the submission's own name, or nothing."""
        return next((item for item in self.fields if item.name == name), None)

    def as_dict(self) -> dict[str, Any]:
        """The form as `agent_actions.produced_prefill_form` stores it."""
        return {
            "tool": self.tool,
            "entity": str(self.entity),
            "title_key": self.title_key,
            "title_es": self.title_es,
            "title_en": self.title_en,
            "submit_path": self.submit_path,
            "fields": [item.as_dict() for item in self.fields],
            "facts": _native(self.facts),
        }

    @classmethod
    def from_stored(cls, raw: Mapping[str, Any] | None) -> "PrefillForm | None":
        """Read a stored form back, or `None` when the column is empty.

        The JSONB is re-validated through this class rather than handed to a client as it
        was written: the column is the record and this is the contract, which is the same
        reason `app/api/v1/answer.py` re-validates a stored citation. A `kind` the enum no
        longer has would otherwise reach a browser as an input nobody can draw.
        """
        if not raw:
            return None
        return cls(
            tool=str(raw["tool"]),
            entity=DraftEntity(raw["entity"]),
            title_key=str(raw["title_key"]),
            title_es=str(raw["title_es"]),
            title_en=str(raw["title_en"]),
            submit_path=str(raw["submit_path"]),
            fields=tuple(_field(item) for item in raw.get("fields", ())),
            facts=dict(raw.get("facts") or {}),
        )


@dataclass(frozen=True, slots=True)
class AgentAction:
    """One row of §3.6's `agent_actions`: what the assistant proposed, and what became of it.

    Every column of the design's table is here. Ticket 40 writes the row and reads it back;
    `confirmed_at`, `resulting_entity_type` and `resulting_entity_id` are written by ticket
    41 when somebody confirms or rejects it, and `thread_id` is the LangGraph thread — for
    this system the conversation — so a paused run and its draft can be found from either
    side.

    `expired` is the row's *own* answer about the clock, computed from the two instants the
    database produced. Nothing here reads the process clock: a service that compared
    `datetime.now()` would disagree with `expires_at` on a container whose clock is a
    second off, and the disagreement would be a draft confirmed after it lapsed.
    """

    id: UUID
    conversation_id: UUID
    user_id: UUID
    tool_name: str
    status: DraftStatus
    created_at: datetime
    expires_at: datetime
    tool_input: Mapping[str, Any] = field(default_factory=dict)
    tool_output: Mapping[str, Any] = field(default_factory=dict)
    form: PrefillForm | None = None
    thread_id: str | None = None
    confirmed_at: datetime | None = None
    resulting_entity_type: str | None = None
    resulting_entity_id: UUID | None = None

    def effective_status(self, *, expired: bool) -> DraftStatus:
        """The status a reader should act on: `expired` once the row's own clock says so.

        `expired` is passed in rather than computed here because **the database decides**:
        `repository.py` compares `expires_at` with `now()` inside the statement that reads
        the row, so this method only applies the answer the comparison produced.
        """
        if self.status is DraftStatus.PROPOSED and expired:
            return DraftStatus.EXPIRED
        return self.status


def _native(values: Mapping[str, Any]) -> dict[str, Any]:
    """A mapping of JSON-native values, so the `JSONB` column can take it as it stands."""
    return {key: value for key, value in values.items()}


def _field(raw: Mapping[str, Any]) -> PrefillField:
    return PrefillField(
        name=str(raw["name"]),
        label_key=str(raw["label_key"]),
        kind=FieldKind(raw["kind"]),
        label_es=str(raw["label_es"]),
        label_en=str(raw["label_en"]),
        value=raw.get("value"),
        required=bool(raw.get("required", True)),
        options=tuple(
            FieldOption(
                value=str(item["value"]),
                label_es=str(item["label_es"]),
                label_en=str(item["label_en"]),
            )
            for item in raw.get("options", ())
        ),
        hint_es=raw.get("hint_es"),
        hint_en=raw.get("hint_en"),
    )


def fields(*items: PrefillField) -> tuple[PrefillField, ...]:
    """A field list, in the order the form presents it. A reader, not a wrapper."""
    return tuple(items)


__all__ = [
    "IDENTITY_FIELDS",
    "AgentAction",
    "DraftEntity",
    "DraftStatus",
    "FieldKind",
    "FieldOption",
    "PrefillField",
    "PrefillForm",
    "fields",
]
