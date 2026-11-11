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
    configured_seat,
    env_seat,
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


def test_several_roles_and_a_two_member_group():
    parsed = parse_seats(
        "reviewer=dashscope:qwen3.8-max,"
        "analyst_worker=anthropic:claude-sonnet-5|dashscope:qwen3.7-plus"
    )
    assert set(parsed) == {"reviewer", "analyst_worker"}
    assert [m.model for m in parsed["analyst_worker"]] == ["claude-sonnet-5", "qwen3.7-plus"]
    assert [m.provider for m in parsed["analyst_worker"]] == ["anthropic", "dashscope"]


def test_a_member_is_written_without_capability_flags():
    """A1: what a member may be sent is its adapter's declaration, so the written member carries
    no `vision`/`documents` key (the registry refuses one) and a text-only model needs no flag."""
    (member,) = parse_seats("analyst_colead=deepseek:deepseek-v4-pro")["analyst_colead"]
    assert "vision" not in member.member() and "documents" not in member.member()


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

    for name in ("orchestration.yaml", "code_gate.yaml"):
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


def test_a_sweep_seat_round_trips_with_its_price():
    arms = pytest.importorskip("benchmarks.science_bench.arms")
    override = from_seat(arms.COLEAD)
    assert (override.provider, override.model) == (arms.COLEAD.provider, arms.COLEAD.model)
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

    write_config_dir(tmp_path, {})  # a whole configuration, as a session's `--seats` writes
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


# ── configured_seat: a configured member as another role's seat ───────────────────────────────


def test_a_configured_seat_reads_the_member_through_the_configuration_pointer():
    """Inside an override it returns the overriding member, settings and prices included."""
    second = seat("dashscope", "qwen3.7-plus", parameters={"reasoning": {"effort": "low"}},
                  priceMTk={"input": 0.4, "output": 1.2})
    with seated(analyst_worker=[seat("anthropic", "claude-sonnet-5"), second]):
        got = configured_seat("analyst_worker", 1)
    assert (got.provider, got.model, got.parameters, got.priceMTk) == (
        "dashscope", "qwen3.7-plus", {"reasoning": {"effort": "low"}}, {"input": 0.4, "output": 1.2})


def test_a_configured_seat_seats_another_role_as_the_registry_resolves_its_own():
    """The cells' path: the lead seated on the configured worker tier resolves to that member."""
    worker = load_registry().resolve("analyst_worker").handles[0].config
    with seated(analyst_lead=configured_seat("analyst_worker")):
        lead = load_registry().resolve("analyst_lead").handles[0]
    assert lead.role == "analyst_lead"
    assert (lead.config.provider, lead.config.model, lead.config.parameters) == (
        worker.provider, worker.model, worker.parameters)
    assert lead.config.priceMTk == worker.priceMTk


def test_a_configured_coder_seat_keeps_its_harness_fields():
    raw = yaml.safe_load(models_yaml().read_text(encoding="utf-8"))["roles"]["coder"]["members"][0]
    got = configured_seat("coder")
    assert (got.harness, got.base_url, got.key_env) == (
        raw.get("harness"), raw.get("base_url"), raw.get("key_env"))


def test_a_configured_seat_of_an_unconfigured_role_raises():
    with pytest.raises(KeyError):
        configured_seat("no_such_role")


def test_a_configured_seat_carries_context_tokens_and_refuses_a_field_it_cannot_carry(tmp_path):
    source = yaml.safe_load(models_yaml().read_text(encoding="utf-8"))
    source["roles"]["analyst_worker"]["members"][0]["context_tokens"] = 70000
    write_config_dir(tmp_path / "ok", {})
    (tmp_path / "ok" / "models.yaml").write_text(yaml.safe_dump(source), encoding="utf-8")
    os.environ[CONFIG_DIR_ENV] = str(tmp_path / "ok")
    try:
        got = configured_seat("analyst_worker")
        assert got.context_tokens == 70000 and got.member()["context_tokens"] == 70000
        with seated(analyst_lead=got):
            assert load_registry().resolve("analyst_lead").handles[0].config.context_tokens == 70000
        source["roles"]["analyst_worker"]["members"][0]["unheard_of"] = 1
        (tmp_path / "ok" / "models.yaml").write_text(yaml.safe_dump(source), encoding="utf-8")
        with pytest.raises(ValueError, match="unheard_of"):
            configured_seat("analyst_worker")
    finally:
        os.environ.pop(CONFIG_DIR_ENV, None)


def test_nested_overrides_compose_with_the_configuration_in_force():
    """An inner block replaces only the roles it names: a session's (or outer block's) seats stay."""
    lead = load_registry().resolve("analyst_lead").handles[0].config.model
    reviewer = load_registry().resolve("reviewer").handles[0].config.model
    with seated(analyst_worker=seat("openai", "gpt-5.6-terra")):
        with seated(reviewer=configured_seat("analyst_worker")):
            registry = load_registry()
            assert registry.resolve("reviewer").handles[0].config.model == "gpt-5.6-terra"
            assert registry.resolve("analyst_worker").handles[0].config.model == "gpt-5.6-terra"
            assert registry.resolve("analyst_lead").handles[0].config.model == lead
        assert load_registry().resolve("reviewer").handles[0].config.model == reviewer
        assert load_registry().resolve("analyst_worker").handles[0].config.model == "gpt-5.6-terra"


def test_an_environment_seat_is_parsed_strictly(monkeypatch):
    monkeypatch.delenv("AR_TEST_SEAT", raising=False)
    assert env_seat("AR_TEST_SEAT") is None
    monkeypatch.setenv("AR_TEST_SEAT", "openai:gpt-5.6-terra")
    got = env_seat("AR_TEST_SEAT")
    assert (got.provider, got.model) == ("openai", "gpt-5.6-terra")
    for bad in ("gpt-5.6-terra", "openai:", "openai:a|anthropic:b", "openai:a,reviewer=x:y"):
        monkeypatch.setenv("AR_TEST_SEAT", bad)
        with pytest.raises(ValueError, match="AR_TEST_SEAT"):
            env_seat("AR_TEST_SEAT")


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

    A live test dies at construction just as easily as it dies at a model call, and the `--live` gate in
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
