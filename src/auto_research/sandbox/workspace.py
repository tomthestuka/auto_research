"""Per-run workspace layout and per-agent memory utilities."""

from __future__ import annotations

from pathlib import Path

from auto_research.env import runtime_data_dir


def run_dir(run_id: str) -> Path:
    """The run's WORKSPACE: what the agents produce, and the only part they may read.

    Figures, reports, per-worker scratch and the authoring worktree. On the filesystem
    allow-list, because an agent that cannot read back the figure it just wrote cannot cite it.
    """
    return runtime_data_dir() / "runs" / run_id


def ledger_dir(run_id: str) -> Path:
    """The run's RECORD: what the harness writes *about* the run, deliberately out of reach.

    Holds ``monitor.jsonl``, ``monitor.md``, ``backend.log`` and ``checkpoint.sqlite`` — the
    event ledger, its rendering, the backend's stdout and the serialized graph state. Kept out
    of :func:`run_dir` so that narrowing the allow-list to ``runs/`` removes them from BOTH
    enforcement layers at once: `PathGuard` for the chat roles, and the bwrap mount plan for the
    coder's subprocess, whose implicit reads a path rule cannot see.

    **Why this is separated at all.** A reviewer read 191 KB of this run's own ledger into the
    trajectory that then failed its compose (2026-09-14), and a coder grepped the run record 31
    times in one session. The same class is well documented elsewhere: openai/codex#27131 (a
    conversation reaching 1,239,690 tokens after reading its own rollouts, including an
    accidental `*.json*` glob that matched `.jsonl`), and codex#40493, which names it
    "self-state reads" and measures it consuming 61% of a context window.

    Supervision is unaffected and is the point: `/status` reads this through
    ``MonitorToolProvider``, which is constructed with the path and exposes no tool that takes
    one — a narrow structured channel rather than filesystem reach. Denying self-knowledge
    entirely is its own failure mode (claude-code#83074: an agent with no way to check its own
    history invented a fictional session and built real constraints around it), so the record
    stays readable through that channel, and only the unstructured path closes.
    """
    return runtime_data_dir() / "ledgers" / run_id


def record_beside(run_directory: Path, name: str) -> Path:
    """Where a record file lives, derived from the RUN DIRECTORY rather than from config.

    `run_dir` and `ledger_dir` are siblings under the runtime dir, so the record directory is
    reachable from the workspace path alone — which is what callers holding a `Path` need.

    **Use this, not :func:`resolve_record`, wherever the run directory was handed in.** A trial
    runs with its own `AUTO_RESEARCH_CONFIG_DIR` pointing `runtime_data_dir()` at a private
    tree, so a helper that consults ambient config resolves to the *launcher's* runtime dir
    instead of the run's. That is not hypothetical: it failed the first slot-5 relaunch at spawn
    (2026-09-14), trying to open `…/test_runtime/ledgers/<id>/backend.log` for a run living
    under `…/ws/<id>/runtime/`. The code it replaced took the directory it was given and
    appended a filename, depending on nothing.
    """
    legacy = run_directory / name
    if legacy.exists():
        return legacy
    target = run_directory.parent.parent / "ledgers" / run_directory.name / name
    # The directory is created HERE, by the function that decides the location, so a caller
    # cannot get the path right and the directory wrong. Two separate launchers made exactly
    # that mistake within an hour of the split (`graph.detached._default_launcher` and
    # `science_bench.trial`, the second after the first had been fixed): opening the log for
    # append is the first thing a spawned backend does, so a missing parent is an immediate
    # exit 1 with no run and no ledger to explain it. A stray empty directory from a read is a
    # far smaller cost than a launcher that silently cannot start.
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def resolve_record(run_id: str, name: str) -> Path:
    """Where a record file lives, tolerating the pre-2026-09-14 layout.

    New runs write under :func:`ledger_dir`. Every batch already on disk — including the ones
    behind published numbers — has these files inside ``run_dir``, and offline scoring reads
    them long after the run. So a reader resolves to the legacy path when that is the one that
    exists, and to the new path otherwise (which is also the path a fresh run will create).
    """
    legacy = run_dir(run_id) / name
    if legacy.exists():
        return legacy
    return ledger_dir(run_id) / name


def agent_dir(run_id: str, agent_key: str) -> Path:
    return run_dir(run_id) / agent_key


def figures_dir(run_id: str, agent_key: str) -> Path:
    d = agent_dir(run_id, agent_key) / "figures"
    d.mkdir(parents=True, exist_ok=True)
    return d


def report_dir(run_id: str) -> Path:
    """Return (and ensure parent + figures/ exist for) the reviewer's report directory.

    Beside the ledger, outside every role's allow-list (human 2026-09-25): a role that could read
    a report back with ``read_file`` would mint its own typed numbers as a receipt of its own.
    """
    d = ledger_dir(run_id) / "reports" / "reviewer" / "report"
    (d / "figures").mkdir(parents=True, exist_ok=True)
    return d


def report_record_path(agent_dir_path: Path, filename: str) -> Path:
    """Where a role's report JSONL lives: beside the ledger, outside every allow-list.

    A role's report is written by the harness from what the model typed. Inside the workspace it
    could be read back (``read_file``) and the read minted as the role's own receipt, grounding
    the very numbers the report check refuses (found 2026-09-25; human ruling: move the files out
    of reach). Derived from the agent folder, like :func:`record_beside`, so a trial's private
    runtime tree resolves to its own ledgers. An agent folder outside a ``runs/`` tree — a
    standalone role with no allow-list to escape — keeps the file in that folder.
    """
    agent_dir_path = Path(agent_dir_path)
    run = agent_dir_path.parent
    if run.parent.name != "runs":
        return agent_dir_path / filename
    target = run.parent.parent / "ledgers" / run.name / "reports" / agent_dir_path.name / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def memory_path(run_id: str, agent_key: str) -> Path:
    """Return (and ensure parent exists for) the agent's memory.md path."""
    d = agent_dir(run_id, agent_key)
    d.mkdir(parents=True, exist_ok=True)
    return d / "memory.md"


def create_run_workspace(run_id: str, agent_keys: list[str]) -> dict[str, Path]:
    """Create workspace folders for all agents and return {agent_key: agent_dir}."""
    result: dict[str, Path] = {}
    for key in agent_keys:
        d = agent_dir(run_id, key)
        d.mkdir(parents=True, exist_ok=True)
        (d / "figures").mkdir(exist_ok=True)
        result[key] = d
    return result


def round_report_path(agent_dir_path: Path, *, role: str, model: str, round_n: int) -> Path:
    """Path of one inter-LLM report JSONL for an agent, beside the ledger (V-4, 2026-09-25).

    One file per debate round (``round_n`` counts monotonically across rework iterations, so
    nothing collides and the trail is continuous). The agent folder already disambiguates which
    worker, so the name carries ``<role>_round<n>``. This is the durable, ReAct-refined report the
    agent writes via ``edit_report`` and reads back via ``review_report``; it is distinct from the
    agent's private ``memory.md``.

    ``model`` is accepted and unused: the name used to carry a model slug, and a filename is
    readable by any agent whose allow-list covers the run directory — a coder's ``grep -rl`` over
    the run dir returned ``analyst_lead_claude-opus-5_round1_report.jsonl`` on 2026-09-03. Which
    member wrote a report belongs in the ledger, the human's record.
    """
    return report_record_path(Path(agent_dir_path), f"{role}_round{round_n}_report.jsonl")


def append_memory(path: Path, text: str) -> None:
    """Append text to an agent's memory.md (bare terminal recording)."""
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)
        if not text.endswith("\n"):
            fh.write("\n")


def read_memory(path: Path) -> str:
    """Return the full contents of memory.md, or empty string if absent."""
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")
