"""The report as one text document, edited like a file (A5/A7, human rulings 2026-10-18).

"Make edit_report really an edit", then "we simply copy Claude Code / Codex's design": the draft
is one text document with a labelled block per report field, and two tools change it —
``write_report{content}`` replaces the whole document (Claude Code's Write) and
``edit_report{old_string, new_string, replace_all}`` replaces an exact match in it (Edit). Before,
every call carried the whole report and overwrote the last: run 32814459's worker 0 replayed 877k
characters of drafts, and a call that left out ``cited_figures`` dropped every placed figure.

One implementation for every role that writes a report — analyst worker and lead, reviewer,
librarian. The document is rendered from the stored report and parsed back into it, so the stored
report stays the Pydantic model and ``turn_end``, provenance and the graph read it unchanged.

The document, for an analyst worker::

    # summary
    The fit converged on both observations.
    # status
    succeeded
    # findings
    - first result, citing [analyst_worker_0:r3]
    - second result, which runs on
      to a second line
    # figures
    - [self:2]

A header is a line ``# <block>`` naming one of the role's blocks; any other line belongs to the
block above it. List items start with ``- ``; an item's further lines are indented two spaces.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

WRITE_REPORT_TOOL = "write_report"


@dataclass(frozen=True)
class Block:
    """One labelled block of the document: its header, the report field, how it is written."""

    name: str
    field: str
    kind: str  # text | line | value | bool | list | figures | handles | keyvalue | claims | sections | tables
    hint: str


#: The analysts' blocks, in document order; a role keeps those its surface declares (figures always).
ANALYST_BLOCKS: tuple[Block, ...] = (
    Block("summary", "summary", "text",
          "a briefing of 3–5 sentences: what was asked, what was done, the headline result, the outcome"),
    Block("status", "status", "value", "the outcome, one word"),
    Block("rework", "rework", "bool",
          "true to send the analysts back for one more round, false when this report is the result"),
    Block("rationale", "rationale", "text", "why another round is warranted (with rework true)"),
    Block("what_to_probe", "what_to_probe", "text",
          "what the analysts do next, handed to every worker verbatim (with rework true)"),
    Block("findings", "findings", "list",
          "the results: one '- ' item per result, each citing the receipts its numbers come from as [handle]"),
    Block("figures", "cited_figures", "figures",
          "the figures placed: one '- [source:index]' line per figure, by the label it was shown with; "
          "the k-th line is 'Figure k' in your prose"),
    Block("uncertainty", "uncertainty", "text", "the dominant caveats"),
    Block("tool_request", "tool_request", "keyvalue",
          "only for a capability the toolkit lacks or a tool that misbehaves: 'key: value' lines "
          "(request_kind, proposed_tool_name, science_context, expected_behavior, observed_issue, "
          "notes) and one 'validation_data: MISSION OBSID' line per dataset"),
)

REVIEWER_BLOCKS: tuple[Block, ...] = (
    Block("assign_to", "assign_to", "value", "who receives this report: librarian, analysts or human"),
    Block("accepted", "accepted", "bool", "true when the analysis answers the request, else false"),
    Block("rationale", "rationale", "text", "the evidence the decision rests on"),
    Block("follow_up", "follow_up", "text", "for the librarian or the analysts: their task, verbatim"),
    Block("title", "title", "line", "the final report's title, one line"),
    Block("abstract", "abstract", "text", "the final report's abstract"),
    Block("sections", "sections", "sections", "the body: '## heading' then that section's text, per section"),
    Block("key_findings", "key_findings", "list", "one '- ' item per finding"),
    Block("figures", "figures", "handles",
          "one '- handle' line per figure receipt; the k-th line is 'Figure k'"),
    Block("tables", "tables", "tables",
          "'## caption' per table, then 'receipt: handle', or 'columns: a | b' and one '| x | y |' "
          "line per row; the k-th table is 'Table k'"),
)

LIBRARIAN_BLOCKS: tuple[Block, ...] = (
    Block("summary", "summary", "text",
          "a briefing of 3–5 sentences: what you searched and read, and the headline of what it establishes"),
    Block("status", "status", "value", "the outcome, one word: succeeded or failed"),
    Block("findings", "findings", "claims",
          "one '- claim' item per claim your sources support, then an indented "
          "'support: lib:p1, lib:w2' line naming the documents behind it"),
    Block("gaps", "gaps", "list", "one '- ' item per question no document answered or source you could not read"),
)

_TOOL_REQUEST_KEYS = (
    "request_kind", "proposed_tool_name", "science_context", "expected_behavior",
    "observed_issue", "notes",
)
_HEADER_LIKE = re.compile(r"^#\s+[A-Za-z_][A-Za-z0-9_ ]*$")
_HEADER_WORD = re.compile(r"^#(?!#)\s*([A-Za-z_]+)")
_FIGURE_LINE = re.compile(r"^\[?\s*([A-Za-z0-9_.\-]+)\s*:\s*(\d+)\s*\]?$")
_CONTEXT = 3


def blocks_for(fields: Sequence[str], master: Sequence[Block], *, always: Sequence[str] = ()) -> tuple[Block, ...]:
    """The blocks of ``master`` whose field this surface writes."""
    return tuple(b for b in master if b.field in fields or b.field in always)


# ── render ─────────────────────────────────────────────────────────────────────


def _item_lines(text: str) -> list[str]:
    lines = str(text).split("\n")
    return ["- " + lines[0]] + [("  " + line) if line.strip() else "" for line in lines[1:]]


def _block_body(block: Block, value: Any) -> list[str]:
    if value is None:
        return []
    kind = block.kind
    if kind in ("text", "line", "value"):
        return str(value).split("\n") if str(value) != "" else []
    if kind == "bool":
        return ["true" if value else "false"]
    if kind == "list":
        return [line for item in value for line in _item_lines(item)]
    if kind == "figures":
        return [f"- [{c['source']}:{c['index']}]" for c in value]
    if kind == "handles":
        return [f"- {h}" for h in value]
    if kind == "claims":
        out: list[str] = []
        for finding in value:
            out.extend(_item_lines(finding.get("claim", "")))
            out.append("  support: " + ", ".join(finding.get("support") or []))
        return out
    if kind == "keyvalue":
        out = []
        for key in _TOOL_REQUEST_KEYS:
            if value.get(key) not in (None, ""):
                lines = str(value[key]).split("\n")
                out.append(f"{key}: {lines[0]}")
                out.extend(("  " + line) if line.strip() else "" for line in lines[1:])
        for entry in value.get("validation_data") or []:
            out.append(
                f"validation_data: {entry.get('mission', '')} {entry.get('obsid', '')}"
                + (" synthetic" if entry.get("synthetic") else "")
            )
        return out
    if kind == "sections":
        out = []
        for section in value:
            out.append(f"## {section.get('heading', '')}")
            out.extend(str(section.get("body", "")).split("\n"))
        return out
    if kind == "tables":
        out = []
        for table in value:
            out.append(f"## {table.get('caption', '')}")
            if table.get("receipt"):
                out.append(f"receipt: {table['receipt']}")
            if table.get("columns"):
                out.append("columns: " + " | ".join(table["columns"]))
            for row in table.get("rows") or []:
                out.append("| " + " | ".join(row) + " |")
        return out
    raise ValueError(f"unknown block kind {kind!r}")  # pragma: no cover — a typo in a block table


def render(values: Mapping[str, Any], blocks: Sequence[Block]) -> str:
    """The document for ``values`` (field → JSON value): every block, in order, empty ones too."""
    lines: list[str] = []
    for block in blocks:
        lines.append(f"# {block.name}")
        lines.extend(_block_body(block, values.get(block.field)))
    return "\n".join(lines) + "\n"


# ── parse ──────────────────────────────────────────────────────────────────────


def _items(lines: list[str], block: str, problems: list[str]) -> list[list[str]]:
    """Split a list block into items: '- ' starts one, any other line continues it."""
    items: list[list[str]] = []
    for line in lines:
        if line.startswith("- ") or line == "-":
            items.append([line[2:]])
        elif not items:
            if line.strip():
                problems.append(
                    f"# {block}: the line {line.strip()[:60]!r} comes before the first item; start "
                    "each item with '- '."
                )
        else:
            items[-1].append(line[2:] if line.startswith("  ") else line)
    return items


def _joined(item: list[str]) -> str:
    return "\n".join(item).strip()


def _parse_block(block: Block, lines: list[str], problems: list[str]) -> tuple[bool, Any]:
    """(present, value) for one block's lines; an empty block is not present."""
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    if not lines:
        return False, None
    kind, name = block.kind, block.name
    if kind == "text":
        return True, "\n".join(lines).strip()
    if kind == "line":
        return True, " ".join(line.strip() for line in lines if line.strip())
    if kind == "value":
        words = [line.strip() for line in lines if line.strip()]
        if len(words) > 1:
            problems.append(f"# {name} holds one value; it has {len(words)} lines.")
        return True, words[0]
    if kind == "bool":
        word = " ".join(line.strip() for line in lines).lower()
        if word in ("true", "yes"):
            return True, True
        if word in ("false", "no"):
            return True, False
        problems.append(f"# {name} is true or false; it reads {word[:40]!r}.")
        return False, None
    if kind == "list":
        return True, [_joined(item) for item in _items(lines, name, problems)]
    if kind in ("figures", "handles"):
        out: list[Any] = []
        for line in lines:
            text = line.strip()
            if not text:
                continue
            text = text[2:].strip() if text.startswith("- ") else text
            if kind == "handles":
                out.append(text.strip("[]").strip())
                continue
            match = _FIGURE_LINE.match(text)
            if match is None:
                problems.append(
                    f"# {name}: {text[:60]!r} is not a figure handle; write one '- [source:index]' "
                    "line per figure, e.g. '- [self:2]'."
                )
            else:
                out.append({"source": match.group(1), "index": int(match.group(2))})
        return True, out
    if kind == "claims":
        claims: list[dict] = []
        for item in _items(lines, name, problems):
            # The support line is the item's last line; an earlier line is the claim's own text.
            body = list(item)
            while body and not body[-1].strip():
                body.pop()
            support: list[str] = []
            if body and body[-1].strip().lower().startswith("support:"):
                tokens = (h.strip("[]") for h in re.split(r"[,\s]+", body.pop().strip()[8:]))
                support = [h for h in tokens if ":" in h]  # a handle is 'lib:p1'; words between are not
            claims.append({"claim": _joined(body), "support": support})
        return True, claims
    if kind == "keyvalue":
        request: dict[str, Any] = {}
        datasets: list[dict] = []
        key = None
        for line in lines:
            if line.startswith("  ") and key is not None:
                request[key] += "\n" + line[2:]
                continue
            stripped = line.strip()
            if not stripped:
                if key is not None:
                    request[key] += "\n"
                continue
            head, sep, rest = stripped.partition(":")
            head = head.strip()
            if not sep:
                problems.append(f"# {name}: {stripped[:60]!r} is not a 'key: value' line.")
                continue
            if head == "validation_data":
                parts = rest.split()
                if len(parts) < 2:
                    problems.append(f"# {name}: validation_data takes 'MISSION OBSID'; it reads {rest.strip()!r}.")
                else:
                    datasets.append({"mission": parts[0], "obsid": parts[1],
                                     "synthetic": "synthetic" in (p.lower() for p in parts[2:])})
                key = None
            elif head in _TOOL_REQUEST_KEYS:
                request[head] = rest.strip()
                key = head
            else:
                problems.append(
                    f"# {name}: {head!r} is not a key it takes; the keys are "
                    f"{', '.join(_TOOL_REQUEST_KEYS)} and validation_data."
                )
                key = None
        request = {k: v.strip() for k, v in request.items()}
        if datasets:
            request["validation_data"] = datasets
        return True, request
    if kind in ("sections", "tables"):
        groups: list[tuple[str, list[str]]] = []
        for line in lines:
            if line.startswith("## "):
                groups.append((line[3:].strip(), []))
            elif not groups:
                if line.strip():
                    problems.append(
                        f"# {name}: the line {line.strip()[:60]!r} comes before the first '## ' heading."
                    )
            else:
                groups[-1][1].append(line)
        if kind == "sections":
            return True, [{"heading": h, "body": "\n".join(body).strip()} for h, body in groups]
        tables: list[dict] = []
        for caption, body in groups:
            table: dict[str, Any] = {"caption": caption, "columns": [], "rows": []}
            for line in body:
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.lower().startswith("receipt:"):
                    table["receipt"] = stripped[8:].strip()
                elif stripped.lower().startswith("columns:"):
                    table["columns"] = [c.strip() for c in stripped[8:].split("|")]
                elif stripped.startswith("|"):
                    table["rows"].append([c.strip() for c in stripped.strip("|").split("|")])
                else:
                    problems.append(
                        f"# {name}, table {caption[:40]!r}: {stripped[:60]!r} is not 'receipt:', "
                        "'columns:' or a '| cell | cell |' row."
                    )
            tables.append(table)
        return True, tables
    raise ValueError(f"unknown block kind {kind!r}")  # pragma: no cover


def parse(text: str, blocks: Sequence[Block]) -> tuple[dict[str, Any], list[str]]:
    """The fields a document sets (field → JSON value), and what is wrong with it."""
    by_header = {f"# {b.name}": b for b in blocks}
    problems: list[str] = []
    seen: dict[str, list[str]] = {}
    current: str | None = None
    preamble: list[str] = []
    for line in str(text).replace("\r\n", "\n").split("\n"):
        header = line.rstrip()
        word = _HEADER_WORD.match(header)
        if header not in by_header and word and f"# {word.group(1).lower()}" in by_header:
            problems.append(
                f"'{header}' reads as the header of # {word.group(1).lower()}; write the header "
                f"as '# {word.group(1).lower()}' on its own line, with the content on the lines below."
            )
            continue
        if header not in by_header and _HEADER_LIKE.match(header):
            names = ", ".join(f"# {b.name}" for b in blocks)
            problems.append(
                f"'{header}' is not a block of this report; the headers are {names} (lower case, "
                "one each). Inside a block's text, write a heading without the leading '# '."
            )
            continue
        if header in by_header:
            if header in seen:
                problems.append(f"'{header}' appears twice; each block appears once.")
            seen.setdefault(header, [])
            current = header
            continue
        if current is None:
            preamble.append(line)
        else:
            seen[current].append(line)
    if any(line.strip() for line in preamble):
        names = ", ".join(f"# {b.name}" for b in blocks)
        problems.append(
            f"the text before the first block header belongs to no field; the headers are {names}."
        )
    values: dict[str, Any] = {}
    for header, lines in seen.items():
        block = by_header[header]
        present, value = _parse_block(block, lines, problems)
        if present:
            values[block.field] = value
    return values, problems


# ── edit ───────────────────────────────────────────────────────────────────────


def apply_edit(document: str, arguments: Mapping[str, Any]) -> tuple[str | None, int, str | None]:
    """Edit semantics (Claude Code's Edit): (new document, offset of the change, problem).

    ``old_string`` must occur in ``document`` exactly once, or ``replace_all`` must be true.
    """
    old = arguments.get("old_string")
    new = arguments.get("new_string")
    if not isinstance(old, str) or old == "":
        return None, 0, (
            "old_string is the exact text to replace, copied from the draft; to write the whole "
            "draft, use write_report."
        )
    if not isinstance(new, str):
        return None, 0, "new_string is the text that replaces old_string (a string; empty deletes it)."
    if old == new:
        return None, 0, "old_string and new_string are the same; nothing to change."
    count = document.count(old)
    if count == 0:
        return None, 0, (
            "old_string is not in the draft. Copy it exactly from the draft below, or from the lines "
            "this tool's last reply showed (the text after each line number and tab), line breaks "
            "and spaces included."
        )
    replace_all = arguments.get("replace_all") in (True, "true", "True")
    if count > 1 and not replace_all:
        return None, 0, (
            f"old_string occurs {count} times in the draft. Include more of the surrounding text "
            "to pick one, or set replace_all to change every one."
        )
    at = document.index(old)
    return document.replace(old, new) if replace_all else document.replace(old, new, 1), at, None


def snippet(before: str, after: str) -> str:
    """The lines that differ between two canonical documents, with three lines of context,
    numbered like ``cat -n`` (the numbering of ``after``, the draft now on file)."""
    old, new = before.rstrip("\n").split("\n"), after.rstrip("\n").split("\n")
    changed = [
        (j1, j2) for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(a=old, b=new).get_opcodes()
        if tag != "equal"
    ]
    if not changed:
        return "(the draft on file is unchanged)"
    first = min(j1 for j1, _ in changed)
    last = max(max(j2 - 1, j1) for j1, j2 in changed)
    start, end = max(0, first - _CONTEXT), min(len(new), last + _CONTEXT + 1)
    width = len(str(end))
    return "\n".join(f"{n + 1:>{width}}\t{new[n]}" for n in range(start, end))


def outline(values: Mapping[str, Any], blocks: Sequence[Block], unset: Mapping[str, str] | None = None) -> str:
    """One line: which blocks hold what."""
    parts: list[str] = []
    for block in blocks:
        value = values.get(block.field)
        if value in (None, "", [], {}):
            note = (unset or {}).get(block.field)
            parts.append(f"{block.name} empty" + (f" ({note})" if note else ""))
        elif isinstance(value, list):
            parts.append(f"{block.name} {len(value)}")
        elif isinstance(value, bool):
            parts.append(f"{block.name} {'true' if value else 'false'}")
        elif block.kind == "value":
            parts.append(f"{block.name} {value}")
        elif isinstance(value, str):
            parts.append(f"{block.name} {len(value)} chars")
        else:
            parts.append(f"{block.name} set")
    return "Draft: " + " · ".join(parts)


# ── tool definitions ───────────────────────────────────────────────────────────


def layout(blocks: Sequence[Block], status_values: Sequence[str] = ()) -> str:
    """The document layout as the model reads it in a tool description."""
    lines = []
    for block in blocks:
        hint = block.hint
        if block.field == "status" and status_values:
            hint = f"the outcome, one word: {' or '.join(status_values)}"
        lines.append(f"# {block.name} — {hint}")
    return "\n".join(lines)


_EDIT_TEXT = (
    "Edit your report draft, which is one text document (a refused call shows it whole): "
    "old_string is replaced by new_string. old_string is copied exactly from the draft — line "
    "breaks and spaces included — and occurs once in it, or set replace_all to change every "
    "occurrence. Every block header is always present, so to fill an empty block, replace its "
    "header line with the header plus the new lines. The reply shows the changed lines with their "
    "context, each after its line number and a tab; old_string is the text after them. Free: costs "
    "no tool budget."
)
_WRITE_TEXT = (
    "Write your whole report draft as one text document, replacing the draft on file; use "
    "edit_report to change part of it. Free: costs no tool budget."
)


def _with_layout(text: str, blocks: Sequence[Block], status_values: Sequence[str], extra: str) -> str:
    return (
        f"{text} {extra}".strip()
        + "\n\nThe document is a '# <block>' header line per block, in this order, each followed by "
        "its content; a list item starts with '- ' and its further lines are indented two "
        "spaces:\n" + layout(blocks, status_values)
    )


def edit_tool_def(name: str, blocks: Sequence[Block], status_values: Sequence[str] = (), extra: str = "") -> dict:
    """``edit_report``: Edit over the draft document."""
    return {
        "name": name,
        "description": _with_layout(_EDIT_TEXT, blocks, status_values, extra),
        "input_schema": {
            "type": "object",
            "properties": {
                "old_string": {
                    "type": "string",
                    "description": "The exact text to replace, copied from the draft.",
                },
                "new_string": {
                    "type": "string",
                    "description": "The text that replaces it (empty deletes it).",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence of old_string (default false: exactly one).",
                },
            },
            "required": ["old_string", "new_string"],
        },
    }


def write_tool_def(blocks: Sequence[Block], status_values: Sequence[str] = (), extra: str = "") -> dict:
    """``write_report``: Write the whole draft document."""
    return {
        "name": WRITE_REPORT_TOOL,
        "description": _with_layout(_WRITE_TEXT, blocks, status_values, extra),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The whole draft: every block header, each followed by its content.",
                },
            },
            "required": ["content"],
        },
    }


# ── one change, for every role ─────────────────────────────────────────────────


@dataclass
class Change:
    """The outcome of one write or edit: the parsed fields, or the problems that refused it.

    ``before`` is the draft as rendered before the call; ``submitted`` the text the call produced.
    The caller renders the stored result to get the canonical document the model edits next.
    """

    values: dict[str, Any]
    problems: list[str]
    before: str
    submitted: str = ""


def change(
    current: Mapping[str, Any],
    blocks: Sequence[Block],
    tool: str,
    arguments: Mapping[str, Any],
) -> Change:
    """Apply ``write_report`` or ``edit_report`` to the draft ``current`` (field → JSON value)."""
    document = render(current, blocks)
    if tool == WRITE_REPORT_TOOL:
        content = arguments.get("content")
        if not isinstance(content, str) or not content.strip():
            return Change(dict(current), ["content is the whole draft as text, every block header included."], document)
        content = content.replace("\r\n", "\n")
        new_document = content if content.endswith("\n") else content + "\n"
    else:
        new_document, _at, problem = apply_edit(document, arguments)
        if problem is not None:
            return Change(dict(current), [problem], document)
    values, problems = parse(new_document, blocks)
    if problems:
        return Change(dict(current), problems, document)
    return Change(values, [], document, new_document)


def _content_lines(document: str) -> list[str]:
    """A document's lines as compared for an echo, empty-block headers left out (the stored draft
    keeps every header; a write that leaves an empty block out matches it). Blank lines count:
    the stored draft drops them, and an old_string copied across one would not match."""
    lines = [line.rstrip() for line in document.strip("\n").split("\n")]
    return [
        line for k, line in enumerate(lines)
        if not (line.startswith("# ") and (k + 1 == len(lines) or lines[k + 1].startswith("# ")))
    ]


def reply_lines(tool: str, change_: Change, after: str) -> list[str]:
    """What a saved write or edit shows: the changed lines of the draft now on file (``after``,
    the canonical render), or for a write the whole saved draft when it differs from what was sent."""
    if tool == WRITE_REPORT_TOOL:
        if _content_lines(after) == _content_lines(change_.submitted):
            return ["Report written."]
        return ["Report written. The draft on file reads:", after.rstrip("\n")]
    return ["Report edited:", snippet(change_.before, after)]


def figure_check(catalog_lookup: Callable[[str, int], str | None]) -> Callable[[list[dict]], list[str]]:
    """Refuse a placed figure the catalog does not hold (the analysts' ``# figures``)."""
    def check(citations: list[dict]) -> list[str]:
        problems = []
        for k, c in enumerate(citations, 1):
            note = catalog_lookup(c["source"], c["index"])
            if note is not None:
                problems.append(f"# figures line {k} [{c['source']}:{c['index']}]: {note}")
        return problems
    return check
