"""Z1d — the capability table (`config/model_capabilities.yaml`) is the one source of what a model
takes; the adapters trust it, and the launch refuses a member whose model has no entry."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
TABLE = REPO / "config" / "model_capabilities.yaml"


def _real_table():
    from auto_research.models.capabilities import load_capabilities

    return load_capabilities(TABLE)


def _roster_models() -> set[tuple[str, str]]:
    """Every provider/model a configuration or a benchmark roster names."""
    from benchmarks.author_bench.legs import GRID_AUTHOR, GRID_COLEADS, GRID_LEADS, ROSTER
    from benchmarks.science_bench import arms

    pairs = set()
    models = yaml.safe_load((REPO / "config" / "models.yaml").read_text(encoding="utf-8"))
    for group in models["roles"].values():
        for member in group["members"]:
            pairs.add((member["provider"], member["model"]))
    for leg in ROSTER:
        pairs.add((leg.provider, leg.model))
        for provider, model in ((leg.lead_provider, leg.lead_model), (leg.colead_provider, leg.colead_model)):
            if provider and model:
                pairs.add((provider, model))
    pairs |= {(m.provider, m.model) for m in (*GRID_LEADS, *GRID_COLEADS)}
    pairs.add((GRID_AUTHOR["provider"], GRID_AUTHOR["model"]))
    for slot in arms.DEMO:
        for seat in (*slot.arm.seats(), *slot.arm.workers.members):
            pairs.add((seat.provider, seat.model))
    reviewers = yaml.safe_load((REPO / "benchmarks" / "gate_bench" / "reviewers.yaml").read_text())
    for group in reviewers["roles"].values():
        for member in group["members"]:
            pairs.add((member["provider"], member["model"]))
    # Live tests pin seats as ("provider", "model") tuples; a missing entry would end the paid run.
    seat = re.compile(r'\("(anthropic|openai|google|deepseek|zai|dashscope)", "([a-z0-9.\-]+)"\)')
    for path in sorted((REPO / "tests").glob("test_live_*.py")):
        pairs |= set(seat.findall(path.read_text(encoding="utf-8")))
    return pairs


def test_every_configured_and_roster_model_has_an_entry():
    table = _real_table()
    missing = sorted(pair for pair in _roster_models() if pair not in table)
    assert not missing, f"models with no capability entry: {missing}"


def test_the_capability_is_the_tables_entry_and_the_adapter_follows_it(tmp_path, monkeypatch):
    """Flip one model's entry: what the member may be sent changes with it, whatever its adapter."""
    from auto_research.models.providers import media_capability
    from auto_research.models.types import RoleModelConfig

    table = yaml.safe_load(TABLE.read_text(encoding="utf-8"))
    table["zai"]["glm-5.3"] = {"images": True, "documents": True}
    table["openai"]["gpt-5.5"] = {"images": False, "documents": False}
    path = tmp_path / "model_capabilities.yaml"
    path.write_text(yaml.safe_dump(table), encoding="utf-8")
    monkeypatch.setattr("auto_research.models.capabilities.model_capabilities_yaml", lambda: path)

    glm = media_capability(RoleModelConfig(name="g", provider="zai", model="glm-5.3"))
    gpt = media_capability(RoleModelConfig(name="o", provider="openai", model="gpt-5.5"))
    # Images follow the entry; a document reaches Z.AI as the local parse (its dialect carries no
    # inline document), which is a wire fact of the adapter, not a capability.
    assert (glm.images, glm.native_documents) == (True, False)
    assert (gpt.images, gpt.native_documents) == (False, False)


def test_a_model_with_no_entry_is_refused_naming_the_table():
    from auto_research.models.capabilities import MissingModelCapability
    from auto_research.models.providers import media_capability
    from auto_research.models.types import RoleModelConfig

    with pytest.raises(MissingModelCapability, match=r"openai/gpt-unlisted: add `gpt-unlisted: "
                       r"\{images: \.\.\., documents: \.\.\.\}` under `openai:`"):
        media_capability(RoleModelConfig(name="o", provider="openai", model="gpt-unlisted"))


@pytest.mark.parametrize("entry", [{"images": True}, {"images": "yes", "documents": True},
                                   {"images": True, "documents": True, "vision": True},
                                   {"images": True, True: True}])
def test_a_malformed_entry_is_refused(tmp_path, entry):
    from auto_research.models.capabilities import load_capabilities

    path = tmp_path / "model_capabilities.yaml"
    path.write_text(yaml.safe_dump({"openai": {"gpt-x": entry}}), encoding="utf-8")
    with pytest.raises(ValueError, match="openai/gpt-x needs exactly `images` and `documents`"):
        load_capabilities(path)


@pytest.mark.parametrize(("text", "says"), [
    ("openai:\n  gpt-x: {images: true, documents: true}\n  gpt-x: {images: false, documents: false}\n",
     "'gpt-x' is given twice"),
    ("openais:\n  gpt-x: {images: true, documents: true}\n", "'openais' is not a provider"),
])
def test_a_duplicate_or_an_unknown_provider_is_refused(tmp_path, text, says):
    from auto_research.models.capabilities import load_capabilities

    path = tmp_path / "model_capabilities.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=says):
        load_capabilities(path)


def test_a_config_dir_without_the_table_is_refused_naming_it(tmp_path, monkeypatch):
    """The table resolves from the active config dir (not the suite's stand-in table here)."""
    from auto_research import config_paths
    from auto_research.models.capabilities import MissingModelCapability, capability_for

    monkeypatch.setattr("auto_research.models.capabilities.model_capabilities_yaml",
                        config_paths.model_capabilities_yaml)
    monkeypatch.setenv("AUTO_RESEARCH_CONFIG_DIR", str(tmp_path))
    with pytest.raises(MissingModelCapability, match="copy config/model_capabilities.yaml"):
        capability_for("openai", "gpt-5.5")
    (tmp_path / "model_capabilities.yaml").write_text(TABLE.read_text(encoding="utf-8"), encoding="utf-8")
    assert capability_for("openai", "gpt-5.5").images is True


def test_a_seat_override_config_dir_carries_the_table(tmp_path):
    from tests.seats import write_config_dir

    directory = write_config_dir(tmp_path / "seats", {})
    assert (directory / "model_capabilities.yaml").read_bytes() == TABLE.read_bytes()


def test_preflight_refuses_a_member_whose_model_has_no_entry(tmp_path, monkeypatch):
    from auto_research.models.capabilities import MissingModelCapability, check_member_capabilities
    from auto_research.preflight import build_default_preflight

    assert "member_capabilities" in build_default_preflight().names()
    check_member_capabilities()  # the repository's own configuration passes

    models = yaml.safe_load((REPO / "config" / "models.yaml").read_text(encoding="utf-8"))
    role = next(iter(models["roles"]))
    models["roles"][role]["members"][0]["model"] = "model-nobody-described"
    config = tmp_path / "cfg"
    config.mkdir()
    (config / "models.yaml").write_text(yaml.safe_dump(models), encoding="utf-8")
    monkeypatch.setenv("AUTO_RESEARCH_CONFIG_DIR", str(config))
    with pytest.raises(MissingModelCapability, match=rf"role {role}, member .*model-nobody-described"):
        check_member_capabilities()


_MODEL_ID = re.compile(r"^(claude|gpt|o\d|gemini|deepseek|glm|qwen|text-embedding)-?[0-9a-z.\-]*\d")


def test_no_adapter_names_a_model_in_its_code():
    """User ruling 2026-11-04: model choices are configuration. String literals only (a docstring
    or comment may quote a vendor page)."""
    offenders = []
    for path in sorted((REPO / "src" / "auto_research" / "models" / "providers").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value) for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings and _MODEL_ID.match(node.value)):
                offenders.append(f"{path.name}:{node.lineno} {node.value!r}")
    assert not offenders, offenders
