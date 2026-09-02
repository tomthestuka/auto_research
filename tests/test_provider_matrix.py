"""Deterministic twin of ``tests/test_live_schema_matrix.py`` — the no-token half of "will this
adapter work".

The live matrix answers what a vendor *honours*, which costs tokens and needs credentials. Two
classes of the same question cost nothing and are checked here instead:

* **Coverage.** A vendor wired into ``CHAT_PROVIDERS`` can hold a graph role, so a vendor absent
  from the live matrix is an adapter nobody measures. That is not hypothetical: ``google`` was
  wired, backed ``librarian_io``'s third member, and appeared in neither the schema matrix nor the
  form probe until this test was written (2026-09-02).
* **Shape.** Whether a schema we *send* satisfies the constraints strict mode imposes is a
  property of the literal, not of the vendor, so it is decidable offline. Strict mode is what
  turns a declared schema into an enforced one, and a schema outside the compilable subset earns a
  400 with details — cheap to prevent, expensive to discover in a $25 trial.

Pairing them is the point: the deterministic half fails in a second on a structural mistake, and
the live half is then spent only on what genuinely needs a vendor's answer.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from auto_research.agents.react import (
    ANOTHER_ROUND_TOOL,
    AUTHOR_CHAIN_TOOL,
    HOLD_AUTHORING_TOOL,
    LEAD_FORM_TOOLS,
    SUBMIT_ADJUDICATION_TOOL,
    TERMINAL_REPORT_TOOL,
    author_chain_tool_def,
    hold_authoring_tool_def,
    request_another_round_tool_def,
    submit_adjudication_tool_def,
    submit_terminal_report_tool_def,
)
from auto_research.models.types import CHAT_PROVIDERS

LIVE_MATRIX = Path(__file__).parent / "test_live_schema_matrix.py"
LIVE_FORMS = Path(__file__).parent / "test_live_form_shapes.py"

FORM_DEFS = {
    AUTHOR_CHAIN_TOOL: author_chain_tool_def,
    HOLD_AUTHORING_TOOL: hold_authoring_tool_def,
    ANOTHER_ROUND_TOOL: request_another_round_tool_def,
    SUBMIT_ADJUDICATION_TOOL: submit_adjudication_tool_def,
    TERMINAL_REPORT_TOOL: submit_terminal_report_tool_def,
}

# Keywords strict mode compiles into a grammar, per each provider's own published subset.
# Anything outside this set is either stripped (so it silently guarantees nothing) or 400s.
_STRICT_SAFE_KEYWORDS = frozenset(
    {
        "type", "properties", "required", "additionalProperties", "items", "enum", "const",
        "anyOf", "allOf", "$ref", "$defs", "definitions", "default", "description", "format",
        "minItems", "title",
    }
)

# Documented as unsupported by at least one provider we send to, and therefore never a guarantee.
# `minLength`/`maxLength`/`minimum`/`maximum` are on Anthropic's Not-Supported list outright and
# the SDKs strip them; a content rule like "non-empty string" has to be enforced by Pydantic in
# the handler and *stated in the description*, which is what the description guard covers.
_NEVER_ENFORCED = frozenset({"minLength", "maxLength", "minimum", "maximum", "multipleOf",
                            "maxItems", "uniqueItems", "pattern", "minProperties",
                            "maxProperties"})


def _providers_named_in(path: Path) -> set[str]:
    """Vendor names a live test parametrizes over, read from its source.

    Read from the source rather than imported because importing a live module pulls in its
    ``skipif`` and its credentials; the question here is only which names it lists.
    """
    source = path.read_text(encoding="utf-8")
    return set(re.findall(r'\(\s*"([a-z_]+)"\s*,', source)) & CHAT_PROVIDERS


def _walk(schema: dict, trail: str = "root"):
    """Every subschema in ``schema``, with a readable path to it."""
    if not isinstance(schema, dict):
        return
    yield trail, schema
    for name, sub in (schema.get("properties") or {}).items():
        yield from _walk(sub, f"{trail}.{name}")
    if isinstance(schema.get("items"), dict):
        yield from _walk(schema["items"], f"{trail}[]")
    for key in ("anyOf", "allOf"):
        for i, branch in enumerate(schema.get(key) or []):
            yield from _walk(branch, f"{trail}.{key}[{i}]")


# ── coverage: a wired vendor is a measured vendor ────────────────────────────


def test_every_wired_chat_vendor_appears_in_the_live_schema_matrix():
    """A vendor in ``CHAT_PROVIDERS`` can hold a graph role, so an unmeasured one is a blind spot.

    This test exists because ``google`` was exactly that: wired, holding ``librarian_io``'s third
    member, and in neither live matrix. Whether a vendor's cells *pass* is the live half's
    business — a documented limitation is a legitimate answer. Being absent is not an answer.
    """
    missing = CHAT_PROVIDERS - _providers_named_in(LIVE_MATRIX)
    assert not missing, (
        f"wired chat vendors absent from the live schema matrix: {sorted(missing)}. A vendor that "
        "can hold a graph role must have its adapter measured, even if the measurement records a "
        "limitation rather than a pass."
    )


def test_every_wired_chat_vendor_appears_in_the_live_form_probe():
    """Same argument for the form tools: a graph role that files a form needs its vendor measured."""
    missing = CHAT_PROVIDERS - _providers_named_in(LIVE_FORMS)
    assert not missing, (
        f"wired chat vendors absent from the live form probe: {sorted(missing)}"
    )


# ── shape: the forms satisfy strict mode by construction ─────────────────────


@pytest.mark.parametrize("name", sorted(FORM_DEFS), ids=sorted(FORM_DEFS))
def test_each_form_asks_for_enforcement(name):
    """``strict`` is opt-in per tool, and a form whose point is a mandatory field must opt in.

    Without it the provider documents that the model "might omit required fields" — which a live
    lead did, filing an empty chain through a `minItems: 1` that no provider was enforcing because
    nobody had asked ($19.43 to learn, 2026-09-01/02).
    """
    assert FORM_DEFS[name]()["strict"] is True


@pytest.mark.parametrize("name", sorted(FORM_DEFS), ids=sorted(FORM_DEFS))
def test_each_form_schema_is_inside_the_strict_compilable_subset(name):
    """Every object closed, every property required, no keyword that would strip or 400.

    Strict mode's two structural demands apply to every object however deep, and optionality is
    expressed by nullability rather than by omission from ``required``.
    """
    schema = FORM_DEFS[name]()["input_schema"]
    problems: list[str] = []
    for trail, sub in _walk(schema):
        if "properties" in sub:
            if sub.get("additionalProperties") is not False:
                problems.append(f"{trail}: additionalProperties must be False")
            if set(sub.get("required") or []) != set(sub["properties"]):
                problems.append(f"{trail}: every property must be listed in required")
        banned = _NEVER_ENFORCED & set(sub)
        if banned:
            problems.append(f"{trail}: {sorted(banned)} is stripped or rejected, never enforced")
        unknown = set(sub) - _STRICT_SAFE_KEYWORDS - _NEVER_ENFORCED
        if unknown:
            problems.append(f"{trail}: unrecognised keyword(s) {sorted(unknown)}")
    assert not problems, "\n".join(problems)


def test_minItems_is_only_ever_zero_or_one():
    """Anthropic supports ``minItems`` of 0 or 1 and nothing beyond; a larger value is rejected."""
    for name, factory in FORM_DEFS.items():
        for trail, sub in _walk(factory()["input_schema"]):
            if "minItems" in sub:
                assert sub["minItems"] in (0, 1), f"{name} {trail}: minItems={sub['minItems']}"


def test_a_nullable_field_is_an_anyOf_with_a_literal_type_on_every_branch():
    """The portable spelling of optional-under-strict, and each constraint is a measured one.

    ``anyOf`` over a type array because it is on Anthropic's supported list; a literal ``type`` on
    every branch because DeepSeek's validator checks that before following anything; and this
    shape because Qwen takes ``anyOf`` 5/5 where it refuses other positions.
    """
    for name, factory in FORM_DEFS.items():
        for trail, sub in _walk(factory()["input_schema"]):
            branches = sub.get("anyOf")
            if not branches:
                continue
            assert {"type": "null"} in branches, f"{name} {trail}: anyOf with no null branch"
            for i, branch in enumerate(branches):
                assert "type" in branch, f"{name} {trail}.anyOf[{i}] has no literal type"
