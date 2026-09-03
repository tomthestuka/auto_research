from pathlib import Path

import pytest

from auto_research.agents import FORBIDDEN_RUNTIME_REFERENCES, PROMPT_FILENAMES, load_prompt


@pytest.mark.parametrize("role", PROMPT_FILENAMES)
def test_runtime_prompt_loads(role: str) -> None:
    content = load_prompt(role)  # type: ignore[arg-type]

    assert content
    assert "Responsibilities:" in content


@pytest.mark.parametrize("role", PROMPT_FILENAMES)
def test_runtime_prompt_has_no_governance_references(role: str) -> None:
    content = load_prompt(role)  # type: ignore[arg-type]

    for forbidden in FORBIDDEN_RUNTIME_REFERENCES:
        assert forbidden not in content


def test_loader_rejects_governance_references(tmp_path: Path) -> None:
    (tmp_path / "reviewer.md").write_text("Do not load DOCS.md", encoding="utf-8")

    with pytest.raises(ValueError, match="forbidden file"):
        load_prompt("reviewer", root=tmp_path)


# --------------------------------------------------------- HITL tools are offered conditionally

# The tools that exist only when a human is attending: every role's provider appends them to its
# tool definitions solely when a `human_handler` is wired (`react.py` ReadOnly/Sandbox providers,
# and `roles.py` for the lead's authoring loop). A prompt that names one unconditionally instructs
# an unattended role to use a tool it was never given — which is what both published benchmark
# batches ran with.
_CONDITIONAL_TOOLS = ("ask_human", "plan_and_debate")

# The phrasings that make the instruction conditional. Positive-action form throughout (CLAUDE.md
# §3), so this is "when it is offered, do X; otherwise do Y" rather than a prohibition.
_CONDITIONAL_MARKERS = ("is offered", "are offered", "unattended", "when it is absent",
                        "no such tool", "when they are offered")


@pytest.mark.parametrize("role", PROMPT_FILENAMES)
def test_a_prompt_naming_a_human_only_tool_says_it_may_be_absent(role: str) -> None:
    content = load_prompt(role)  # type: ignore[arg-type]
    if not any(tool in content for tool in _CONDITIONAL_TOOLS):
        pytest.skip(f"{role} names no human-only tool")
    assert any(marker in content for marker in _CONDITIONAL_MARKERS), (
        f"{role}.md names {[t for t in _CONDITIONAL_TOOLS if t in content]} but never says the "
        "tool may be absent, so an unattended run is told to use a tool it was not given"
    )


def test_every_role_that_can_ask_a_human_is_covered_by_the_check() -> None:
    """Guards the guard: if a new prompt starts naming a human-only tool, the check above must
    actually reach it rather than silently skipping."""
    named = {
        role for role in PROMPT_FILENAMES
        if any(tool in load_prompt(role) for tool in _CONDITIONAL_TOOLS)  # type: ignore[arg-type]
    }
    assert named == {"analyst_worker", "analyst_lead", "reviewer", "coder"}, (
        f"the set of prompts naming a human-only tool changed: {sorted(named)}"
    )


def test_model_facing_text_names_no_vendor_or_model_family():
    """No prompt or tool description tells one seat what model fills another.

    Two static strings did: `drive_coder` said the coder is "a Sonnet Claude-Code session" (false
    for a campaign whose coder is another vendor) and the review tools said "the independent GPT
    co-lead". Constant across arms, so not arm-distinguishing — but a lead is told to write
    plan-mode-grade briefs *because* the coder is weak, and one believing it drives a stronger
    model may under-specify. Scoped to what a model reads (tool definitions, runtime prompts),
    not module source, whose build-time docstrings discuss vendor behaviour legitimately.
    """
    import json
    from pathlib import Path

    from auto_research.agents import react

    banned = ("sonnet", "opus", "haiku", "gpt-", "gpt co-lead", "deepseek", "qwen", "glm",
              "claude-code", "claude code session")
    surfaces: dict[str, str] = {}
    for name in dir(react):
        if not name.endswith("_tool_def"):
            continue
        try:
            surfaces[name] = json.dumps(getattr(react, name)())
        except TypeError:
            continue  # a builder that needs arguments is exercised by its own test
    assert surfaces, "no tool definitions were collected — the scan would be vacuous"
    for path in Path("prompts").glob("*.md"):
        if path.name != "DOCS.md":  # build-time documentation; §4 keeps it out of runtime context
            surfaces[path.name] = path.read_text(encoding="utf-8")
    for where, text in surfaces.items():
        low = text.lower()
        for token in banned:
            assert token not in low, f"{where} names a model family: {token!r}"
