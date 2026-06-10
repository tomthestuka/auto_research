"""Deterministic tests for the step-2 model registry."""

import asyncio

import pytest
import yaml

from auto_research.models import ModelHandle, ModelMessage, ModelResponse, ModelUsage, load_registry
from auto_research.models.registry import REQUIRED_ROLES
from auto_research.models.types import RoleModelConfig


def _models_config(**role_overrides):
    roles = {
        role: {
            "members": [
                {
                    "name": f"{role}_member",
                    "provider": "openai",
                    "model": f"{role}-model",
                    "vision": role in {"reviewer", "analyst_lead", "analyst_worker"},
                    "parameters": {"temperature": 0},
                }
            ],
        }
        for role in REQUIRED_ROLES
    }
    roles.update(role_overrides)
    return {"roles": roles}


def _write_models_yaml(tmp_path, data):
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_registry_loads_required_roles(tmp_path):
    path = _write_models_yaml(tmp_path, _models_config())

    registry = load_registry(path)

    assert set(registry.config.roles) == REQUIRED_ROLES


def test_registry_resolves_role_to_group_handle(tmp_path):
    path = _write_models_yaml(
        tmp_path,
        _models_config(
            reviewer={
                "members": [
                    {
                        "name": "opus",
                        "provider": "anthropic",
                        "model": "reviewer-configured-model",
                        "vision": True,
                        "parameters": {"max_tokens": 1024},
                    },
                    {
                        "name": "gpt",
                        "provider": "openai",
                        "model": "reviewer-second-model",
                        "vision": True,
                        "parameters": {"reasoning": {"effort": "high"}},
                    },
                ]
            }
        ),
    )

    group = load_registry(path).resolve("reviewer")

    assert group.role == "reviewer"
    assert [handle.config.name for handle in group.handles] == ["opus", "gpt"]
    assert group.member("opus").config.provider == "anthropic"
    assert group.member("opus").config.model == "reviewer-configured-model"
    assert group.member("opus").config.parameters == {"max_tokens": 1024}


def test_registry_rejects_missing_required_role(tmp_path):
    data = _models_config()
    del data["roles"]["reviewer"]
    path = _write_models_yaml(tmp_path, data)

    with pytest.raises(ValueError, match="reviewer"):
        load_registry(path)


def test_registry_rejects_unknown_provider(tmp_path):
    path = _write_models_yaml(
        tmp_path,
        _models_config(
            reviewer={
                "members": [
                    {
                        "name": "bad",
                        "provider": "unknown",
                        "model": "x",
                    }
                ]
            }
        ),
    )

    with pytest.raises(ValueError):
        load_registry(path)


def test_registry_rejects_unknown_role(tmp_path):
    path = _write_models_yaml(tmp_path, _models_config())

    with pytest.raises(KeyError, match="unknown model role"):
        load_registry(path).resolve("not_a_role")


def test_registry_rejects_empty_group(tmp_path):
    path = _write_models_yaml(tmp_path, _models_config(reviewer={"members": []}))

    with pytest.raises(ValueError):
        load_registry(path)


def test_registry_loads_project_model_groups():
    registry = load_registry()

    assert [handle.config.name for handle in registry.resolve("reviewer").handles] == [
        "claude_opus_4_7",
        "gpt_5_5_high",
        "gemini_3_5_flash_high",
    ]
    assert [handle.config.name for handle in registry.resolve("analyst_worker").handles] == [
        "claude_sonnet_4_6",
        "gpt_5_5_medium",
    ]
    assert [handle.config.name for handle in registry.resolve("librarian_io").handles] == [
        "claude_sonnet_4_6",
        "gpt_5_5_medium",
        "gemini_3_5_flash_thinking",
    ]
    assert all(handle.config.documents for handle in registry.resolve("librarian_io").handles)


class FakeProvider:
    async def call(self, messages, config, *, schema=None, tools=None, tool_choice="auto", images=(), documents=()):
        assert [message.content for message in messages] == ["hello"]
        assert config.model == "fake-model"
        assert schema is None
        assert tools is None
        assert images == ()
        assert documents == ()
        return ModelResponse(content="hi", usage=ModelUsage(input_tokens=1, output_tokens=2))


def test_model_handle_normalizes_messages_and_delegates(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_RESEARCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    handle = ModelHandle(
        role="reviewer",
        config=RoleModelConfig(name="fake", provider="openai", model="fake-model", vision=True),
        provider=FakeProvider(),
    )

    response = asyncio.run(handle.call([{"role": "user", "content": "hello"}]))

    assert response.content == "hi"
    assert response.usage.input_tokens == 1
    assert response.usage.output_tokens == 2


def test_model_handle_rejects_images_for_non_vision_role(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_RESEARCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    handle = ModelHandle(
        role="librarian_searcher",
        config=RoleModelConfig(name="fake", provider="openai", model="fake-model", vision=False),
        provider=FakeProvider(),
    )

    with pytest.raises(ValueError, match="not configured for vision"):
        asyncio.run(
            handle.call(
                [ModelMessage(role="user", content="look")],
                images=[
                    {
                        "path": "/tmp/figure.png",
                        "caption": "Figure",
                        "mime_type": "image/png",
                        "produced_by": "test",
                        "created_at": "2026-05-27T00:00:00Z",
                        "sha256": "a" * 64,
                    }
                ],
            )
        )
