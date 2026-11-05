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
                        "parameters": {"max_tokens": 1024},
                    },
                    {
                        "name": "gpt",
                        "provider": "openai",
                        "model": "reviewer-second-model",
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

    # The shipped seats of the figure-seeing roles take images, as each adapter declares (A1). A
    # text-only seat is legal (A2: it receives notes); this pins the configured team.
    from auto_research.models.providers import media_capability

    for role in _VISION_ROLES:
        assert all(media_capability(h.config).images for h in registry.resolve(role).handles), (
            f"every {role} member must be vision-capable (§6/§7)"
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
        config=RoleModelConfig(name="fake", provider="openai", model="fake-model"),
        provider=FakeProvider(),
    )

    response = asyncio.run(handle.call([{"role": "user", "content": "hello"}]))

    assert response.content == "hi"
    assert response.usage.input_tokens == 1
    assert response.usage.output_tokens == 2


def test_a_retired_media_flag_is_refused_where_the_config_loads(tmp_path):
    data = _models_config()
    data["roles"]["reviewer"]["members"][0]["vision"] = True
    with pytest.raises(ValueError, match="vision"):
        load_registry(_write_models_yaml(tmp_path, data))


@pytest.mark.parametrize(("provider", "needs_parser"), [("dashscope", True), ("openai", False)])
def test_the_document_parser_check_reads_the_adapters_parsed_route(
    tmp_path, monkeypatch, provider, needs_parser,
):
    """A1: preflight's `document_parser_ready` reads the same capability as the handle: a member
    whose adapter routes documents through the local parse needs docling, any other does not."""
    import sys

    from auto_research.models.providers._doc_parse import check_document_parser_ready

    data = _models_config()
    data["roles"]["librarian_io"]["members"][0]["provider"] = provider
    for group in data["roles"].values():  # every member a model the capability table describes
        for member in group["members"]:
            member["model"] = "fake-model"
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    _write_models_yaml(config_dir, data)
    monkeypatch.setenv("AUTO_RESEARCH_CONFIG_DIR", str(config_dir))
    monkeypatch.setitem(sys.modules, "docling", None)  # docling absent: `import docling` raises

    if needs_parser:
        with pytest.raises(ValueError, match="docling"):
            check_document_parser_ready()
    else:
        assert check_document_parser_ready() is None
