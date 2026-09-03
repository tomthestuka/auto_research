"""Per-run workspace layout and per-agent memory utilities."""

from __future__ import annotations

from pathlib import Path

from auto_research.env import runtime_data_dir


def run_dir(run_id: str) -> Path:
    return runtime_data_dir() / "runs" / run_id


def agent_dir(run_id: str, agent_key: str) -> Path:
    return run_dir(run_id) / agent_key


def figures_dir(run_id: str, agent_key: str) -> Path:
    d = agent_dir(run_id, agent_key) / "figures"
    d.mkdir(parents=True, exist_ok=True)
    return d


def report_dir(run_id: str) -> Path:
    """Return (and ensure parent + figures/ exist for) the reviewer's report directory."""
    d = agent_dir(run_id, "reviewer") / "report"
    (d / "figures").mkdir(parents=True, exist_ok=True)
    return d


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
    """Path of one inter-LLM report JSONL inside an agent's working folder (V-4).

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
    return Path(agent_dir_path) / f"{role}_round{round_n}_report.jsonl"


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
