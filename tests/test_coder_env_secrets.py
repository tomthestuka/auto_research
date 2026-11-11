"""No store admin secret reaches an author harness or a unit's process (no LLM, no live call).

The orchestrator loads the stores' admin secrets (``stores[].admin_env``) into its own environment,
and both author SDKs start their CLI from that environment overlaid with the options' ``env``. Each
test declares a store whose admin variables are set in this process and drives the harness's own
env-building function (``claude_sdk._build_options``, ``CodexBackend._build_client``), then checks what
the child would start from: the SDK's merge of ``os.environ`` and the overlay.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from auto_research.config_paths import CONFIG_DIR_ENV
from auto_research.models.types import RoleModelConfig
from auto_research.store import config as store_config

SECRETS = {"T5_S3_KEY": "admin-key-id", "T5_S3_SECRET": "admin-secret-value", "T5_PG_PASSWORD": "pg-admin-pass"}


@pytest.fixture
def secrets_held(tmp_path, monkeypatch):
    """A declared store whose admin secrets this process holds, as the orchestrator does."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "orchestration.yaml").write_text(yaml.safe_dump({"stores": [{
        "name": "db", "kind": "remote", "endpoint": "http://127.0.0.1:1", "buckets": ["data"],
        "catalog_dsn": "host=127.0.0.1 dbname=db user=ar_admin",
        "admin_env": {"s3_access_key": "T5_S3_KEY", "s3_secret_key": "T5_S3_SECRET",
                      "catalog_password": "T5_PG_PASSWORD"}}]}))
    monkeypatch.setenv(CONFIG_DIR_ENV, str(config))
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    store_config.reset()
    yield
    store_config.reset()


def _child_env(overlay: dict[str, str] | None) -> dict[str, str]:
    """What an SDK's child starts from: this process's environment overlaid with ``overlay``."""
    import os

    return {**os.environ, **(overlay or {})}


def test_the_admin_variable_names_come_from_every_declared_store(secrets_held):
    assert store_config.admin_env_names() == frozenset(SECRETS)
    assert store_config.without_admin_secrets({"A": "1", **SECRETS}) == {"A": "1"}


def test_the_claude_author_starts_with_no_store_secret(secrets_held, tmp_path):
    from auto_research.sandbox.coder.claude_sdk import _build_options

    for env in (None, {"PATH": "/usr/bin", "T5_PG_PASSWORD": "pg-admin-pass"}):
        options = _build_options(worktree=tmp_path / "wt", coder_model="claude-sonnet-5", system_prompt="p",
                                 escalation_server={}, max_turns=1, worktree_base=tmp_path / "authoring", env=env)
        child = _child_env(options.env)
        assert not any(child.get(name) for name in SECRETS), options.env


def test_the_codex_author_starts_with_no_store_secret(secrets_held, tmp_path):
    from auto_research.sandbox.coder.codex_sdk import CodexBackend

    captured: dict = {}

    class _Client:
        def start(self): pass
        def initialize(self): pass
        def account_login_start(self, params): pass

    def factory(config, _approve):
        captured["env"] = dict(config.env or {})
        return _Client()

    for env in (None, {"PATH": "/usr/bin", "T5_S3_SECRET": "admin-secret-value"}):
        backend = CodexBackend(
            member=RoleModelConfig(name="m", provider="openai", model="gpt-5.6-terra", harness="codex",
                                   key_env="OPENAI_API_KEY"),
            worktree=tmp_path / "wt", system_prompt="p", env=env, client_factory=factory)
        backend._build_client()
        child = _child_env(captured["env"])
        assert not any(child.get(name) for name in SECRETS), captured["env"]


def test_the_jail_launcher_unsets_every_store_secret_whatever_env_it_is_spawned_with(secrets_held, tmp_path):
    """The Claude SDK's version check spawns the CLI with this process's whole environment (no
    ``options.env``): the launcher itself unsets each admin secret before the jailed binary runs."""
    import os
    import subprocess

    from auto_research.sandbox import jail
    from auto_research.sandbox.jail import JailPolicy, write_launcher

    if not jail.jail_available()[0]:
        pytest.skip("the OS jail is required and bwrap cannot run here")
    binary = tmp_path / "bin" / "cli"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nenv\n")
    binary.chmod(0o755)
    script = write_launcher(tmp_path / "cli-jailed", str(binary),
                            JailPolicy(ro=(), rw=(str(tmp_path),), network=False, home=None, label="secrets"))
    done = subprocess.run([str(script), "-v"], env=dict(os.environ), capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "PATH=" in done.stdout  # the binary ran and printed its environment
    assert not any(name in done.stdout or value in done.stdout for name, value in SECRETS.items())
