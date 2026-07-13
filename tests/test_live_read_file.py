"""Env-gated LIVE test: the real analyst lead can read a long toolkit source file end-to-end
through the read-by-line ``read_file`` tool (F8), paging until it reaches the last line.

Activate with:
    AUTO_RESEARCH_LIVE_READ_FILE_TESTS=1 pytest tests/test_live_read_file.py -q

What it proves: we build the actual ``analyst_lead`` model + its ``ReadOnlyToolProvider`` over
``toolkit_root`` and ask it to read the whole of ``xspec_utils/lib/xspec_help.py`` (≈1.5k lines,
longer than one page). We assert on the recorded tool stream — the union of line ranges the lead
actually pulled via ``read_file`` reaches ``total_lines`` — i.e. the silent-truncation blindspot
that bit #12 is gone: the lead can page through long source rather than reading the first window
and stopping. Writes a JSONL + Markdown summary under ``logs/dev/read_file/``.
"""

import asyncio
import json
import os
import re
import time
from pathlib import Path

import pytest
from dotenv import load_dotenv

from auto_research.agents.react import ReadOnlyToolProvider
from auto_research.agents.roles import AnalystLeadAgent
from auto_research.graph.runtime import load_orchestration_config
from auto_research.models import ModelMessage, load_registry

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_READ_FILE_TESTS") != "1",
    reason="set AUTO_RESEARCH_LIVE_READ_FILE_TESTS=1 to run the live read_file test (spends tokens)",
)

_LINE_NO = re.compile(r"^\s*(\d+)\t")
_FOOTER_TOTAL = re.compile(r"of (\d+)[;\]]")


class _RecordingProvider(ReadOnlyToolProvider):
    """A ReadOnlyToolProvider that records what each read_file call actually returned."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.read_calls: list[dict] = []

    async def execute(self, name, arguments):
        out = await super().execute(name, arguments)
        if name == "read_file" and not out.is_error:
            nums = [int(m.group(1)) for m in (_LINE_NO.match(ln) for ln in out.text.splitlines()) if m]
            total_m = _FOOTER_TOTAL.search(out.text)
            self.read_calls.append(
                {
                    "args": dict(arguments),
                    "first_line": nums[0] if nums else None,
                    "last_line": nums[-1] if nums else None,
                    "total_seen": int(total_m.group(1)) if total_m else None,
                }
            )
        return out


def test_live_lead_reads_whole_xspec_help():
    load_dotenv()
    toolkit_root = Path(load_orchestration_config().get("toolkit_root", "")).expanduser()
    target = toolkit_root / "xspec_utils" / "lib" / "xspec_help.py"
    if not target.exists():
        pytest.skip(f"{target} not present on this host")

    total_lines = len(target.read_text(errors="replace").splitlines())
    lead = AnalystLeadAgent(model=load_registry().resolve("analyst_lead").handles[0])
    provider = _RecordingProvider(roots=[toolkit_root])

    system = (
        "You are the analyst lead inspecting toolkit source. Use the read_file tool to read the "
        "ENTIRE file you are given. It is longer than one read window: after each read, if the "
        "output ends with a note that more lines remain, call read_file again with the offset it "
        "names, and keep going until you have read the final line. Do not stop early. When you have "
        "read the whole file, reply with the total number of lines and the name of the LAST "
        "top-level function defined in it."
    )
    task = f"Read the whole file at {target} and report its length and last top-level function."

    async def run():
        return await lead._react(
            seed_messages=[
                ModelMessage(role="system", content=system),
                ModelMessage(role="user", content=task),
            ],
            tools=provider,
            action="inspect",
            cap=12,
        )

    asyncio.run(run())
    # The lead's final answer is the terminal no-tool turn (_react breaks before appending it).
    final_answer = str(getattr(lead.last_response, "content", "") or "")

    max_line = max((c["last_line"] or 0) for c in provider.read_calls) if provider.read_calls else 0
    covered = set()
    for c in provider.read_calls:
        if c["first_line"] and c["last_line"]:
            covered.update(range(c["first_line"], c["last_line"] + 1))

    # Log for the record (logs/dev/, per the dev-artifact convention).
    logdir = Path("logs/dev/read_file")
    logdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    (logdir / f"lead_reads_xspec_help_{stamp}.jsonl").write_text(
        "\n".join(json.dumps(c) for c in provider.read_calls) + "\n", encoding="utf-8"
    )
    (logdir / f"lead_reads_xspec_help_{stamp}.md").write_text(
        f"# Lead reads xspec_help.py (F8 live)\n\n"
        f"- file total_lines: {total_lines}\n"
        f"- read_file calls: {len(provider.read_calls)}\n"
        f"- max line reached: {max_line}\n"
        f"- distinct lines covered: {len(covered)}/{total_lines}\n\n"
        + "\n".join(
            f"  - call {i+1}: offset={c['args'].get('offset', 1)} "
            f"limit={c['args'].get('limit', 'default')} → lines {c['first_line']}–{c['last_line']}"
            for i, c in enumerate(provider.read_calls)
        )
        + f"\n\n## Lead's final answer\n\n{final_answer}\n",
        encoding="utf-8",
    )
    print(
        f"\n[read_file live] {len(provider.read_calls)} read_file calls; "
        f"max line {max_line}/{total_lines}; covered {len(covered)}/{total_lines}\n"
        f"[read_file live] lead's final answer:\n{final_answer}"
    )

    assert provider.read_calls, "the lead never called read_file"
    assert len(provider.read_calls) >= 2, "file needs paging but the lead read only one window"
    # It reached the end: the last line it pulled is the file's final line.
    assert max_line >= total_lines, (
        f"lead stopped at line {max_line} of {total_lines} — did not page to the end"
    )
