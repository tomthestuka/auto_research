"""Render the reviewer's accepted report as one self-contained HTML page with provenance popovers.

Beside the ``.tex`` when ``report_formats`` lists ``html``. Every number in the prose opens (tap, or
hover with a mouse) the chain that produced it, drawn as a two-column z-chain, data | tool:

    <input data> ─► <toolkit root>/<package>.<function>  (commit, environment, arguments)
    <output data> ◄─(environment)─┘ ─► <next tool> … ─► the receipt holding the number

The page gives a reader what they need to reproduce a number themselves — the tool under its
toolkit root, the commit, the environment, the exact arguments (rebuilt from the run ledger and
proven by the receipt's hash), the inputs with their hashes — and a "copy reproduce code" button
per tool. The runtime re-runs nothing (user ruling 2026-10-16: replay is the human's).

Text from the model, a tool or a receipt reaches the page only through :func:`html.escape`; the
page loads nothing external and carries one script of its own. :func:`check_page` parses the
written file back and refuses anything else (``on*`` attributes, external sources, a second
script, an unresolved popover, copy code whose arguments do not parse).
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
from pathlib import Path

from auto_research.provenance import ReceiptLookup, parse_bindings
from auto_research.provenance.bindings import ATTRIBUTION_KINDS, NumberSource
from auto_research.provenance.calls import LedgerCall, calls_by_receipt, ledger_calls
from auto_research.provenance.store import NOT_EVIDENCE_NAMES, TOOLKIT_PRODUCER_PREFIX, is_evidence
from auto_research.report.figures import stage_figure_receipts
from auto_research.report.render import MAX_TABLE_ROWS, parse_delimited, report_attributions
from auto_research.schemas.artifacts import InputFile, Receipt
from auto_research.schemas.review import ReviewerReport

HTML_NAME = "report.html"

_ID_RE = re.compile(r"[^A-Za-z0-9]")


def _slug(handle: str) -> str:
    """A unique, id-safe name for a handle (``a-b:r1`` and ``a_b:r1`` stay apart)."""
    return _ID_RE.sub("-", handle) + "-" + hashlib.sha1(handle.encode("utf-8")).hexdigest()[:8]


def _pid(handle: str) -> str:
    """The popover id of a receipt's chain."""
    return "chain-" + _slug(handle)


def _aid(handle: str) -> str:
    """The anchor of a receipt's row in the provenance table."""
    return "rcpt-" + _slug(handle)


@dataclass
class HtmlResult:
    """One render: where the page went, what the model can fix, and the renderer's own faults."""

    html_path: Path | None
    problems: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    chains: list[str] = field(default_factory=list)


# -- the chain behind one receipt --------------------------------------------------------------


@dataclass
class _Step:
    """One tool call in a chain: the receipts it minted, its inputs, and its ledger call."""

    call_id: str
    receipts: list[Receipt]
    call: LedgerCall | None

    @property
    def first(self) -> Receipt:
        return self.receipts[0]


class _Chains:
    """Builds and renders the chain behind each receipt, each popover once."""

    def __init__(self, lookup: ReceiptLookup, calls: Sequence[LedgerCall]) -> None:
        self.lookup = lookup
        self.calls = calls
        self.wanted: list[str] = []

    def want(self, handle: str) -> str:
        if handle not in self.wanted:
            self.wanted.append(handle)
        return _pid(handle)

    def _closure(self, handle: str) -> list[Receipt]:
        seen: dict[str, Receipt] = {}
        stack = [handle]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            receipt = self.lookup.get(current)
            if receipt is None:
                continue
            seen[current] = receipt
            stack.extend(receipt.parents)
        return list(seen.values())

    def steps(self, handle: str) -> list[_Step]:
        closure = self._closure(handle)
        matched = calls_by_receipt(self.calls, closure)
        groups: dict[str, list[Receipt]] = {}
        for receipt in sorted(closure, key=lambda r: r.created_at):
            groups.setdefault(receipt.call_id or receipt.handle, []).append(receipt)
        return [
            _Step(call_id=key, receipts=group, call=matched.get(group[0].handle))
            for key, group in groups.items()
        ]

    def popover(self, handle: str) -> str:
        receipt = self.lookup.get(handle)
        title = f"Where {escape(handle)} comes from"
        if receipt is None:
            body = "<p>This receipt is not in the run's store.</p>"
        elif receipt.kind in ("paper", "web"):
            body = _reference_body(receipt)
        else:
            body = self._chain_html(handle)
        return (
            f'<div popover id="{_pid(handle)}" class="chain-pop">'
            f'<div class="pop-head"><strong>{title}</strong>'
            f'<button type="button" class="close" popovertarget="{_pid(handle)}" '
            f'popovertargetaction="hide" aria-label="Close">×</button></div>{body}</div>'
        )

    def _chain_html(self, handle: str) -> str:
        steps = self.steps(handle)
        produced = {r.handle: i for i, step in enumerate(steps, 1) for r in step.receipts}
        rows: list[str] = ['<div class="col-h">data</div><div class="col-h">tool</div>']
        for number, step in enumerate(steps, 1):
            inputs = step.first.inputs
            for k, item in enumerate(inputs):
                arrow = "─┐" if k < len(inputs) - 1 else "─┘"
                rows.append(f'<div class="data">{_input_html(item, produced)} <span class="arw">{arrow}</span></div>'
                            '<div class="tool gap"></div>')
            rows.append(f'<div class="data arw-cell"><span class="arw">{"└─►" if inputs else "─►"}</span></div>'
                        f'<div class="tool">{_tool_html(step, number)}</div>')
            env = _env_label(step.first)
            outs = " ".join(_output_html(r, r.handle == handle) for r in step.receipts)
            rows.append(f'<div class="data">{outs} <span class="arw">◄─{escape(env)}─┘</span></div>'
                        '<div class="tool gap"></div>')
        return f'<div class="zchain">{"".join(rows)}</div>'


def _short(text: str, n: int = 12) -> str:
    return text[:n]


def _input_html(item: InputFile, produced: Mapping[str, int]) -> str:
    sha = f'<code class="sha">{_short(item.sha256)}</code>'
    if item.receipt and item.receipt in produced:
        return (f'<span class="node">{escape(Path(item.path).name)} '
                f'<span class="muted">[{escape(item.receipt)}, step {produced[item.receipt]}]</span> {sha}</span>')
    if item.basis == "obsdb":
        mission, obsid = Path(item.path).parent.name, Path(item.path).name
        return (f'<details class="node"><summary>obs-db {escape(mission)}/{escape(obsid)} {sha}</summary>'
                f'<div>catalog entry + folder listing of <code>{escape(str(item.path))}</code><br>'
                f'sha256 <code>{escape(item.sha256)}</code></div></details>')
    what = "folder listing" if item.basis == "listing" else "file"
    return (f'<details class="node"><summary>{escape(Path(item.path).name)} {sha}</summary>'
            f'<div>{what} <code>{escape(str(item.path))}</code><br>sha256 <code>{escape(item.sha256)}</code>'
            + (f'<br>made by {escape(item.receipt)}' if item.receipt else "") + "</div></details>")


def _output_html(receipt: Receipt, final: bool) -> str:
    label = receipt.caption or (Path(receipt.path).name if receipt.kind in ("data", "figure") else f"{receipt.kind} text")
    flags = f' <span class="flag">{escape(", ".join(receipt.flags))}</span>' if receipt.flags else ""
    mark = ' <span class="here">← the number is here</span>' if final else ""
    return (f'<details class="node{" final" if final else ""}"><summary>{escape(receipt.handle)} '
            f'{escape(label)}{flags}{mark}</summary><div>{escape(receipt.kind)}, sha256 '
            f'<code>{escape(receipt.sha256)}</code></div></details>')


def _tool_name(receipt: Receipt) -> tuple[str, bool]:
    """``(display name, is a toolkit tool)``: ``<toolkit root>/<package>.<function>``."""
    if receipt.produced_by.startswith(TOOLKIT_PRODUCER_PREFIX):
        root = str(receipt.toolkit_root) if receipt.toolkit_root else "<toolkit root>"
        return f"{root}/{receipt.produced_by[len(TOOLKIT_PRODUCER_PREFIX):]}", True
    what = NOT_EVIDENCE_NAMES.get(receipt.produced_by)
    return (f"{receipt.produced_by} ({what})" if what else receipt.produced_by), False


def _env_record(fingerprint: str | None) -> dict:
    """The environment's lock record (name, prefix, boot file), when this host's lock store has it."""
    if not fingerprint:
        return {}
    try:
        from auto_research.env import env_lock_dir

        record = json.loads((env_lock_dir() / f"{fingerprint}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, ImportError):
        return {}
    return record if isinstance(record, dict) else {}


def _env_label(receipt: Receipt) -> str:
    if not receipt.env:
        return "environment not recorded"
    name = _env_record(receipt.env).get("environment") or "environment"
    return f"{name} @ env:{receipt.env[:12]}" + (", untested" if receipt.env_untested else "")


def _tool_html(step: _Step, number: int) -> str:
    receipt = step.first
    name, toolkit = _tool_name(receipt)
    parts = [f'<div class="tname{"" if toolkit else " grey"}">{number}. {escape(name)}</div>']
    if receipt.toolkit_commit:
        parts.append(f'<div class="muted">commit <code>{escape(receipt.toolkit_commit[:12])}</code></div>')
    if step.call is not None:
        args = json.dumps(step.call.sent_args, indent=1, sort_keys=True, default=str)
        parts.append(f'<details><summary>arguments</summary><pre class="args">{escape(args)}</pre></details>')
        if toolkit:
            code, args_json = reproduce_code(receipt, step.call)
            parts.append(
                f'<button type="button" class="copy" data-code="{escape(code)}" '
                f'data-args="{escape(args_json)}">copy reproduce code</button>'
            )
    elif receipt.args_sha256:
        parts.append('<div class="muted">arguments: not rebuilt from the ledger (hash '
                     f'<code>{escape(receipt.args_sha256[:12])}</code>)</div>')
    return "".join(parts)


def reproduce_code(receipt: Receipt, call: LedgerCall) -> tuple[str, str]:
    """The notebook lines that rerun one toolkit call by hand, and the arguments JSON in them."""
    args_json = json.dumps(call.sent_args, sort_keys=True, default=str)
    root = str(receipt.toolkit_root) if receipt.toolkit_root else "<toolkit root>"
    fn = str(call.context.get("fn") or receipt.produced_by.rsplit(".", 1)[-1])
    module = str(call.context.get("module") or "")
    importable = fn.isidentifier() and all(part.isidentifier() for part in module.split(".")) and bool(module)
    record = _env_record(receipt.env)

    def one_line(text: object) -> str:
        return re.sub(r"[\r\n]+", " ", str(text))

    lines = [f"# Reproduce {one_line(receipt.handle)}: {one_line(receipt.produced_by)}"]
    if receipt.toolkit_commit:
        lines.append(f"# toolkit {one_line(root)} at commit {one_line(receipt.toolkit_commit)}")
    if receipt.env:
        lines.append(f"# environment {one_line(record.get('environment') or '?')} (env:{receipt.env}); "
                     f"rebuild with auto_research.env.replay_environment({receipt.env!r})")
    if record.get("boot_file"):
        lines.append("# its REPL boot file runs first (recorded in the environment's lock)")
    if call.context.get("workdir"):
        lines.append(f"# the call ran in {one_line(call.context['workdir'])}; relative paths resolve there")
    lines.append("# arguments naming output files point into the run's folders: change them to a scratch "
                 "folder so the run's receipted files stay as they are")
    lines.append("# a fresh interpreter: a tool relying on an earlier call's session needs that call first")
    lines += ["import json, sys", f"sys.path.insert(0, {root!r})"]
    if importable:
        lines.append(f"from {module} import {fn}")
    else:
        lines.append(f"# import the tool's function from its module under {one_line(root)}")
        fn = "tool"
    lines += [f"args = json.loads({args_json!r})", f"out = {fn}(**args)"]
    return "\n".join(lines), args_json


def _reference_body(receipt: Receipt) -> str:
    ids = receipt.ids or {}
    links = []
    for key, base in (("arxiv", "https://arxiv.org/abs/"), ("doi", "https://doi.org/"),
                      ("bibcode", "https://ui.adsabs.harvard.edu/abs/")):
        if ids.get(key):
            links.append(f'<a href="{escape(base + ids[key])}" rel="noopener">{escape(key)}: {escape(ids[key])}</a>')
    if ids.get("url") and not links and re.match(r"^https?://", ids["url"], re.IGNORECASE):
        links.append(f'<a href="{escape(ids["url"])}" rel="noopener">{escape(ids["url"])}</a>')
    return (f'<p>{escape(receipt.title or receipt.handle)}</p><p>{" · ".join(links)}</p>'
            f'<p class="muted">{escape(receipt.kind)}, sha256 <code>{escape(receipt.sha256[:12])}</code>. '
            "A value attributed to a paper is checked by a human against the PDF.</p>")


# -- prose ----------------------------------------------------------------------------------------

#: Containers whose text is the reviewer's prose: every number in them must sit in a button.
_PROSE = 'data-prose="1"'


@dataclass
class _Prose:
    lookup: ReceiptLookup
    own_source: str
    numbers: Mapping[str, list[NumberSource]]
    chains: _Chains
    problems: list[str] = field(default_factory=list)
    cited: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)

    def note(self, handle: str) -> None:
        if handle not in self.cited:
            self.cited.append(handle)

    def segments(self, text: str, label: str) -> list[tuple[int, int, str]]:
        """The buttons in one field, as ``(start, end, html)`` over the exact text the grounding
        check scanned (``numbers`` is keyed by it)."""
        segments: list[tuple[int, int, str]] = []
        bindings = parse_bindings(text, own_source=self.own_source)
        cited_here = {b.handle for b in bindings}
        for binding in bindings:
            self.note(binding.handle)
            pid = self.chains.want(binding.handle)
            if binding.quote is None:
                html = (f'<button type="button" class="cite" popovertarget="{pid}" popovertargetaction="show">'
                        f"[{escape(binding.handle)}]</button>")
            else:
                html = (f'<button type="button" class="num" popovertarget="{pid}" popovertargetaction="show">'
                        f"{escape(binding.quote)}</button>")
                receipt = self.lookup.get(binding.handle)
                if binding.locator and receipt is not None and receipt.kind in ATTRIBUTION_KINDS:
                    # Where in the paper: masked by the grounding check with its binding (a locator
                    # on any other quote is not shown, as in the .tex).
                    html += f' <span class="muted" data-loc="1">({escape(binding.locator)})</span>'
            segments.append((binding.start, binding.end, html))
        for number in self.numbers.get(text, []):
            evidence = [h for h in number.handles if (r := self.lookup.get(h)) is not None and is_evidence(r)]
            if not evidence:
                self.problems.append(
                    f'{label}: "{number.value}" appears in no receipt the report can show as its source. '
                    "Cite the toolkit result it comes from as [handle] in the same text."
                )
                continue
            own = [h for h in evidence if h in cited_here]
            if own:
                self.note(own[0])
                pid = self.chains.want(own[0])
                cls = "num"
            else:
                # Grounded only by the report's wider scope: the text names none of the receipts
                # holding it, so the page lists them all rather than presenting one as its source.
                pid = self._held(number.value, evidence)
                cls = "num scoped"
            segments.append((number.start, number.end,
                             f'<button type="button" class="{cls}" popovertarget="{pid}" popovertargetaction="show">'
                             f"{escape(number.value)}</button>"))
        return sorted(segments)

    def _held(self, value: str, handles: Sequence[str]) -> str:
        pid = f"held-{len(self.held) + 1}"
        listed = handles[:12]
        items = "".join(
            f'<li><button type="button" class="cite" popovertarget="{self.chains.want(h)}" '
            f'popovertargetaction="show">[{escape(h)}]</button></li>' for h in listed
        )
        more = f"<p class=\"muted\">and {len(handles) - len(listed)} more.</p>" if len(handles) > len(listed) else ""
        self.held.append(
            f'<div popover id="{pid}" class="chain-pop"><div class="pop-head"><strong>Where '
            f'“{escape(value)}” appears</strong><button type="button" class="close" popovertarget="{pid}" '
            'popovertargetaction="hide" aria-label="Close">×</button></div><p>The text cites none of the '
            f"receipts holding this value; it appears in {len(handles)} receipt(s) the reviewer could "
            f"cite:</p><ul>{items}</ul>{more}</div>"
        )
        return pid

    def inline(self, text: str, label: str) -> str:
        """One field → HTML: bindings and numbers become buttons; everything else is escaped.
        Newlines are kept, so blocks can be split afterwards."""
        return _render_range(text, self.segments(text, label), 0, len(text))

    def block(self, text: str, label: str) -> str:
        """Paragraphs (blank line) and bullet lines ('- ') → HTML."""
        rendered = self.inline(text, label).strip()
        out = []
        for paragraph in re.split(r"\n\s*\n", rendered):
            lines = [line.rstrip() for line in paragraph.splitlines() if line.strip()]
            bullets = [line for line in lines if line.lstrip().startswith("- ")]
            if lines and len(bullets) == len(lines):
                out.append("<ul>" + "".join(f"<li>{b.lstrip()[2:]}</li>" for b in bullets) + "</ul>")
            elif lines:
                # Line breaks are kept: a line start is where the grounding check exempts a list
                # enumerator ("2. The model …"), and the page check reads it the same way.
                out.append("<p>" + "<br>\n".join(line.strip() for line in lines) + "</p>")
        return "".join(out)

    def cells(self, cells: Sequence[str], label: str, tag: str) -> str:
        """One table row, rendered from the row's joined text (the form the check scanned), each
        cell from its own span of it."""
        joined = " | ".join(cells)
        segments = self.segments(joined, label)
        out, start = [], 0
        for cell in cells:
            end = start + len(cell)
            crossing = [s for s in segments if s[0] < end < s[1]]
            if crossing:
                self.problems.append(f"{label}: a quote spans two cells. Quote within one cell.")
            out.append(f"<{tag}>{_render_range(joined, segments, start, end)}</{tag}>")
            start = end + 3
        return "".join(out)


def _render_range(text: str, segments: Sequence[tuple[int, int, str]], start: int, end: int) -> str:
    """``text[start:end]`` escaped, with the segments that begin inside it rendered."""
    out, at = [], start
    for seg_start, seg_end, html in segments:
        if seg_start < at or seg_start >= end:
            continue
        out.append(escape(text[at:seg_start]))
        out.append(html)
        at = min(seg_end, end) if seg_end <= end else end
    out.append(escape(text[at:end]))
    return "".join(out)


def _table_html(table, number: int, prose: _Prose) -> tuple[str, list[str]]:
    from auto_research.report.render import check_table

    label = f"table {number}"
    rows, note, problems = check_table(table, number, prose.lookup)
    if problems:
        return "", problems
    caption = prose.inline(table.caption, f"{label} caption") if table.caption.strip() else ""
    if table.receipt:
        prose.note(table.receipt)
        pid = prose.chains.want(table.receipt)
        header, *data = rows
        more = len(data) - MAX_TABLE_ROWS
        body = "".join("<tr>" + "".join(f"<td>{escape(c)}</td>" for c in row) + "</tr>"
                       for row in data[:MAX_TABLE_ROWS])
        notes = "".join(f'<p class="muted">{escape(text)}</p>' for text in (
            note, f"{more} further rows are in {table.receipt}." if more > 0 else None) if text)
        return (f'<figure class="table"><figcaption {_PROSE}>Table {number}. {caption} '
                f'<button type="button" class="cite" popovertarget="{pid}" popovertargetaction="show">'
                f'[{escape(table.receipt)}]</button></figcaption><div class="scroll"><table><thead><tr>'
                + "".join(f"<th>{escape(h)}</th>" for h in header)
                + f"</tr></thead><tbody>{body}</tbody></table></div>{notes}</figure>"), []
    head = prose.cells(table.columns, f"{label} columns", "th")
    body = "".join("<tr>" + prose.cells(row, f"{label} row {j}", "td") + "</tr>"
                   for j, row in enumerate(table.rows, 1))
    return (f'<figure class="table"><figcaption {_PROSE}>Table {number}. {caption}</figcaption>'
            f'<div class="scroll"><table {_PROSE}><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'
            "</div></figure>"), []


# -- the page ---------------------------------------------------------------------------------------

_IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")


def _data_uri(path: Path, mime: str) -> str:
    import base64

    return f"data:{mime};base64," + base64.b64encode(Path(path).read_bytes()).decode("ascii")


def render_html(
    report: ReviewerReport,
    lookup: ReceiptLookup,
    report_dir: Path,
    *,
    run_id: str,
    numbers: Mapping[str, list[NumberSource]],
    ledger: Path | None,
    own_source: str = "rev",
    write: bool = True,
) -> HtmlResult:
    """Write ``report_dir/report.html`` from a verified report.

    ``numbers`` is the grounding check's own answer for every field (``check_report_fields(...,
    sources=True).numbers``), so each number on the page points at a receipt the check accepted.
    ``ledger`` is the run's ``monitor.jsonl``: the arguments each call ran with are rebuilt from it.
    Nothing is written while a model-fixable problem stands, nor with ``write=False`` (another
    format of the same attempt has one); a written page the check refuses is removed.
    """
    ledger_error = None
    try:
        calls = ledger_calls(ledger) if ledger is not None and Path(ledger).is_file() else []
    except OSError as exc:
        calls = []
        ledger_error = f"the run ledger could not be read ({exc}); arguments are not shown"
    chains = _Chains(lookup, calls)
    prose = _Prose(lookup=lookup, own_source=own_source, numbers=numbers, chains=chains)
    staged, problems = stage_figure_receipts(report.figures, lookup, report_dir) if write else ([], [])

    parts: list[str] = []
    title = prose.inline(report.title, "title").strip() if report.title.strip() else "Research report"
    if report.abstract.strip():
        parts.append(f'<section class="abstract" {_PROSE}><h2>Abstract</h2>{prose.block(report.abstract, "abstract")}</section>')
    for i, section in enumerate(report.sections, 1):
        parts.append(f"<section {_PROSE}><h2>{prose.inline(section.heading, f'section {i} heading').strip()}</h2>"
                     f"{prose.block(section.body, f'section {i}')}</section>")
    if report.key_findings:
        items = "".join(f"<li>{prose.inline(f, f'key_finding {k}').strip()}</li>"
                        for k, f in enumerate(report.key_findings, 1))
        parts.append(f"<section {_PROSE}><h2>Key findings</h2><ul>{items}</ul></section>")
    for number, (receipt, relpath) in enumerate(staged, 1):
        prose.note(receipt.handle)
        pid = chains.want(receipt.handle)
        alt = escape(receipt.caption or receipt.handle)
        # Embedded, so the one file sent to a phone carries its figures (the staged copy is the one
        # whose bytes were re-hashed against the receipt).
        shown = (f'<img src="{_data_uri(report_dir / relpath, receipt.mime_type or "")}" alt="{alt}">'
                 if (receipt.mime_type or "") in _IMAGE_TYPES
                 else f'<a href="{escape(relpath)}">open the figure ({escape(receipt.mime_type or "file")})</a>')
        parts.append(
            f"<figure>{shown}<figcaption>Figure {number}. {escape(receipt.caption or '')} "
            f'<button type="button" class="cite" popovertarget="{pid}" popovertargetaction="show">'
            f"[{escape(receipt.handle)}]</button></figcaption></figure>"
        )
    for number, table in enumerate(report.tables, 1):
        html, found = _table_html(table, number, prose)
        problems.extend(found)
        parts.append(html)
    problems.extend(prose.problems)
    if problems or not write:
        return HtmlResult(html_path=None, problems=list(dict.fromkeys(problems)))

    references = [r for h in prose.cited if (r := lookup.get(h)) is not None and r.kind in ("paper", "web")]
    if references:
        items = "".join(
            f'<li><button type="button" class="cite" popovertarget="{chains.want(r.handle)}" '
            f'popovertargetaction="show">[{escape(r.handle)}]</button> {escape(r.title or r.handle)}</li>'
            for r in references
        )
        parts.append(f"<section><h2>References</h2><ol>{items}</ol></section>")
    attributions = report_attributions(report, lookup, own_source=own_source)
    if attributions:
        rows = "".join(f"<tr><td>{escape(a.handle)}</td><td>{escape(a.value)}</td><td>{escape(a.locator)}</td></tr>"
                       for a in attributions)
        parts.append("<section><h2>Values to verify against the PDFs</h2><p>Each value is attributed to a paper "
                     "and was not checked against its text.</p><div class=\"scroll\"><table><thead><tr><th>Paper</th>"
                     f"<th>Value</th><th>Where</th></tr></thead><tbody>{rows}</tbody></table></div></section>")
    parts.append(_provenance_table(prose.cited, lookup))
    if ledger_error:
        parts.append(f'<p class="muted">{escape(ledger_error)}</p>')

    popovers = "".join(chains.popover(h) for h in chains.wanted) + "".join(prose.held)
    page = _PAGE.format(
        title_text=escape(report.title or "Research report"), title=title, run_id=escape(run_id),
        body="".join(parts), popovers=popovers, css=_CSS, js=_JS, prose=_PROSE,
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / HTML_NAME
    path.write_text(page, encoding="utf-8")
    errors = check_page(path)
    if errors:
        path.unlink(missing_ok=True)  # never leave a page the check refused where a human looks
    return HtmlResult(html_path=None if errors else path, errors=errors, chains=list(chains.wanted))


def _provenance_table(handles: Iterable[str], lookup: ReceiptLookup) -> str:
    rows = []
    for handle in handles:
        receipt = lookup.get(handle)
        if receipt is None:
            continue
        name, _ = _tool_name(receipt)
        rows.append(
            f'<tr id="{_aid(handle)}"><td>{escape(handle)}</td><td>{escape(receipt.kind)}</td>'
            f"<td>{escape(name)}</td><td><code>{escape(receipt.sha256[:12])}</code></td>"
            f"<td>{escape(', '.join(receipt.flags) or '—')}</td></tr>"
        )
    if not rows:
        return ""
    return ("<section><h2>Provenance</h2><p>Every artifact this report cites, as the runtime recorded it."
            '</p><div class="scroll"><table><thead><tr><th>Handle</th><th>Kind</th><th>Produced by</th>'
            f"<th>SHA-256</th><th>Flags</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>")


# -- the page's own check ---------------------------------------------------------------------------


_FORBIDDEN_TAGS = frozenset({"iframe", "frame", "base", "form", "object", "embed", "link"})
_VOID = frozenset({"area", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"})


class _PageScan(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.targets: list[str] = []
        self.sources: list[str] = []
        self.links: list[str] = []
        self.scripts = 0
        self.handlers: list[str] = []
        self.forbidden: list[str] = []
        self.codes: list[tuple[str, str]] = []
        self.loose: list[str] = []
        self._stack: list[tuple[str, bool, bool]] = []  # (tag, opens prose, opens a no-scan region)
        self._prose = self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: v or "" for k, v in attrs}
        if values.get("id"):
            self.ids.append(values["id"])
        if values.get("popovertarget"):
            self.targets.append(values["popovertarget"])
        if tag == "script":
            self.scripts += 1
        if tag in _FORBIDDEN_TAGS or (tag == "meta" and "http-equiv" in values):
            self.forbidden.append(tag)
        for key in ("src", "srcset", "data", "poster", "action", "formaction", "background"):
            if values.get(key):
                self.sources.append(values[key])
        if tag == "a" and values.get("href"):
            self.links.append(values["href"])
        self.handlers.extend(name for name in values if name.lower().startswith("on") or name.lower() == "style")
        if "data-code" in values:
            self.codes.append((values["data-code"], values.get("data-args", "")))
        if tag in _VOID:
            return
        prose = values.get("data-prose") == "1"
        skip = tag in ("button", "code", "pre", "script", "style") or "data-loc" in values
        self._prose += prose
        self._skip += skip
        self._stack.append((tag, prose, skip))

    def handle_endtag(self, tag: str) -> None:
        while self._stack:
            open_tag, prose, skip = self._stack.pop()
            self._prose -= prose
            self._skip -= skip
            if open_tag == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._prose and not self._skip and data.strip():
            from auto_research.provenance.bindings import _scanned_numbers

            self.loose.extend(m.group() for m in _scanned_numbers(data, ()))


def check_page(path: Path) -> list[str]:
    """Parse the written page back; every problem is the renderer's (the model never writes HTML).

    Refused: a popover target that does not exist, a duplicate id, anything loaded from outside
    the report folder, a link with a scheme other than http(s), a forbidden tag, an inline handler
    or ``style`` attribute, other than exactly one script, a copy block whose arguments do not
    parse back to the recorded JSON, and a number in the reviewer's prose outside a button.
    """
    scan = _PageScan()
    try:
        scan.feed(Path(path).read_text(encoding="utf-8"))
        scan.close()
    except (OSError, ValueError) as exc:
        return [f"report.html could not be parsed back: {exc}"]
    errors = []
    missing = sorted(set(scan.targets) - set(scan.ids))
    if missing:
        errors.append(f"report.html: popovers without a target: {', '.join(missing[:8])}")
    duplicates = sorted({i for i in scan.ids if scan.ids.count(i) > 1})
    if duplicates:
        errors.append(f"report.html: duplicate ids: {', '.join(duplicates[:8])}")
    external = [s[:60] for s in scan.sources
                if re.match(r"^[a-z][a-z0-9+.-]*:|^//|^/", s.strip(), re.IGNORECASE)
                and not re.match(r"^data:image/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]*$", s.strip())]
    if external:
        errors.append(f"report.html loads something not beside it: {', '.join(external[:4])}")
    schemes = [h for h in scan.links
               if not re.match(r"^https?://", h.strip(), re.IGNORECASE)
               and (re.match(r"^\s*[a-z][a-z0-9+.-]*:", h, re.IGNORECASE) or h.strip().startswith("/"))]
    if schemes:
        errors.append(f"report.html links outside the page other than http(s): {', '.join(schemes[:4])}")
    if scan.forbidden:
        errors.append(f"report.html carries forbidden tags: {', '.join(sorted(set(scan.forbidden)))}")
    if scan.scripts != 1:
        errors.append(f"report.html carries {scan.scripts} scripts; it must carry its own one")
    if scan.handlers:
        errors.append(f"report.html carries inline handlers or styles: {', '.join(sorted(set(scan.handlers)))}")
    for code, args_json in scan.codes:
        try:
            literal = re.search(r"^args = json\.loads\((.*)\)$", code, re.MULTILINE)
            if literal is None or json.loads(ast.literal_eval(literal.group(1))) != json.loads(args_json):
                raise ValueError("arguments differ")
        except (ValueError, SyntaxError) as exc:
            errors.append(f"report.html: a reproduce block's arguments do not parse ({exc})")
            break
    if scan.loose:
        errors.append(f"report.html: numbers in the prose outside a provenance button: {', '.join(scan.loose[:8])}")
    return errors


_CSS = """
:root{--bg:#fff;--fg:#1d1d1f;--muted:#6b6b70;--line:#d8d8dc;--accent:#0b57d0;--chip:#eef3fd;--pop:#fff;--grey:#8a8a8f;--here:#0a7d33}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#141416;--fg:#ececf0;--muted:#a0a0a8;--line:#3a3a40;--accent:#8ab4f8;--chip:#1f2a3d;--pop:#1c1c20;--grey:#7c7c84;--here:#5fd38a}}
:root[data-theme="dark"]{--bg:#141416;--fg:#ececf0;--muted:#a0a0a8;--line:#3a3a40;--accent:#8ab4f8;--chip:#1f2a3d;--pop:#1c1c20;--grey:#7c7c84;--here:#5fd38a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 Georgia,"DejaVu Serif",serif}
main{max-width:46rem;margin:0 auto;padding:1.5rem 16px 4rem}
h1{font-size:1.6rem;line-height:1.25}h2{font-size:1.2rem;margin-top:2rem}
.muted{color:var(--muted);font-size:.9em}
code,pre{font:13px/1.45 ui-monospace,Menlo,Consolas,monospace}
button.num,button.cite{font:inherit;color:var(--accent);background:var(--chip);border:0;border-radius:4px;padding:0 .2em;cursor:pointer}
button.cite{font-size:.85em}
button.num.scoped{background:none;text-decoration:underline dotted}
figure{margin:1.5rem 0}img{max-width:100%;height:auto}
.scroll{overflow-x:auto}table{border-collapse:collapse;font-size:.9em}td,th{border-bottom:1px solid var(--line);padding:.25rem .5rem;text-align:left}
[popover].chain-pop{width:min(48rem,94vw);max-height:85vh;overflow:auto;background:var(--pop);color:var(--fg);border:1px solid var(--line);border-radius:10px;padding:.8rem 1rem;font-size:14px}
.pop-head{display:flex;justify-content:space-between;align-items:center;gap:1rem;margin-bottom:.6rem}
.close{font-size:1.3rem;background:none;border:0;color:var(--fg);cursor:pointer}
.zchain{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);column-gap:.6rem;row-gap:.25rem}
.col-h{font-size:.75rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);border-bottom:1px solid var(--line)}
.data,.tool{min-width:0;overflow-wrap:anywhere}
.tool:not(.gap){border:1px solid var(--line);border-radius:8px;padding:.35rem .5rem}
.tname{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px}.tname.grey{color:var(--grey)}
.arw{color:var(--muted);font-family:ui-monospace,Menlo,Consolas,monospace;white-space:nowrap}
.arw-cell{text-align:right}
.node summary{cursor:pointer}.node.final summary{font-weight:bold}.here{color:var(--here)}
.flag{color:#b3261e}.args{white-space:pre-wrap;max-height:16rem;overflow:auto}
button.copy{margin-top:.3rem;font:12px ui-monospace,Menlo,Consolas,monospace;border:1px solid var(--line);background:var(--chip);color:var(--fg);border-radius:6px;padding:.15rem .5rem;cursor:pointer}
@media (max-width:520px){[popover].chain-pop{padding:.6rem;font-size:13px}.zchain{column-gap:.3rem}}
"""

_JS = """
(function(){
  var hover = window.matchMedia && window.matchMedia('(hover: hover) and (pointer: fine)').matches;
  var open = null, timer = null;
  function pop(btn){ return document.getElementById(btn.getAttribute('popovertarget')); }
  if (hover) {
    document.querySelectorAll('button.num, button.cite').forEach(function(btn){
      btn.addEventListener('mouseenter', function(){
        clearTimeout(timer); var p = pop(btn); if (!p) return;
        if (open && open !== p) { try { open.hidePopover(); } catch (e) {} }
        try { p.showPopover(); open = p; } catch (e) {}
      });
      btn.addEventListener('mouseleave', function(){
        var p = pop(btn); timer = setTimeout(function(){ if (p && !p.matches(':hover')) { try { p.hidePopover(); } catch (e) {} } }, 400);
      });
    });
    document.querySelectorAll('[popover]').forEach(function(p){
      p.addEventListener('mouseleave', function(){ timer = setTimeout(function(){ try { p.hidePopover(); } catch (e) {} }, 400); });
      p.addEventListener('mouseenter', function(){ clearTimeout(timer); });
    });
  }
  document.querySelectorAll('button.copy').forEach(function(btn){
    btn.addEventListener('click', function(){
      var text = btn.getAttribute('data-code'), label = btn.textContent;
      function done(){ btn.textContent = 'copied'; setTimeout(function(){ btn.textContent = label; }, 1200); }
      if (navigator.clipboard && navigator.clipboard.writeText) { navigator.clipboard.writeText(text).then(done, fallback); } else { fallback(); }
      function fallback(){ var t = document.createElement('textarea'); t.value = text; document.body.appendChild(t); t.select(); try { document.execCommand('copy'); done(); } catch (e) {} t.remove(); }
    });
  });
})();
"""

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title_text}</title><style>{css}</style></head>
<body><main><h1 {prose}>{title}</h1><p class="muted">auto_research run <code>{run_id}</code>. Tap a number (or hover it) to see the chain that produced it.</p>
{body}</main>{popovers}<script>{js}</script></body></html>
"""
