"""The seat-override helper is test infrastructure that gates a paid batch, so it is tested.

A probe pointed at the wrong configuration does not look like a defect; it looks like a boring
result — the 2026-09-01 lesson, where three paid runs were lost to bugs in the code that *read*
results rather than in the code under test. Everything here is deterministic and free: no provider
is called, and the only thing asserted is that what the helper wrote is what a reader resolves.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from auto_research.config_paths import CONFIG_DIR_ENV, config_dir, models_yaml, repo_config_dir
from auto_research.models.registry import REQUIRED_ROLES, load_registry
from auto_research.models.types import ModelsConfig
from tests.seats import (
    from_seat,
    models_config,
    parse_seats,
    pinned_parameters,
    seat,
    seated,
    write_config_dir,
)


# ── the spec language ───────────────────────────────────────────────────────────────────


def test_one_role_one_member():
    parsed = parse_seats("analyst_lead=openai:gpt-5.6-terra")
    assert list(parsed) == ["analyst_lead"]
    (member,) = parsed["analyst_lead"]
    assert (member.provider, member.model) == ("openai", "gpt-5.6-terra")
    assert member.vision and member.documents


def test_several_roles_and_a_two_member_group():
    parsed = parse_seats(
        "reviewer=dashscope:qwen3.8-max,"
        "analyst_worker=anthropic:claude-sonnet-5|dashscope:qwen3.7-plus"
    )
    assert set(parsed) == {"reviewer", "analyst_worker"}
    assert [m.model for m in parsed["analyst_worker"]] == ["claude-sonnet-5", "qwen3.7-plus"]
    assert [m.provider for m in parsed["analyst_worker"]] == ["anthropic", "dashscope"]


def test_capability_flags_clear_the_declaration():
    """A text-only member must be *expressible*, since the declaration is what the guard reads."""
    parsed = parse_seats("analyst_colead=deepseek:deepseek-v4-pro!novision!nodocs")
    (member,) = parsed["analyst_colead"]
    assert not member.vision
    assert not member.documents


def test_whitespace_around_a_clause_is_ignored():
    parsed = parse_seats(" analyst_lead = openai : gpt-5.6-terra , reviewer=anthropic:claude-fable-5 ")
    assert set(parsed) == {"analyst_lead", "reviewer"}
    assert parsed["analyst_lead"][0].model == "gpt-5.6-terra"


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "   ",
        "analyst_lead",
        "analyst_lead=",
        "=openai:gpt-5.6-terra",
        "analyst_lead=openai",
        "analyst_lead=:gpt-5.6-terra",
        "analyst_lead=openai:",
        "analyst_lead=openai:gpt-5.6-terra|",
        "analyst_lead=openai:gpt-5.6-terra!notaflag",
    ],
)
def test_a_malformed_spec_raises_rather_than_silently_seating_nobody(spec):
    """A typo must stop the session, not run the default seats under a name that says otherwise."""
    with pytest.raises(ValueError):
        parse_seats(spec)


# ── what gets written ───────────────────────────────────────────────────────────────────


def test_untouched_roles_survive_and_required_roles_stay_satisfied():
    source = yaml.safe_load((repo_config_dir() / "models.yaml").read_text(encoding="utf-8"))
    built = models_config(parse_seats("analyst_lead=openai:gpt-5.6-terra"), source)

    assert set(built["roles"]) == set(source["roles"]), "an override changed which roles exist"
    assert REQUIRED_ROLES <= set(built["roles"])
    assert built["roles"]["analyst_lead"]["members"][0]["model"] == "gpt-5.6-terra"
    for role in set(source["roles"]) - {"analyst_lead"}:
        assert built["roles"][role] == source["roles"][role], f"{role} was not left alone"


def test_the_written_directory_validates_and_only_models_differs(tmp_path):
    directory = write_config_dir(
        tmp_path / "cfg", parse_seats("reviewer=dashscope:qwen3.8-max")
    )

    for name in ("orchestration.yaml", "observations.yaml", "code_gate.yaml"):
        origin = repo_config_dir() / name
        if origin.is_file():
            assert (directory / name).read_bytes() == origin.read_bytes(), (
                f"{name} must be byte-identical: an override varies seats and nothing else, "
                "which is what keeps the value-caching config readers correct"
            )

    written = yaml.safe_load((directory / "models.yaml").read_text(encoding="utf-8"))
    ModelsConfig.model_validate(written)  # the registry's own validation, before any provider call
    assert written["roles"]["reviewer"]["members"][0]["provider"] == "dashscope"


def test_a_member_carries_the_campaigns_pinned_parameters(tmp_path):
    """A probe must send the batch's request, so effort is pinned rather than vendor-defaulted."""
    directory = write_config_dir(
        tmp_path / "cfg",
        parse_seats("analyst_lead=openai:gpt-5.6-terra,reviewer=anthropic:claude-fable-5"),
    )
    written = yaml.safe_load((directory / "models.yaml").read_text(encoding="utf-8"))

    lead = written["roles"]["analyst_lead"]["members"][0]
    assert lead["parameters"] == pinned_parameters("openai")
    assert lead["parameters"]["reasoning"]["effort"] == "high"

    reviewer = written["roles"]["reviewer"]["members"][0]
    assert "max_tokens" in reviewer["parameters"], (
        "Anthropic requires max_tokens; omitting it is a silent fallback to 1024"
    )


def test_an_explicit_parameters_argument_wins_over_the_pin():
    member = seat("openai", "gpt-5.6-terra", parameters={"reasoning": {"effort": "low"}})
    assert member.parameters == {"reasoning": {"effort": "low"}}


def test_a_sweep_seat_round_trips_with_its_flags_and_price():
    arms = pytest.importorskip("benchmarks.science_bench.arms")
    override = from_seat(arms.COLEAD)
    assert (override.provider, override.model) == (arms.COLEAD.provider, arms.COLEAD.model)
    assert override.vision == arms.COLEAD.vision
    assert override.documents == arms.COLEAD.documents
    assert override.parameters == arms.COLEAD.parameters()
    assert override.priceMTk == arms.COLEAD.price()


def test_coder_harness_fields_reach_the_member():
    member = seat(
        "deepseek", "deepseek-v4-flash", harness="codex", base_url="https://x/", key_env="K"
    ).member()
    assert member["harness"] == "codex"
    assert member["base_url"] == "https://x/"
    assert member["key_env"] == "K"


# ── the context manager, and the no-op default ──────────────────────────────────────────


def test_inside_the_block_a_reader_resolves_the_override_and_outside_it_does_not():
    before = load_registry().resolve("analyst_lead").handles[0].config.model

    with seated(analyst_lead=seat("openai", "gpt-5.6-terra")) as directory:
        assert config_dir() == directory
        assert models_yaml() == directory / "models.yaml"
        handle = load_registry().resolve("analyst_lead").handles[0]
        assert handle.config.model == "gpt-5.6-terra"
        assert handle.config.provider == "openai"
        # The role layer is still in front of it: a resolved handle carries its role and provider
        # adapter, not just a model string.
        assert handle.role == "analyst_lead"

    assert load_registry().resolve("analyst_lead").handles[0].config.model == before


def test_the_previous_configuration_pointer_is_restored_either_way(tmp_path):
    os.environ.pop(CONFIG_DIR_ENV, None)
    with seated(reviewer=seat("anthropic", "claude-fable-5")):
        pass
    assert CONFIG_DIR_ENV not in os.environ

    os.environ[CONFIG_DIR_ENV] = str(tmp_path)
    try:
        with seated(reviewer=seat("anthropic", "claude-fable-5")):
            assert os.environ[CONFIG_DIR_ENV] != str(tmp_path)
        assert os.environ[CONFIG_DIR_ENV] == str(tmp_path)
    finally:
        os.environ.pop(CONFIG_DIR_ENV, None)


def test_a_temporary_directory_is_removed_and_a_given_one_is_kept(tmp_path):
    with seated(reviewer=seat("anthropic", "claude-fable-5")) as temporary:
        assert temporary.is_dir()
    assert not temporary.exists()

    kept = tmp_path / "kept"
    with seated(reviewer=seat("anthropic", "claude-fable-5"), config_dir=kept):
        pass
    assert (kept / "models.yaml").is_file()


def test_a_role_name_that_is_not_an_identifier_goes_through_the_mapping_form():
    with seated({"analyst_lead": seat("openai", "gpt-5.6-terra")}):
        assert load_registry().resolve("analyst_lead").handles[0].config.model == "gpt-5.6-terra"


def test_seated_refuses_an_empty_override():
    with pytest.raises(ValueError):
        with seated():
            pass


def test_a_two_member_group_resolves_as_two_handles():
    workers = parse_seats("analyst_worker=anthropic:claude-sonnet-5|dashscope:qwen3.7-plus")
    with seated(workers):
        handles = load_registry().resolve("analyst_worker").handles
        assert [h.config.model for h in handles] == ["claude-sonnet-5", "qwen3.7-plus"]
        assert len({h.config.provider for h in handles}) == 2, "the pair must stay decorrelated"


def test_by_default_the_session_overrides_nothing(_seat_overrides):
    """The conftest's inactive path is a no-op, not a subtly different default.

    Requesting the session fixture by name is how this asserts on the mechanism rather than on a
    reimplementation of it. Run with ``--seats`` this test is skipped rather than failed, since
    then the session is *meant* to be pointed elsewhere.
    """
    if _seat_overrides is not None:
        pytest.skip("session was launched with --seats, so the override is expected")
    assert config_dir() == repo_config_dir()
    assert Path(os.environ.get(CONFIG_DIR_ENV, repo_config_dir())) == repo_config_dir()


@pytest.mark.parametrize("choice", ["codex", "claude_sdk", ""])
def test_the_live_coding_tests_coder_seat_builds_for_every_harness(monkeypatch, choice):
    """The seat a paid live test opens with is constructed for free, here.

    A live test dies at construction just as easily as it dies at a model call, and the env flag in
    front of it means it gets far fewer chances to be caught than an ordinary source branch (the
    2026-08-20 lesson). Measured: the first launch of the rebuilt chain test spent nothing and
    failed on ``FrozenInstanceError`` assigning to ``SeatOverride.harness`` — a frozen dataclass,
    the same trap as N11 — which this would have caught with no run at all.
    """
    from auto_research.sandbox.coder.harness import HARNESS_ENDPOINTS
    from tests.test_live_loader_authoring import CODER_HARNESS_ENV, _coder_seat

    if choice:
        monkeypatch.setenv(CODER_HARNESS_ENV, choice)
    else:
        monkeypatch.delenv(CODER_HARNESS_ENV, raising=False)

    override = _coder_seat()
    assert override.provider and override.model
    assert (override.provider, override.harness) in HARNESS_ENDPOINTS, (
        f"{override.provider}/{override.harness} is not a legal author pairing"
    )
    if choice:
        assert override.harness == choice
    # And the registry resolves what it writes — the seat is a written file, not an intention.
    with seated(coder=override):
        member = load_registry().resolve("coder").handles[0].config
        assert (member.provider, member.harness) == (override.provider, override.harness)


def test_an_unknown_coder_harness_is_refused_rather_than_silently_defaulted(monkeypatch):
    """A typo must not quietly author on whichever backend the configuration happens to name — the
    run would then report a harness it did not use."""
    from tests.test_live_loader_authoring import CODER_HARNESS_ENV, _coder_seat

    monkeypatch.setenv(CODER_HARNESS_ENV, "claude-sdk")
    with pytest.raises(ValueError, match="claude-sdk"):
        _coder_seat()
