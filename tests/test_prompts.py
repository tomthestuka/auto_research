from pathlib import Path

import pytest

from auto_research.agents import FORBIDDEN_RUNTIME_REFERENCES, PROMPT_FILENAMES, load_prompt


@pytest.mark.parametrize("role", PROMPT_FILENAMES)
def test_runtime_prompt_loads(role: str) -> None:
    content = load_prompt(role)  # type: ignore[arg-type]

    assert content
    # Every runtime prompt must state, up front, what the role is responsible for. Two accepted
    # forms since F8 (2026-09-09) sectioned the longest prompts: the legacy bare
    # `Responsibilities:` label, or a Markdown heading serving the same purpose. Asserting the
    # bare literal alone made a *structural improvement* fail — `analyst_worker.md` and
    # `analyst_lead.md` were 10k and 20k characters with zero headings and single lines of 1,319
    # and 2,144 characters, which is how a rule ended up buried mid-paragraph and unread.
    assert "Responsibilities:" in content or "\n## " in content, (
        f"{role}: state the role's responsibilities as a labelled block or a `## ` section"
    )


@pytest.mark.parametrize("role", PROMPT_FILENAMES)
def test_a_long_runtime_prompt_is_sectioned_and_line_bounded(role: str) -> None:
    """A prompt long enough to need navigation must have it (F8).

    Not cosmetic. `analyst_lead.md` carried "One link is one capability, one commit" as the third
    sentence of its single longest line — 2,144 characters, sharing that paragraph with the
    chain-planning procedure and the disposition decision — and two live runs violated the rule.
    Modern attention does not rescue a wall of text; sectioning is what makes a rule findable.
    """
    content = load_prompt(role)  # type: ignore[arg-type]

    # The LINE BOUND is the load-bearing half, and it is the one the evidence supports: the
    # measured failure was a rule buried mid-paragraph in a 2,144-character line, not a prompt
    # that was merely large. A model reads the whole thing linearly; what defeats it is a wall of
    # text, so this applies to every prompt regardless of size.
    longest = max((len(line) for line in content.splitlines()), default=0)
    assert longest <= 600, f"{role}: longest line is {longest} chars — break it up"

    # Sections are for navigation, so they are required only where there is enough to navigate.
    # The threshold is deliberately high: `analyst_colead.md` is 7.7k of well-wrapped short lines
    # and reads fine unsectioned, and inventing a lower bound to catch it would be a style rule
    # with no incident behind it.
    if len(content) >= 12000:
        assert content.count("\n## ") >= 4, f"{role}: {len(content)} chars needs sections"


@pytest.mark.parametrize("role", PROMPT_FILENAMES)
def test_runtime_prompt_has_no_governance_references(role: str) -> None:
    content = load_prompt(role)  # type: ignore[arg-type]

    for forbidden in FORBIDDEN_RUNTIME_REFERENCES:
        assert forbidden not in content


def test_loader_rejects_governance_references(tmp_path: Path) -> None:
    (tmp_path / "conventions.md").write_text("Team conventions.", encoding="utf-8")
    (tmp_path / "reviewer.md").write_text("Do not load DOCS.md", encoding="utf-8")

    with pytest.raises(ValueError, match="forbidden file"):
        load_prompt("reviewer", root=tmp_path)
    (tmp_path / "reviewer.md").write_text("The reviewer's job.", encoding="utf-8")
    (tmp_path / "conventions.md").write_text("See AGENTS.md", encoding="utf-8")
    with pytest.raises(ValueError, match="conventions.md references forbidden"):
        load_prompt("reviewer", root=tmp_path)


def test_a_unit_prompt_is_the_conventions_then_its_role_and_a_non_unit_prompt_is_its_own(
    tmp_path: Path,
) -> None:
    from auto_research.agents.prompts import UNIT_ROLES

    (tmp_path / "conventions.md").write_text("Team conventions.", encoding="utf-8")
    for role in PROMPT_FILENAMES:
        (tmp_path / PROMPT_FILENAMES[role]).write_text(f"The {role} job.", encoding="utf-8")
    assert UNIT_ROLES == {"reviewer", "librarian_io", "analyst_worker", "analyst_lead"}
    for role in PROMPT_FILENAMES:
        expected = (f"Team conventions.\n\nThe {role} job." if role in UNIT_ROLES else f"The {role} job.")
        assert load_prompt(role, root=tmp_path) == expected  # type: ignore[arg-type]


# --------------------------------------------------------- HITL tools are offered conditionally

# The address that exists only when a human is attending: `human` is among `message`'s addresses
# solely when the run has a human handler (E12, `hitl.router.SeatRouter.directory`). A prompt that
# names it unconditionally instructs an unattended role to text nobody — which is what both
# published benchmark batches ran with, when the address was a tool (`ask_human`).
_CONDITIONAL_TOOLS = ('`to: "human"`', "`message` to `human`", "`human` is among")

# The phrasings that make the instruction conditional. Positive-action form throughout (CLAUDE.md
# §3), so this is "when it is offered, do X; otherwise do Y" rather than a prohibition.
_CONDITIONAL_MARKERS = ("is among", "is not among", "unattended", "when it is absent",
                        "is reachable", "no human reachable")


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


#: The output rules before the three classes (B2, 2026-11-04): a file the tool printed or named was
#: receipted, and figures were re-homed into a shared folder. No prompt teaches them any more.
_REMOVED_OUTPUT_RULES = ("absolute** path in `stdout`", "absolute path in stdout", "absolute path in `stdout`",
                         "figures directory", "figures dir", "re-home", "auto-generate a timestamped path",
                         # synthetic data: a marked ObsID the tests build in their fixture obs-db (ruling)
                         "stored under the mission path", "by its marked ObsID", "referenced by a marked `synth",
                         "folder whose name marks it", "synth_flat_spectrum",
                         "generated beforehand in worker scratch")


@pytest.mark.parametrize("role", PROMPT_FILENAMES)
def test_no_prompt_teaches_the_removed_output_rules(role: str) -> None:
    content = " ".join(load_prompt(role).lower().split())  # type: ignore[arg-type]  # a rule may wrap
    for rule in _REMOVED_OUTPUT_RULES:
        assert rule.lower() not in content, f"{role}: still teaches {rule!r}"


def test_the_coder_is_taught_the_three_output_classes() -> None:
    """What the runtime mints and the gate checks (B1-B3) is what the coder reads (B4)."""
    from auto_research.provenance.capture import _MAX_PRODUCTS, PRODUCT_KEYS
    from auto_research.provenance.inputs import OUTPUT_SHAPE_KEY, OUTPUT_SHAPES

    coder = " ".join(load_prompt("coder").split())  # a phrase may wrap across lines
    for taught in ('`"output"`', f'`"{OUTPUT_SHAPE_KEY}"`', *(f'`"{shape}"`' for shape in OUTPUT_SHAPES),
                   '`"default"`', '"products": [{', *(f'"{key}": "<' for key in PRODUCT_KEYS),
                   f"at most {_MAX_PRODUCTS} products", "depends on a `parameter`-kind argument",
                   "outside the mission team's defaults", "recommended settings only", "`synth<YYMMDD>-<hex>`",
                   "obsdb_catalog.neutral.new",
                   "neutral.advance(level=", "obsdb_catalog.healthcheck", 'tmp_path / "<MISSION>"',
                   "in exactly one class", "tempfile.mkdtemp()", "(`unclassified`)",
                   'tmp_path / "out"', "in-place header keyword", "**Scratch**",
                   f'"kind": "output", "{OUTPUT_SHAPE_KEY}":'):
        assert taught in coder, f"coder.md no longer teaches {taught!r}"


@pytest.mark.parametrize("role", ["analyst_worker", "analyst_lead", "analyst_colead"])
def test_the_roles_around_the_coder_read_the_same_classes(role: str) -> None:
    content = " ".join(load_prompt(role).split())  # type: ignore[arg-type]
    if role != "analyst_worker":  # the Q1 rule, as the lead briefs and the co-lead reviews it
        assert "depends on a `parameter`-kind argument" in content and "recommended settings only" in content, role
        # Synthetic validation data is a marked ObsID the tests build in their fixture obs-db (user
        # ruling 2026-11-04), in the form the runtime flags: a result built on it is synthetic.
        from auto_research.provenance.store import is_synthetic_path

        assert "`synth<YYMMDD>-<hex>`" in content and "fixture obs-db" in content, role
        example = "synth<YYMMDD>-<hex>".replace("<YYMMDD>", "261104").replace("<hex>", "ab12cd")
        assert is_synthetic_path(Path("IXPE") / example / "event_l2" / "x.fits")
    assert "own folder" in content or role == "analyst_colead", f"{role}: says nothing of the call's own folder"
    assert "output argument" in content or "`output` argument" in content, role
    if role != "analyst_worker":
        assert "obsdb_catalog" in content and "products" in content, role
