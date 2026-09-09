"""Put any model in any seat, for the length of one test, without editing the configuration.

**Why this exists.** Every live test that drives a real role resolves its model the way production
does — ``load_registry().resolve("analyst_lead")`` — so it measures whatever the machine-local
configuration happens to name. That is right for a regression test and useless for a *probe*: the
science sweep runs members no configuration names, and the question "can this model hold this
seat" cannot be asked by a test whose seats are fixed. The alternative each test reached for
instead was to build a ``ModelHandle`` by hand, which skips the role layer — the 2026-08-06
lesson, where live tests calling ``provider.call`` directly never executed ``roles.py`` and a
defect above the adapter stayed invisible for a week.

**The mechanism is the configuration pointer, not an injection seam.** ``AUTO_RESEARCH_CONFIG_DIR``
already exists (``auto_research.config_paths``) and every reader of every configured fact goes
through it. So a seat override here is a *whole configuration directory*: the repository's own
three other files copied byte for byte, and a ``models.yaml`` with the named roles replaced. Code
under test then resolves the overridden seat through its ordinary path, knowing nothing about this
module, and the media guard, key rotation, retry and prompt loading are all still in front of it.

Three properties make it safe to point a *running* process at a new directory:

* ``load_registry()`` holds no cache and resolves ``models_yaml()`` on every call.
* The four cached configuration readers key on the **resolved path**, so they are not served the
  previous directory's answer.
* The caches that hold a *value* rather than a path (the ReAct loop caps, the structured-retry
  cap, the jail flags) are unaffected **because orchestration is copied unchanged** — an override
  never varies a cap, only which model sits where. Keep it that way: a helper that also edited
  ``orchestration.yaml`` would be silently ineffective for anything already read once.

Two ways in, and they compose:

``seated(...)``
    A context manager, for a test that knows which member it wants::

        with seated(analyst_lead=seat("openai", "gpt-5.6-terra")):
            lead = AnalystLeadAgent(model=load_registry().resolve("analyst_lead").handles[0], ...)

``AUTO_RESEARCH_SEATS`` / ``--seats``
    A whole-session override applied by ``tests/conftest.py``, so **any** existing live test runs
    against any seat assignment with no edit to that test::

        AUTO_RESEARCH_SEATS='analyst_lead=openai:gpt-5.6-terra,reviewer=dashscope:qwen3.8-max' \
            AUTO_RESEARCH_LIVE_FIGURE_TESTS=1 pytest tests/test_live_figure_vision.py -s

Parameters travel with the seat, and by default they are the **sweep's pinned settings** rather
than a vendor default, because a probe of a batch's condition has to send the batch's request: the
effort level is what the campaign pins, and intra-model effort spread dwarfs every between-model
gap. ``benchmarks.science_bench.arms`` owns those constants; this module reads them so there is
one definition and not a second one that drifts.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

import yaml

from auto_research.config_paths import CONFIG_DIR_ENV, repo_config_dir

#: Environment variable holding a whole-session seat assignment, read by ``tests/conftest.py``.
SEATS_ENV = "AUTO_RESEARCH_SEATS"

#: The configuration files a directory must hold to configure a process. ``models.yaml`` is the
#: one an override rewrites; the rest are copied so nothing else about the run differs.
_CONFIG_FILES = ("orchestration.yaml", "models.yaml", "observations.yaml", "code_gate.yaml")


@dataclass(frozen=True)
class SeatOverride:
    """One model, and what it may be sent — a ``models.yaml`` member waiting to be written."""

    provider: str
    model: str
    vision: bool = True
    documents: bool = True
    parameters: dict[str, Any] = field(default_factory=dict)
    priceMTk: dict[str, float] = field(default_factory=dict)
    harness: str | None = None
    base_url: str | None = None
    key_env: str | None = None

    def member(self) -> dict[str, Any]:
        """This override as one ``models.yaml`` member entry.

        Named for its model, as both the live configuration and the sweep's own roster do: a model
        knowing which model it is leaks nothing, and it keeps a failure legible in a log.
        """
        name = "".join(c if c.isalnum() else "_" for c in self.model.lower()).strip("_")
        entry: dict[str, Any] = {
            "name": name or "probe",
            "provider": self.provider,
            "model": self.model,
            "vision": self.vision,
            "documents": self.documents,
        }
        if self.parameters:
            entry["parameters"] = dict(self.parameters)
        if self.priceMTk:
            entry["priceMTk"] = dict(self.priceMTk)
        for key in ("harness", "base_url", "key_env"):
            value = getattr(self, key)
            if value is not None:
                entry[key] = value
        return entry


def pinned_parameters(provider: str) -> dict[str, Any]:
    """The campaign's pinned per-vendor call settings for ``provider``.

    Read from ``benchmarks.science_bench.arms`` rather than restated, so a probe cannot drift from
    the batch it is probing. Falls back to empty when the benchmark package is not importable,
    which keeps this module usable on its own.
    """
    try:
        from benchmarks.science_bench import arms
    except Exception:  # noqa: BLE001 — a probe must not depend on the benchmark being importable
        return {}
    return dict(arms.pinned_parameters(provider))


def seat(
    provider: str,
    model: str,
    *,
    vision: bool = True,
    documents: bool = True,
    parameters: Mapping[str, Any] | None = None,
    **extra: Any,
) -> SeatOverride:
    """One seat override, carrying the campaign's pinned settings unless told otherwise."""
    return SeatOverride(
        provider=provider,
        model=model,
        vision=vision,
        documents=documents,
        parameters=dict(parameters) if parameters is not None else pinned_parameters(provider),
        **extra,
    )


def from_seat(sweep_seat: Any) -> SeatOverride:
    """A :class:`benchmarks.science_bench.arms.Seat` as an override, flags and prices included."""
    return SeatOverride(
        provider=sweep_seat.provider,
        model=sweep_seat.model,
        vision=sweep_seat.vision,
        documents=sweep_seat.documents,
        parameters=sweep_seat.parameters(),
        priceMTk=sweep_seat.price(),
    )


def parse_seats(spec: str) -> dict[str, list[SeatOverride]]:
    """Parse ``role=provider:model[+role2=...]`` into overrides, for the env var and the CLI flag.

    Grammar, kept deliberately small::

        analyst_lead=openai:gpt-5.6-terra,reviewer=dashscope:qwen3.8-max
        analyst_worker=anthropic:claude-sonnet-5|dashscope:qwen3.7-plus

    Commas separate roles; ``|`` separates the members of one role, so a two-member group such as
    the analyst-worker pair is expressible. A trailing ``!novision`` or ``!nodocs`` on a member
    clears that flag, since a member's declared capability is what the call-time guard reads and a
    probe of a text-only model must be able to say so.
    """
    overrides: dict[str, list[SeatOverride]] = {}
    for clause in (part.strip() for part in spec.split(",")):
        if not clause:
            continue
        if "=" not in clause:
            raise ValueError(f"seat spec needs role=provider:model, got {clause!r}")
        role, members = clause.split("=", 1)
        role = role.strip()
        if not role:
            raise ValueError(f"seat spec has an empty role: {clause!r}")
        parsed: list[SeatOverride] = []
        for member in (m.strip() for m in members.split("|")):
            if not member:
                raise ValueError(f"seat spec has an empty member: {clause!r}")
            flags = member.split("!")
            body = flags[0].strip()
            if ":" not in body:
                raise ValueError(f"seat member needs provider:model, got {body!r}")
            provider, model = (piece.strip() for piece in body.split(":", 1))
            if not provider or not model:
                raise ValueError(f"seat member needs provider:model, got {body!r}")
            unknown = {flag.strip() for flag in flags[1:]} - {"novision", "nodocs"}
            if unknown:
                raise ValueError(f"unknown seat flags {sorted(unknown)} in {member!r}")
            parsed.append(
                seat(
                    provider,
                    model,
                    vision="novision" not in flags[1:],
                    documents="nodocs" not in flags[1:],
                )
            )
        overrides[role] = parsed
    if not overrides:
        raise ValueError(f"seat spec named no roles: {spec!r}")
    return overrides


def models_config(
    overrides: Mapping[str, list[SeatOverride]], source: Mapping[str, Any]
) -> dict[str, Any]:
    """``source``'s roles with the overridden ones replaced. Every other role is left alone.

    Untouched roles are copied rather than dropped because the registry refuses a configuration
    missing any required role, and because a role nobody is probing must stay what production
    runs.
    """
    roles = {name: dict(group) for name, group in (source.get("roles") or {}).items()}
    for role, members in overrides.items():
        roles[role] = {"members": [member.member() for member in members]}
    return {"roles": roles}


def write_config_dir(
    destination: Path, overrides: Mapping[str, list[SeatOverride]], *, source: Path | None = None
) -> Path:
    """Build a configuration directory at ``destination`` with the seats overridden.

    The three files that are not ``models.yaml`` are copied byte for byte: an override varies who
    sits in a seat and nothing else, which is also what keeps the value-caching configuration
    readers correct in a process that has already read them.
    """
    origin = source or repo_config_dir()
    destination.mkdir(parents=True, exist_ok=True)
    for name in _CONFIG_FILES:
        candidate = origin / name
        if candidate.is_file():
            shutil.copyfile(candidate, destination / name)
    with (origin / "models.yaml").open("r", encoding="utf-8") as stream:
        base = yaml.safe_load(stream) or {}
    with (destination / "models.yaml").open("w", encoding="utf-8") as stream:
        yaml.safe_dump(models_config(overrides, base), stream, sort_keys=False)
    return destination


@contextmanager
def seated(
    _overrides: Mapping[str, SeatOverride | list[SeatOverride]] | None = None,
    /,
    *,
    config_dir: Path | None = None,
    **kwargs: SeatOverride | list[SeatOverride],
) -> Iterator[Path]:
    """Run the block with the named seats overridden, then restore the previous configuration.

    Accepts roles as keyword arguments (``seated(analyst_lead=seat(...))``) or as one mapping, for
    role names that are not identifiers. Yields the configuration directory in use, so a test can
    read back exactly what it configured — the artifact, not the intention.
    """
    requested: dict[str, SeatOverride | list[SeatOverride]] = dict(_overrides or {})
    requested.update(kwargs)
    normalised = {
        role: (list(value) if isinstance(value, list) else [value])
        for role, value in requested.items()
    }
    if not normalised:
        raise ValueError("seated() needs at least one seat override")

    temporary = config_dir is None
    target = Path(config_dir) if config_dir is not None else Path(mkdtemp(prefix="seats-"))
    previous = os.environ.get(CONFIG_DIR_ENV)
    try:
        write_config_dir(target, normalised)
        os.environ[CONFIG_DIR_ENV] = str(target)
        yield target
    finally:
        if previous is None:
            os.environ.pop(CONFIG_DIR_ENV, None)
        else:
            os.environ[CONFIG_DIR_ENV] = previous
        if temporary:
            shutil.rmtree(target, ignore_errors=True)


__all__ = [
    "SEATS_ENV",
    "SeatOverride",
    "from_seat",
    "models_config",
    "parse_seats",
    "pinned_parameters",
    "seat",
    "seated",
    "write_config_dir",
]
