"""Deterministic tests for launch-time environment handling."""

from __future__ import annotations

import os

import pytest

from auto_research import env


def test_check_required_env_loads_project_dotenv(tmp_path, monkeypatch) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "\n".join(
            [
                "ANTHROPIC_API_KEY=test-anthropic",
                "OPENAI_API_KEY=test-openai",
                "GEMINI_API_KEY=test-google-alias",
                "ADS_API_TOKEN=test-ads-alias",
                "TAVILY_API_KEY=test-tavily",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(env, "_ENV_FILE", dotenv)
    # Clear every name the check consults — the fixed source keys and the model keys the
    # configuration implies — since `load_dotenv(override=False)` leaves a name that is already
    # set, and a real key in this process's environment would mask the file under test.
    model_groups, _ = env._model_key_groups()
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
    monkeypatch.setattr(env, "_MODELS_CONFIG", tmp_path / "models.yaml")
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
    monkeypatch.setattr(env, "_MODELS_CONFIG", tmp_path / "absent.yaml")
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
