"""Ticket 42's redaction: one export point, a whitelist, and no way for a passage out.

DESIGN §10.1 decided option (A) — LangSmith Cloud with a PII filter — so the failure this
module exists to prevent is *invisible leakage*: a trace that looks redacted because nobody
looked inside `counts`, and a passage that left inside a nested dict. The tests are written
against that failure rather than against its easy cousin:

* `test_forbidden_material_nested_at_every_depth_does_not_survive` plants conversation text,
  a prompt, a query, a document passage and a citation **at five depths**, asserts the input
  really carries them all, and then asserts on `json.dumps` of what the exporter would send —
  not on the dict's top level, which is the assertion that would pass while the passage
  travelled.
* `test_the_serialised_payload_names_no_forbidden_field` and
  `test_a_forbidden_field_offered_to_the_state_is_an_error_rather_than_a_redaction` cover the
  two ways the protective layer could be removed: a field smuggled in, and the whitelist
  itself weakened.
* `test_only_the_observability_module_names_a_tracing_client` is the structural half of "one
  export point": an `ast` walk over every module under `app/`, in the shape
  `test_architecture_constraints.py` uses for constraint A.

Every test names the checklist line it pins.
"""

import ast
import json
from pathlib import Path

import pytest

from app.ai.observability import (
    ALLOWED_TRACE_FIELDS,
    EXPORTER,
    FORBIDDEN,
    TRACING_CLIENT_PACKAGES,
    Delivered,
    ForbiddenTraceField,
    TraceExporter,
    TraceExportRefused,
    TraceTooLarge,
    build_payload,
    violations_in,
)
from app.ai.observability.whitelist import (
    DECISION_VALUES,
    TOOL_OUTCOME_VALUES,
    TRACE_FIELDS,
)

API_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = API_ROOT / "app"

#: The one module allowed to name a tracing client. Everything else must go through it.
EXPORT_MODULE = "app.ai.observability.exporter"

#: **The smuggled material.** A conversation body, a prompt, a query, a document passage and
#: a citation — the five §10.1 exists to keep in the installation — written as the strings a
#: test can search the serialised payload for. They are deliberately *sentences*: a test that
#: searched for a short token would pass on a payload that carried the passage minus the word.
CONVERSATION = "I have a medical appointment on the 14th and need the morning off"
PROMPT = "You are the company assistant. Answer only from the passages below."
QUERY = "¿Cuántos días de permiso por matrimonio corresponden?"
PASSAGE = "Los empleados tendrán derecho a quince días naturales en caso de matrimonio."
CITATION = "Politica de vacaciones y permisos p.3"


def _record(**overrides: object) -> dict:
    """A node record shaped exactly as `records.NodeRecord.as_dict()` produces one."""
    record: dict = {
        "node_name": "answer_policy",
        "tool_name": None,
        "decision": None,
        "input_keys": ["question", "conversation_id"],
        "output_keys": ["answer", "conversation_id", "is_refusal"],
        "counts": {"deltas": 4, "citations": 2},
        "latency_ms": 812,
        "is_refusal": False,
        "error_type": None,
    }
    record.update(overrides)
    return record


def _state(**overrides: object) -> dict:
    """A run state as the graph holds one: the records, the question, the answer, the tool."""
    state: dict = {
        "question": QUERY,
        "records": [_record()],
        "is_refusal": False,
        "answer": {
            "content": CONVERSATION,
            "citations": [CITATION],
            "token_in": 640,
            "token_out": 96,
        },
    }
    state.update(overrides)
    return state


# --- the whitelist itself ----------------------------------------------------


def test_the_whitelist_is_the_one_design_10_1_names() -> None:
    """**The first checklist line.** §10.1's allowed fields, asserted by name.

    A literal list rather than `== ALLOWED_TRACE_FIELDS`, so that *adding* a field is a
    decision somebody makes in this test as well as in the module — which is the property a
    blacklist cannot have: a new field must be admitted here deliberately.
    """
    assert ALLOWED_TRACE_FIELDS == frozenset(
        {
            "tool_name",
            "node_name",
            "latency_ms",
            "token_in",
            "token_out",
            "error_type",
            "provider_used",
            "model_used",
            "retrieval_hit_count",
            "is_refusal",
            # The record's own shape, which §10.1's list summarises as 「节点流转」:
            "decision",
            "input_keys",
            "output_keys",
            "counts",
        }
    )


def test_every_forbidden_field_design_10_1_names_is_forbidden_here() -> None:
    """**The second checklist line.** The nine, by name, plus this repository's spelling.

    `工具入参` and `工具出参` are the state keys `tool_arguments` and `tool_result` here, and a
    tripwire that watched only the design's English spelling would miss them.
    """
    required = {
        "content",
        "messages",
        "prompt",
        "completion",
        "query",
        "chunk_text",
        "citations",
        "tool_input",
        "tool_output",
    }
    assert required <= FORBIDDEN, f"not watched: {sorted(required - FORBIDDEN)}"
    assert {"tool_arguments", "tool_result"} <= FORBIDDEN
    # §10.1's two lists are disjoint, and every exportable field is a key of the table that
    # carries its predicate: there is no third category a value could fall into.
    assert not (ALLOWED_TRACE_FIELDS & FORBIDDEN)
    assert ALLOWED_TRACE_FIELDS == frozenset(TRACE_FIELDS)


def test_the_value_catalogues_agree_with_the_enums_they_mirror() -> None:
    """The two closed vocabularies are the graph's and the registry's, not a second opinion.

    They are literals in `whitelist.py` because the observability layer must be importable
    without the graph it observes; this is the test that keeps the copy honest.
    """
    from app.ai.agents.intents import Intent
    from app.ai.tools.models import ToolOutcome

    assert DECISION_VALUES == {str(item.value) for item in Intent}
    assert TOOL_OUTCOME_VALUES == {str(item.value) for item in ToolOutcome}


def test_a_record_is_projected_down_to_the_whitelist() -> None:
    """What *does* travel: the operational facts, field by field, with nothing else."""
    projected = build_payload(_state(), run_id="run-1").as_dict()["records"][0]
    assert projected == {
        "node_name": "answer_policy",
        "input_keys": ["question", "conversation_id"],
        "output_keys": ["answer", "conversation_id", "is_refusal"],
        "counts": {"deltas": 4, "citations": 2},
        "latency_ms": 812,
        "is_refusal": False,
    }
    assert set(projected) <= ALLOWED_TRACE_FIELDS


# --- the failure this ticket exists for --------------------------------------


def _smuggled_state() -> dict:
    """A state carrying §10.1's material at five depths, for the test below.

    The depths are the point, and each is a different way a naive filter loses:

    1. **top level** — the question itself, and an `answer` mapping whose `content` is a
       completion, and a `citations` list;
    2. **inside a record's `counts`** — a passage as a *count*, under a key no whitelist
       admits because the value is text;
    3. **inside `input_keys`** — a prompt as an entry of a list whose field *is* whitelisted;
    4. **inside `output_keys`** — a citation as an entry of a list that must hold identifiers;
    5. **inside `model_used`** — a passage smuggled under a whitelisted field that *would*
       have accepted a label, and a query inside a second record's `counts`.

    Depths 2-5 are the ones a whitelist has to survive **silently**: the field name is
    allowed, so nothing about the key looks wrong and only the value's *shape* is. Every one
    of them is dropped by the projection; the test asserts the **serialised** payload contains
    none of the strings, which is the assertion that fails if the projection ever admits one.

    A record carrying a forbidden *key* — `tool_result`, `content` — is a different case and
    a different test: that one is a bug in whoever built the record, and `reject_forbidden`
    raises rather than redacting.
    """
    return {
        "question": QUERY,
        "is_refusal": False,
        "answer": {
            "content": CONVERSATION,
            "token_in": 11,
            "token_out": 22,
            "provider": "openai",
            "model": "gpt-4o",
        },
        "citations": [{"snippet": CITATION}],
        "records": [
            _record(counts={"deltas": 4, "passage": PASSAGE}),
            _record(
                node_name="read_only_tools",
                input_keys=["question", PROMPT],
                output_keys=["tool_result", CITATION],
                counts={"leaked_query": QUERY},
                model_used=PASSAGE,
                token_in=640,
                token_out=96,
                provider_used="openai",
            ),
        ],
    }


def test_forbidden_material_nested_at_every_depth_does_not_survive() -> None:
    """**The checklist line about nested structures.** The whole reason this ticket exists.

    The assertions are on the serialised payload — what a backend would store — and the test
    first proves the input really carries every string, so it cannot pass because the fixture
    stopped smuggling anything.

    The mutation this pins: replace `project_record`'s projection with
    `{k: v for k, v in record.items() if k not in FORBIDDEN}` and the passage inside `counts`
    travels; drop the projection entirely and every one of the five travels.
    """
    state = _smuggled_state()
    # The control: all five really are in the input, at their five depths. Serialised the
    # same way the payload is (`ensure_ascii=False`), so an accented question is not
    # escaped into a form the search below would miss.
    serialised_state = json.dumps(state, ensure_ascii=False, default=str)
    for planted in (CONVERSATION, PROMPT, QUERY, PASSAGE, CITATION):
        assert planted in serialised_state, (
            f"the fixture no longer carries {planted!r}, so the assertions below would pass "
            "against a state that never smuggled anything"
        )

    payload = build_payload(state, run_id="run-nested")
    serialised = payload.serialised()

    for forbidden in (CONVERSATION, PROMPT, QUERY, PASSAGE, CITATION):
        assert forbidden not in serialised, (
            f"{forbidden!r} left the process in the trace payload: {serialised}"
        )
    # And the field *names*, which is the other half: a payload that carried a key called
    # `tool_result` would be a redaction that renamed rather than removed.
    assert violations_in(payload.as_dict()) == frozenset()
    for name in ("tool_result", "tool_arguments", "chunk_text", "citations", "content"):
        assert f'"{name}"' not in serialised, (
            f"the serialised payload names {name!r}: {serialised}"
        )
    # What did survive is the operational record and nothing else.
    assert "answer_policy" in serialised and "812" in serialised


def test_an_entire_record_cannot_be_smuggled_under_an_undocumented_key() -> None:
    """A record's *unknown* keys are dropped too, not only the named forbidden ones.

    This is the whitelist's real claim: `tool_result` is dropped because it is not allowed,
    and `a_key_nobody_listed` is dropped for exactly the same reason. A blacklist would let
    the second one through, which is the semantic difference between the two designs.
    """
    state = _state(
        records=[_record(a_key_nobody_listed={"passage": PASSAGE}, transcript=CONVERSATION)]
    )
    serialised = build_payload(state, run_id="run-unknown").serialised()
    assert PASSAGE not in serialised
    assert CONVERSATION not in serialised
    assert "a_key_nobody_listed" not in serialised
    assert "transcript" not in serialised


def test_the_serialised_payload_names_no_forbidden_field() -> None:
    """The `FORBIDDEN` tripwire, over a payload carrying every whitelisted field at once.

    A positive control for `violations_in`: it is called on a payload that *does* carry a
    forbidden name and must say so, because a scanner that always returned nothing would make
    the exporter's post-condition vacuous.
    """
    full = _record(
        tool_name="get_my_leave_balance",
        decision="read_only_query",
        error_type="AnswerModelUnavailable",
        counts={"result_fields": 3},
    )
    serialised = build_payload(_state(records=[full]), run_id="run-full").serialised()
    assert violations_in(json.loads(serialised)) == frozenset()

    planted = {"records": [{"node_name": "classify", "content": CONVERSATION}]}
    assert violations_in(planted) == frozenset({"content"})
    nested = {"records": [{"counts": {"tool_output": 1}}]}
    assert violations_in(nested) == frozenset({"tool_output"})


def test_a_forbidden_field_on_a_record_is_an_error_rather_than_a_redaction() -> None:
    """A **record** built from forbidden material is refused loudly. The tripwire's level.

    The level matters, and this test states it: the *state* is allowed to hold a question, a
    tool result and a draft — that is the installation's own Postgres — while the **record**
    is the thing shaped like a trace, so a record carrying `content` or `tool_result` means a
    layer above this one is copying material into something it intends to export. Raising
    rather than dropping is the decision: a redaction that silently absorbed it would leave
    the bug alive until a payload that does not pass through here carried it out.

    The mutation this pins: delete `reject_forbidden` and the record-level case would project
    happily (the projection would still drop the field, so this is the *tripwire* being
    removed rather than the filter — which is exactly why both exist).
    """
    with pytest.raises(ForbiddenTraceField) as at_record:
        build_payload(_state(records=[_record(content=CONVERSATION)]), run_id="run-1")
    assert "record.content" in str(at_record.value)

    with pytest.raises(ForbiddenTraceField):
        build_payload(_state(records=[_record(tool_result={"rows": []})]), run_id="run-1")

    # Through the export point as well, which is what actually guards delivery.
    sink = Delivered()
    with pytest.raises(ForbiddenTraceField):
        TraceExporter(sink=sink).export(
            _state(records=[_record(prompt=PROMPT)]), run_id="run-1"
        )
    assert sink.payloads == [], "a refused trace must not have been delivered"


def test_a_state_full_of_conversation_material_still_exports_a_clean_payload() -> None:
    """The other side of the tripwire's level: a *state* may hold anything, nothing travels.

    The same state as the nesting test, exported without the projection being bypassed:
    building succeeds and the payload is clean. A filter that refused every state containing
    a question would be a filter nobody could run, and a guard nobody can run is a guard
    somebody deletes.
    """
    payload = build_payload(_smuggled_state(), run_id="run-state")
    serialised = payload.serialised()
    assert QUERY not in serialised
    assert CONVERSATION not in serialised
    assert CITATION not in serialised
    assert payload.as_dict()["records"], "the run's records did not travel at all"


def test_the_export_refuses_when_the_serialised_payload_carries_a_forbidden_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-condition, exercised where it lives: `TraceExporter.export`.

    Reached by replacing the projection with one that admits the whole record — the mutation
    the ticket names as "allow a forbidden field back in" — which is the only way a forbidden
    name can reach the serialised payload now that the projection builds a fresh dict. The
    assertion is that the exporter **refuses** rather than sends: a leak that is merely
    noticed is not a leak prevented.
    """
    sink = Delivered()
    exporter = TraceExporter(sink=sink)

    def leaky(record: dict) -> dict:
        return dict(record)

    monkeypatch.setattr("app.ai.observability.exporter.project_record", leaky)
    with pytest.raises(TraceExportRefused) as refused:
        exporter.export(
            _state(records=[_record(tool_output=PASSAGE, tool_result={"x": 1})]),
            run_id="run-leak",
        )
    named = {
        name
        for name in ("tool_output", "tool_result")
        if any(name in reason for reason in refused.value.reasons)
    }
    assert named, f"the refusal named neither forbidden field: {refused.value.reasons}"
    assert sink.payloads == [], "a refused trace must not have been delivered"


def test_a_payload_past_the_record_ceiling_is_refused() -> None:
    """A trace that is not a set of node records is refused rather than sent."""
    with pytest.raises(TraceTooLarge):
        build_payload(
            _state(records=[_record(node_name=f"node_{index}") for index in range(200)]),
            run_id="run-big",
        )


def test_a_payload_past_the_character_ceiling_is_refused() -> None:
    """The other ceiling, and it is the one that catches a single runaway value."""
    from app.ai.observability import TracePayload
    from app.ai.observability.whitelist import forbid_oversized

    wide = TracePayload(run_id="run-wide", records=({"model_used": "m" * 20_000},))
    with pytest.raises(TraceTooLarge):
        forbid_oversized(wide.as_dict())


# --- the single export point -------------------------------------------------


def test_the_export_point_delivers_what_was_projected_and_nothing_else() -> None:
    """**The first checklist line's other half.** One call, and the sink sees the payload.

    Asserted through a real sink rather than a mock: `Delivered` keeps the `TracePayload` the
    exporter built, so `serialised()` here is the bytes a backend would store.
    """
    sink = Delivered()
    exporter = TraceExporter(sink=sink)
    payload = exporter.export(_state(), run_id="run-delivered")

    assert payload is not None
    assert exporter.delivered == 1
    assert len(sink.payloads) == 1
    assert sink.payloads[0].run_id == "run-delivered"
    assert sink.payloads[0].schema_version == 1
    assert QUERY not in sink.payloads[0].serialised()

    # No sink: the filter still runs and refuses, and nothing is delivered. That is what a
    # deployment with no backend configured gets, and it is why `export` returns the payload.
    bare = TraceExporter()
    filtered = bare.export(_state(), run_id="run-filtered")
    assert filtered is not None and bare.delivered == 0
    with pytest.raises(ForbiddenTraceField):
        bare.export(_state(records=[_record(tool_result={})]), run_id="run-filtered")


def test_the_module_level_exporter_is_a_single_shared_instance() -> None:
    """`EXPORTER` is one object, so "did anything export" is answerable from anywhere."""
    assert isinstance(EXPORTER, TraceExporter)
    assert isinstance(EXPORTER.delivered, int)


def _imported_names(path: Path) -> set[str]:
    """Every dotted module name a file imports, absolute and relative resolved loosely.

    Loose on purpose: this walk is looking for a *substring* of a package name, so a
    relative import that fails to resolve exactly still has to be caught. A walker that
    under-reported would pass while a module reached `langsmith`.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover - a broken file fails its own tests
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


def test_only_the_observability_module_names_a_tracing_client() -> None:
    """**There is exactly one export point, and this is the structural proof of it.**

    A tracing backend arrives as an import. If any module outside
    `app/ai/observability/` names `langsmith`, `langfuse`, `opentelemetry` or LangChain's
    callbacks, then something else can send a payload out — and that path is not the one the
    whitelist guards. The walk is asserted non-empty first, because a walker that found no
    files would pass vacuously.
    """
    files = sorted(APP_ROOT.rglob("*.py"))
    assert len(files) > 100, f"the walk found {len(files)} modules under {APP_ROOT}"

    offenders: dict[str, set[str]] = {}
    for path in files:
        if "observability" in path.parts:
            continue
        reached = {
            name
            for name in _imported_names(path)
            if any(package in name for package in TRACING_CLIENT_PACKAGES)
        }
        if reached:
            offenders[str(path.relative_to(APP_ROOT))] = reached
    assert not offenders, (
        "a module outside app/ai/observability reaches a tracing client, so the whitelist's "
        f"export point is not the only way out: {offenders}"
    )


def test_the_walker_would_catch_a_second_export_point(tmp_path: Path) -> None:
    """The positive control for the walk above, so its pass is not vacuous."""
    planted = tmp_path / "leaky.py"
    planted.write_text("from langsmith import Client\n", encoding="utf-8")

    def reaches(path: Path) -> set[str]:
        return {
            name
            for name in _imported_names(path)
            if any(package in name for package in TRACING_CLIENT_PACKAGES)
        }

    assert reaches(planted) == {"langsmith", "langsmith.Client"}

    nested = tmp_path / "also_leaky.py"
    nested.write_text("import opentelemetry.trace\n", encoding="utf-8")
    assert reaches(nested), "a bare `import opentelemetry.trace` was not caught"

    callbacks = tmp_path / "third.py"
    callbacks.write_text("from langchain.callbacks import tracing_v2_enabled\n", encoding="utf-8")
    assert reaches(callbacks), "a LangChain callback import was not caught"
