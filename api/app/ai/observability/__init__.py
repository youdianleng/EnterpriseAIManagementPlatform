"""The trace-redaction edge: one export point, and a whitelist of what may leave.

DESIGN §10.1 decided option (A) — LangSmith Cloud with a PII filter — and this package is
that filter, in the two halves the decision names:

* `whitelist.py` is *what may leave*. The list is of the **allowed** fields, copied from
  §10.1's `ALLOWED_TRACE_FIELDS`, and each one carries a predicate for its value: a node name
  is an identifier, a decision is a key of the intent catalogue, an error type is an exception
  class's name, a count is a bounded integer. §10.1's nine forbidden names are listed too, but
  as a tripwire that raises rather than as the filter — a blacklist is defeated by the next
  field somebody adds, which is the whole reason the design asks for the other direction.
* `exporter.py` is *where it leaves*. `TraceExporter.export` is the single function in this
  repository that hands a payload to a backend, and it projects, checks the serialised result,
  and only then delivers.

**This package's interface is two verbs and no predicates.** `build_payload` produces the
whitelisted payload for one run, and `TraceExporter.export` produces *and* delivers it. The
per-field predicates in `whitelist.py` are deliberately **not** re-exported: a caller that
could ask "does this field accept this value" could assemble a payload field by field and
avoid the post-condition, and the property this package exists to have is that the only way
out is the whole chain. Import `app.ai.observability.whitelist` directly if you are writing a
test about the list itself.

**The direction of the dependency is `ai → domain` here too** (constraint A). This package
imports nothing from `app.domain` at all, which is why `DECISION_VALUES` and
`TOOL_OUTCOME_VALUES` are literals with a test asserting they agree with the enums rather
than imports: an observability layer that could not be imported without the graph would be a
layer the graph's own tests could not use in isolation.

**What is not here yet, stated rather than implied.** The LangSmith client itself: §10.1
chooses a *filter* and this ticket's checklist is the filter and the whitelist, so the sink a
deployment supplies is `TRACE_SINK`'s second adapter. A `TraceSink` implementation for
LangSmith Cloud is a small class over `langsmith.Client.create_run`, and it is deliberately
not written behind an unset API key this repository's tests may not use (the verification
standard forbids a test that needs a real key).
"""

from app.ai.observability.exporter import (
    EXPORTER,
    RECORDS_KEY,
    TRACE_SCHEMA_VERSION,
    TRACING_CLIENT_PACKAGES,
    Delivered,
    TraceExporter,
    TraceExportRefused,
    TracePayload,
    TraceSink,
    build_payload,
)
from app.ai.observability.whitelist import (
    ALLOWED_TRACE_FIELDS,
    FORBIDDEN,
    ForbiddenTraceField,
    TraceTooLarge,
    violations_in,
)

__all__ = [
    "ALLOWED_TRACE_FIELDS",
    "EXPORTER",
    "FORBIDDEN",
    "RECORDS_KEY",
    "TRACE_SCHEMA_VERSION",
    "TRACING_CLIENT_PACKAGES",
    "Delivered",
    "ForbiddenTraceField",
    "TraceExportRefused",
    "TraceExporter",
    "TracePayload",
    "TraceSink",
    "TraceTooLarge",
    "build_payload",
    "violations_in",
]
