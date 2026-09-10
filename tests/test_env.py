"""Deterministic tests for launch-time environment handling."""

from __future__ import annotations

import os

import pytest

from auto_research import config_paths, env


def test_check_required_env_loads_project_dotenv(tmp_path, monkeypatch) -> None:
    # These five are written by hand because each is the point of the test: three are
    # *non-canonical* aliases, so resolving them is what proves the alias groups work.
    aliases = [
        "ANTHROPIC_API_KEY=test-anthropic",
        "OPENAI_API_KEY=test-openai",
        "GEMINI_API_KEY=test-google-alias",
        "ADS_API_TOKEN=test-ads-alias",
        "TAVILY_API_KEY=test-tavily",
    ]
    # Everything else the *project* configuration implies is derived rather than listed. The
    # requirement comes from `config/models.yaml`, so a fixed list makes this test fail whenever a
    # role gains a member from a new vendor — a valid config failing the check that exists to
    # catch an invalid one. (Measured: adding the deepseek coder on 2026-09-09 did exactly that.)
    model_groups, _ = env._model_key_groups()
    named = {line.split("=", 1)[0] for line in aliases}
    derived = [
        f"{group[0]}=test-derived"
        for group in (*env._REQUIRED, *model_groups)
        if not set(group) & named
    ]
    dotenv = tmp_path / ".env"
    dotenv.write_text("\n".join([*aliases, *derived]), encoding="utf-8")
    monkeypatch.setattr(env, "_ENV_FILE", dotenv)
    # Clear every name the check consults — the fixed source keys and the model keys the
    # configuration implies — since `load_dotenv(override=False)` leaves a name that is already
    # set, and a real key in this process's environment would mask the file under test.
    for group in (*env._REQUIRED, *model_groups):
        for key in group:
            monkeypatch.delenv(key, raising=False)

    env.check_required_env()

    assert os.environ["ANTHROPIC_API_KEY"] == "test-anthropic"
    assert os.environ["GEMINI_API_KEY"] == "test-google-alias"
    assert os.environ["ADS_API_TOKEN"] == "test-ads-alias"


def _write_models(tmp_path, roles: str) -> None:
    (tmp_path / "models.yaml").write_text(roles, encoding="utf-8")


def _isolate(monkeypatch, tmp_path, roles: str, dotenv: str) -> None:
    """Point the env check at a throwaway .env + models.yaml and clear every name they use."""
    env_file = tmp_path / ".env"
    env_file.write_text(dotenv, encoding="utf-8")
    _write_models(tmp_path, roles)
    monkeypatch.setattr(env, "_ENV_FILE", env_file)
    # Through the real seam rather than an internal attribute: the env var is how a process is
    # pointed at another configuration in production, so patching it exercises what actually runs.
    monkeypatch.setenv(config_paths.CONFIG_DIR_ENV, str(tmp_path))
    for name in (
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
        "ADS_DEV_KEY", "ADS_API_TOKEN", "TAVILY_API_KEY", "DEEPSEEK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


_SOURCES = "ADS_DEV_KEY=a\nTAVILY_API_KEY=t\n"


def test_required_keys_are_derived_from_the_configured_providers(monkeypatch, tmp_path) -> None:
    """A run needs the vendors it is configured with — not a list written once. No Google member
    means no Google key is demanded."""
    roles = """
roles:
  reviewer:
    members:
      - name: claude
        provider: anthropic
        model: claude-x
  coder:
    members:
      - name: gpt
        provider: openai
        model: gpt-x
"""
    _isolate(monkeypatch, tmp_path, roles, _SOURCES + "ANTHROPIC_API_KEY=a\nOPENAI_API_KEY=o\n")
    env.check_required_env()  # no GOOGLE_API_KEY anywhere, and none is required


def test_a_member_naming_key_env_makes_that_variable_required(monkeypatch, tmp_path) -> None:
    """The coder harnesses reach DeepSeek, Z.AI and Model Studio through a `key_env` variable.
    Without this the run fails later inside an author subprocess instead of at launch."""
    roles = """
roles:
  coder:
    members:
      - name: deepseek_v4_pro
        provider: deepseek
        harness: codex
        model: deepseek-v4-pro
        base_url: https://api.deepseek.com/
        key_env: DEEPSEEK_API_KEY
"""
    _isolate(monkeypatch, tmp_path, roles, _SOURCES)
    with pytest.raises(RuntimeError) as excinfo:
        env.check_required_env()
    message = str(excinfo.value)
    assert "DEEPSEEK_API_KEY" in message
    # The message names who wanted it, because that is what makes it fixable.
    assert "coder/deepseek_v4_pro" in message


def test_a_configured_key_env_that_is_set_passes(monkeypatch, tmp_path) -> None:
    roles = """
roles:
  coder:
    members:
      - name: deepseek_v4_pro
        provider: deepseek
        harness: codex
        model: deepseek-v4-pro
        key_env: DEEPSEEK_API_KEY
"""
    _isolate(monkeypatch, tmp_path, roles, _SOURCES + "DEEPSEEK_API_KEY=d\n")
    env.check_required_env()


def test_an_unreadable_models_config_falls_back_to_the_historical_keys(monkeypatch, tmp_path):
    """A config we cannot parse must not silently relax the check to nothing."""
    monkeypatch.setenv(config_paths.CONFIG_DIR_ENV, str(tmp_path))  # holds no models.yaml
    env_file = tmp_path / ".env"
    env_file.write_text(_SOURCES, encoding="utf-8")
    monkeypatch.setattr(env, "_ENV_FILE", env_file)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        env.check_required_env()
    for expected in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        assert expected in str(excinfo.value)


def test_google_aliases_still_satisfy_each_other(monkeypatch, tmp_path) -> None:
    roles = """
roles:
  librarian_io:
    members:
      - name: gemini
        provider: google
        model: gemini-x
"""
    _isolate(monkeypatch, tmp_path, roles, _SOURCES + "GEMINI_API_KEY=g\n")
    env.check_required_env()

def test_a_vendor_member_without_key_env_uses_that_vendor_s_own_variable(monkeypatch, tmp_path):
    """The sweep will configure these vendors as chat members too, so a member that names no
    `key_env` still requires the variable that vendor's own SDK reads."""
    roles = """
roles:
  analyst_worker:
    members:
      - name: glm
        provider: zai
        model: glm-5.3
"""
    _isolate(monkeypatch, tmp_path, roles, _SOURCES)
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ZAI_API_KEY"):
        env.check_required_env()

    monkeypatch.setenv("ZAI_API_KEY", "z")
    env.check_required_env()


def test_key_env_wins_over_the_provider_default(monkeypatch, tmp_path) -> None:
    """A plan-specific endpoint may use a differently-named variable, so the member's own choice
    is what the check honours."""
    roles = """
roles:
  coder:
    members:
      - name: qwen_token_plan
        provider: dashscope
        harness: codex
        model: qwen3.7-plus
        key_env: DASHSCOPE_TOKEN_PLAN_KEY
"""
    _isolate(monkeypatch, tmp_path, roles, _SOURCES)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_TOKEN_PLAN_KEY", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        env.check_required_env()
    assert "DASHSCOPE_TOKEN_PLAN_KEY" in str(excinfo.value)
    assert "DASHSCOPE_API_KEY" not in str(excinfo.value)


# --------------------------------------------------------------------------- ledger field caps


def _orch(monkeypatch, mapping: dict) -> None:
    monkeypatch.setattr(env, "_orch_config", lambda: mapping)


def test_ledger_caps_default_to_100k_when_the_config_is_silent(monkeypatch) -> None:
    """A config predating the keys keeps a working, generous cap rather than an accidental 0."""
    _orch(monkeypatch, {})
    caps = env.ledger_caps()
    assert (caps.arg, caps.result, caps.coder_text) == (100_000, 100_000, 100_000)


def test_ledger_caps_are_read_from_config_so_a_batch_records_its_own_value(monkeypatch) -> None:
    """The ledger is the run's durable record and an offline scoring pass reads it long after,
    so the cap a batch used has to travel with that batch's config rather than with the source."""
    _orch(monkeypatch, {"ledger_arg_cap": 7, "ledger_result_cap": 8, "ledger_coder_text_cap": 9})
    caps = env.ledger_caps()
    assert (caps.arg, caps.result, caps.coder_text) == (7, 8, 9)


@pytest.mark.parametrize("bad", [0, -1, "nope", None])
def test_a_malformed_or_zero_ledger_cap_falls_back_rather_than_silencing_the_ledger(
    monkeypatch, bad
) -> None:
    """A 0 or a typo would truncate every field to nothing — a silently empty durable record is
    strictly worse than an over-large one."""
    _orch(monkeypatch, {"ledger_arg_cap": bad, "ledger_result_cap": bad,
                        "ledger_coder_text_cap": bad})
    caps = env.ledger_caps()
    assert (caps.arg, caps.result, caps.coder_text) == (100_000, 100_000, 100_000)


def test_the_ledger_consumers_read_the_cap_at_call_time(monkeypatch) -> None:
    """Pinned because the previous shape captured the value at import: a module constant would
    make the config key look wired while doing nothing."""
    from auto_research.agents import roles as roles_mod
    from auto_research.sandbox.coder import events as coder_events

    _orch(monkeypatch, {"ledger_arg_cap": 11, "ledger_result_cap": 12,
                        "ledger_coder_text_cap": 5})
    assert roles_mod._event_arg_cap() == 11
    assert roles_mod._event_result_cap() == 12
    assert coder_events.coder_text_cap() == 5
    # cap_text with no explicit limit follows the config, and states what it dropped.
    assert coder_events.cap_text("abcdefghij") == "abcde… [+5 chars]"
