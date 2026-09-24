"""Report serialization, and the TeX log reader the compiler step uses."""

from __future__ import annotations

import re
from pathlib import Path

from auto_research.schemas.report import ResearchReport


def write_report_jsonl(report: ResearchReport, path: Path) -> Path:
    """Write the machine report as a single JSONL line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json() + "\n", encoding="utf-8")
    return path


# TeX prints the offending source line as `l.<number> <text>` a line or two below its `!`
# message; nothing else in the log identifies what failed.
_TEX_CONTEXT = re.compile(r"^l\.\d+\s")


def _errors_with_context(stdout: str, *, lookahead: int = 4) -> list[str]:
    """Pair each ``!`` error line with the ``l.<n>`` source line that follows it.

    The ``!`` line alone is usually not actionable — TeX says *"! Undefined control sequence."*
    and only the context line names the macro (observed live on Q21, 2026-08-10, where two
    attempts failed identically on ``\\lesssim``). ``lookahead`` bounds the search so an error with
    no context yields the bare message rather than swallowing an unrelated later line.
    """

    lines = stdout.splitlines()
    errors: list[str] = []
    for i, line in enumerate(lines):
        if not line.startswith("!"):
            continue
        message = line.strip()
        for candidate in lines[i + 1 : i + 1 + lookahead]:
            if candidate.startswith("!"):
                break  # the next error owns everything past here
            if _TEX_CONTEXT.match(candidate):
                message = f"{message} [{candidate.strip()}]"
                break
        errors.append(message)
    return errors
