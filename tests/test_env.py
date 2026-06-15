"""Deterministic tests for launch-time environment handling."""

from __future__ import annotations

import os

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
    for group in env._REQUIRED:
        for key in group:
            monkeypatch.delenv(key, raising=False)

    env.check_required_env()

    assert os.environ["ANTHROPIC_API_KEY"] == "test-anthropic"
    assert os.environ["GEMINI_API_KEY"] == "test-google-alias"
    assert os.environ["ADS_API_TOKEN"] == "test-ads-alias"
