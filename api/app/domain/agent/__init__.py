"""The agent's own tables: what the assistant proposed, and what became of it.

`app/domain/agent/` is DESIGN §3.6's `agent_actions` (§6.3's human-review point) as a
domain module, and it sits here rather than under `app/ai/` for the reason the whole
constraint is about: **the draft tools must not be able to write, and the platform must.**
A tool returns a `PrefillForm` (`app/ai/tools/draft.py`); this package is what records it —
a repository, a service and the row's own shapes. The dependency runs `ai → domain`, which
is constraint A's direction and the only one this tree has.

`models.PrefillForm` is also the shape the drafts travel in, which is why the tools import
it from here instead of defining a second one: the form the assistant proposes, the form the
row stores, and the form the interface draws are one object with one definition.
"""

from app.domain.agent.models import (
    IDENTITY_FIELDS,
    AgentAction,
    DraftEntity,
    DraftStatus,
    FieldKind,
    FieldOption,
    PrefillField,
    PrefillForm,
)
from app.domain.agent.repository import PostgresAgentActionRepository, StoredAgentAction
from app.domain.agent.service import AgentActionService, RecordedDraft, service_for

__all__ = [
    "IDENTITY_FIELDS",
    "AgentAction",
    "AgentActionService",
    "DraftEntity",
    "DraftStatus",
    "FieldKind",
    "FieldOption",
    "PostgresAgentActionRepository",
    "PrefillField",
    "PrefillForm",
    "RecordedDraft",
    "StoredAgentAction",
    "service_for",
]
