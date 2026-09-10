"""Deterministic tests for the step-2 model registry."""

import asyncio
from typing import get_args

import pytest
import yaml

from auto_research.models import ModelHandle, ModelMessage, ModelResponse, ModelUsage, load_registry
from auto_research.models.registry import REQUIRED_ROLES
from auto_research.models.types import ProviderName, RoleModelConfig


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


# Capability contracts a valid config must uphold regardless of WHICH models back a role.
# Derived from the annotation the field itself carries, never re-listed: a vendor added to
# ``ProviderName`` is legal in a role the moment the adapter exists, and a literal here would
# fail a valid config instead of the invalid one it is meant to catch.
_VALID_PROVIDERS = set(get_args(ProviderName))
_VISION_ROLES = {"reviewer", "analyst_lead", "analyst_worker"}  # must see figures (§6/§7)
_DOCUMENT_ROLES = {"reviewer", "librarian_io"}  # ingest PDFs (open_paper / literature)


def test_project_config_satisfies_role_contract():
    """The shipped `config/models.yaml` loads and upholds the role *contract* — not any specific
    model choice.

    `models.yaml` is an editable config default (CLAUDE.md §6: "edit freely — not code"), so this
    asserts structure and capabilities, never member identities.
    """
    registry = load_registry()

    # Every required role is present and resolves to a non-empty group of valid handles.
    assert REQUIRED_ROLES <= set(registry.config.roles)
    for role in REQUIRED_ROLES:
        handles = registry.resolve(role).handles
        assert handles, f"role {role} has no configured members"
        for handle in handles:
            assert handle.config.name
            assert handle.config.model
            assert handle.config.provider in _VALID_PROVIDERS

    # Capability contracts (model-independent): vision for the figure-seeing roles, document
    # input for the PDF-reading roles.
    for role in _VISION_ROLES:
        assert all(handle.config.vision for handle in registry.resolve(role).handles), (
            f"every {role} member must be vision-capable (§6/§7)"
        )
    for role in _DOCUMENT_ROLES:
        assert all(handle.config.documents for handle in registry.resolve(role).handles), (
            f"every {role} member must accept document (PDF) inputs"
        )


class FakeProvider:
    async def call(
        self, messages, config, *, schema=None, tools=None, tool_choice="auto",
        images=(), documents=(), cache_prefix=False,
    ):
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
