"""Where the project's configuration lives — one answer, resolved when it is asked for.

Every configured fact about a run comes from four YAML files: the orchestration caps and paths,
the model roles, the observation-database location, and the code-gate scan lists. Until now each
consumer computed their location itself, relative to its own source file, so the configuration
directory was not a concept the code had — it was a coincidence that eleven modules agreed. That
made "run this pipeline against a different configuration" inexpressible: six independent readers
of the orchestration file alone, four of them caching their answer for the lifetime of the
process, so patching one of them silently left five reading the original.

This module is that missing concept, and it is deliberately the smallest thing that can be one:
the directory, and the four files inside it.

**Overriding it is a per-process decision, by design.** ``AUTO_RESEARCH_CONFIG_DIR`` is read from
the environment, so a caller that needs a run configured differently launches it as its own
process with that variable set, and every reader inside it — including the sandbox REPL, the
author harness and the gate's test runner, which inherit the environment — agrees without being
told. A process-local mechanism could not do that: those three are subprocesses, and the two that
matter most are not even Python. Setting the variable *before* the process starts is what also
makes the existing caches harmless, and the readers here cache on the resolved path rather than on
nothing, so a test that moves the directory is not served a stale answer either.

Unset — which is every ordinary run, the CLI, the notebook and the whole test suite — resolves to
the repository's own ``config/``, exactly as before.

The installer is deliberately not routed through here. It writes the live configuration *from* the
tracked ``.example`` templates, which live in the repository whatever a run is configured with, so
pointing it at an override directory would have it read templates that are not there.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Set this to a directory holding ``orchestration.yaml``, ``models.yaml``, ``observations.yaml``
#: and ``code_gate.yaml`` to configure a process against it instead of the repository's own.
CONFIG_DIR_ENV = "AUTO_RESEARCH_CONFIG_DIR"

_REPO_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def repo_config_dir() -> Path:
    """The repository's own ``config/`` — the default, and where the templates live."""
    return _REPO_CONFIG_DIR


def config_dir() -> Path:
    """The configuration directory this process reads.

    Resolved on every call rather than captured at import, so a caller can set the variable and a
    reader will see it; in the ordinary case the variable is unset and this is the repository's
    own ``config/``.
    """
    override = os.environ.get(CONFIG_DIR_ENV)
    return Path(override).expanduser() if override else _REPO_CONFIG_DIR


def orchestration_yaml() -> Path:
    """Iteration caps, runtime paths, filesystem allow-list, feature flags."""
    return config_dir() / "orchestration.yaml"


def models_yaml() -> Path:
    """Role → model members: which model sits in which seat."""
    return config_dir() / "models.yaml"


def observations_yaml() -> Path:
    """Where the observation database is."""
    return config_dir() / "observations.yaml"


def code_gate_yaml() -> Path:
    """The mergeability gate's scan lists."""
    return config_dir() / "code_gate.yaml"


__all__ = [
    "CONFIG_DIR_ENV",
    "code_gate_yaml",
    "config_dir",
    "models_yaml",
    "observations_yaml",
    "orchestration_yaml",
    "repo_config_dir",
]
