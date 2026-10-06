"""Render the reviewer's verified report into LaTeX, and compile it with lualatex.

The reviewer writes plain text; this module is the only author of LaTeX (human 2026-09-24). That
split is what lets the quote check run on the very text the model wrote: a model that wrote LaTeX
would write ``\\%`` and ``$\\pm$`` where the receipt says ``%`` and ``±``, and the check would compare
different characters. Here every quote is substituted by :func:`render_bindings` and escaped by one
fixed map, so the typeset number is the receipt's number.

What the renderer decides, and the model cannot:

* **the preamble** — lualatex with a Unicode text font, so χ², ± and ≲ typeset as written;
* **figures** — staged into the report folder by content hash, re-hashed first ([D4]+[D5]);
* **tool-computed tables** — typeset from the receipt's stored bytes, never retyped;
* **references** — built from the cited paper/web receipts' titles and identifiers ([C4]);
* **the provenance appendix** — every cited handle with its producer and hash.

Problems the model can fix (an unparseable table receipt, a figure handle that is not a figure, a
malformed row) come back as sentences for ``turn_end``. A compile failure is the renderer's own
bug, with one exception the model can act on: a character the font cannot typeset, named so the
model rewrites it.
"""

from __future__ import annotations

import csv
import io
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote as url_quote

from auto_research.provenance import ReceiptLookup, paper_attributions, parse_bindings
from auto_research.report.figures import stage_figure_receipts
from auto_research.report.writers import _errors_with_context
from auto_research.schemas.artifacts import PaperAttribution, Receipt
from auto_research.schemas.review import ReviewerReport, ReviewerTable

#: The text font, and the fonts luaotfload falls back to for a glyph it lacks. DejaVu Serif carries
#: Greek and most symbols; its math companion and DejaVu Sans fill the relations it misses (≲, ≳), so
#: a report written in Unicode typesets without a character map.
MAIN_FONT = "DejaVu Serif"
FALLBACK_FONTS = ("DejaVu Math TeX Gyre:mode=harf;", "DejaVu Sans:mode=harf;", "FreeSerif:mode=harf;")
TEX_NAME = "report.tex"
LATEX_COMPILER = "lualatex"

#: Rows typeset from one table receipt; the rest are counted in a note naming the receipt.
MAX_TABLE_ROWS = 60

_SPECIALS = {
    "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "$": r"\$", "&": r"\&", "#": r"\#",
    "^": r"\textasciicircum{}", "_": r"\_", "%": r"\%", "~": r"\textasciitilde{}",
}
_SPECIALS_RE = re.compile("|".join(re.escape(c) for c in _SPECIALS))
# A placeholder the escape map leaves alone: NUL never occurs in model text that reached here.
_SLOT = "\x00{}\x00"
_SLOT_RE = re.compile("\x00(\\d+)\x00")
_MISSING_GLYPH = re.compile(r"Missing character: There is no (.+?) \((U\+[0-9A-Fa-f]+)\) in font")


def latex_escape(text: str) -> str:
    """Escape LaTeX's special characters; everything else (Unicode included) passes through."""
    return _SPECIALS_RE.sub(lambda m: _SPECIALS[m.group()], text)


def _anchor(handle: str) -> str:
    return "rcpt-" + re.sub(r"[^A-Za-z0-9]", "-", handle)


def _url(target: str) -> str:
    """A URL for ``\\href``'s first argument: percent-encoded, then its ``%``/``#`` escaped."""
    encoded = url_quote(target, safe=":/?=.-_~@+,")
    return encoded.replace("%", r"\%").replace("#", r"\#")


@dataclass
class _Context:
    """What a cite resolves to while one report renders."""

    lookup: ReceiptLookup
    own_source: str
    figure_numbers: dict[str, int] = field(default_factory=dict)
    reference_numbers: dict[str, int] = field(default_factory=dict)
    cited: list[str] = field(default_factory=list)

    def note(self, handle: str) -> None:
        if handle not in self.cited:
            self.cited.append(handle)

    def cite(self, handle: str) -> str:
        self.note(handle)
        if handle in self.figure_numbers:
            return f"[Fig.~\\ref{{fig:{self.figure_numbers[handle]}}}]"
        if handle in self.reference_numbers:
            n = self.reference_numbers[handle]
            return f"\\protect\\hyperlink{{ref-{n}}}{{[{n}]}}"
        return f"\\protect\\hyperlink{{{_anchor(handle)}}}{{[{latex_escape(handle)}]}}"


def _inline(text: str, ctx: _Context) -> str:
    """One run of prose → LaTeX: bindings rendered, everything else escaped."""
    rendered: list[str] = []

    def slot(latex: str) -> str:
        rendered.append(latex)
        return _SLOT.format(len(rendered) - 1)

    marked = text
    for binding in reversed(parse_bindings(text, own_source=ctx.own_source)):
        ctx.note(binding.handle)
        if binding.quote is None:
            latex = ctx.cite(binding.handle)
        elif binding.locator:
            # A value attributed to a paper: typeset as written, followed by its reference, so the
            # reader sees the source; the value itself was never text-checked (human 2026-09-24).
            latex = f"{latex_escape(binding.quote)}~{ctx.cite(binding.handle)}"
        else:
            latex = latex_escape(binding.quote)
        marked = marked[: binding.start] + slot(latex) + marked[binding.end :]
    return _SLOT_RE.sub(lambda m: rendered[int(m.group(1))], latex_escape(marked))


def _block(text: str, ctx: _Context) -> str:
    """Prose with paragraphs (blank line) and bullet lines ('- ') → LaTeX."""
    out: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        lines = [line.rstrip() for line in paragraph.splitlines() if line.strip()]
        bullets = [line for line in lines if line.lstrip().startswith("- ")]
        if lines and len(bullets) == len(lines):
            items = "\n".join(f"  \\item {_inline(b.lstrip()[2:], ctx)}" for b in bullets)
            out.append(f"\\begin{{itemize}}\n{items}\n\\end{{itemize}}")
        else:
            out.append(_inline(" ".join(line.strip() for line in lines), ctx))
    return "\n\n".join(out)


def parse_delimited(text: str) -> list[list[str]] | None:
    """A table from delimited text (CSV, TSV, ``;``, or Markdown pipe rows), or ``None``.

    ``#`` lines are dropped (toolkit CSVs carry a provenance header). A table needs a header and at
    least one row, two or more columns, and every row as wide as the header; anything else is not
    guessed at.
    """
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) < 2:
        return None
    if all(line.strip().startswith("|") for line in lines):
        rows = [
            [cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in lines
            if not re.fullmatch(r"\|?[\s:\-|]+\|?", line.strip())
        ]
    else:
        try:
            dialect = csv.Sniffer().sniff("\n".join(lines[:20]), delimiters=",\t;")
        except csv.Error:
            return None
        rows = [[cell.strip() for cell in row] for row in csv.reader(io.StringIO("\n".join(lines)), dialect)]
    width = len(rows[0]) if rows else 0
    if width < 2 or len(rows) < 2 or any(len(row) != width for row in rows):
        return None
    return rows


def _longtable(caption: str, number: int, header: Sequence[str], body: Sequence[str], note: str = "") -> str:
    size = "\\footnotesize" if len(header) > 7 else ("\\small" if len(header) > 4 else "")
    spec = "l" * len(header)
    head = " & ".join(header) + r" \\"
    rows = "\n".join(row + r" \\" for row in body)
    tail = f"\\multicolumn{{{len(header)}}}{{l}}{{\\emph{{{note}}}}} \\\\\n" if note else ""
    return (
        f"{{{size}\n\\begin{{longtable}}{{{spec}}}\n"
        f"\\caption{{{caption}}}\\label{{tab:{number}}} \\\\\n\\toprule\n{head}\n\\midrule\n\\endhead\n"
        f"{rows}\n\\bottomrule\n{tail}\\end{{longtable}}\n}}"
    )


def _table(table: ReviewerTable, number: int, ctx: _Context, problems: list[str]) -> str | None:
    label = f"Table {number}"
    caption = _inline(table.caption, ctx) if table.caption.strip() else label
    if table.receipt:
        if table.columns or table.rows:
            problems.append(
                f"{label} names a receipt and also lists columns/rows. Fill one form: the receipt "
                "for a table a tool computed, or columns and rows for a table you build."
            )
            return None
        receipt = ctx.lookup.get(table.receipt)
        text, changed = (
            ctx.lookup.text_with_note(table.receipt)
            if receipt is not None and hasattr(ctx.lookup, "text_with_note")
            else ((ctx.lookup.text(table.receipt), None) if receipt is not None else (None, None))
        )
        parsed = parse_delimited(text) if text is not None else None
        if parsed is None:
            problems.append(
                f"{label} cites {table.receipt}, which is not a receipt you can cite holding a "
                "delimited table (CSV, TSV or Markdown pipe rows). Cite a receipt that holds one, "
                "or build the table from columns and rows."
            )
            return None
        ctx.note(table.receipt)
        header, *data = parsed
        note = latex_escape(changed) if changed else ""  # changed since its receipt (F26): say so
        if len(data) > MAX_TABLE_ROWS:
            more = latex_escape(f"{len(data) - MAX_TABLE_ROWS} further rows are in {table.receipt}.")
            note = f"{note} {more}".strip()
            data = data[:MAX_TABLE_ROWS]
        source = f"{caption} (from \\protect\\hyperlink{{{_anchor(table.receipt)}}}{{[{latex_escape(table.receipt)}]}})"
        return _longtable(
            source, number,
            [latex_escape(h) for h in header],
            [" & ".join(latex_escape(c) for c in row) for row in data],
            note,
        )
    if not table.columns or not table.rows:
        problems.append(
            f"{label} has neither a receipt nor columns and rows. Cite the receipt of a table a "
            "tool computed, or list the columns and rows of the table you build."
        )
        return None
    bad = [i for i, row in enumerate(table.rows, 1) if len(row) != len(table.columns)]
    if bad:
        problems.append(
            f"{label} has {len(table.columns)} columns, but row(s) {', '.join(map(str, bad))} "
            "have a different number of cells."
        )
        return None
    return _longtable(
        caption, number,
        [_inline(c, ctx) for c in table.columns],
        [" & ".join(_inline(c, ctx) for c in row) for row in table.rows],
    )


def _figure(receipt: Receipt, relpath: str, number: int, ctx: _Context) -> str:
    caption = latex_escape(receipt.caption or receipt.handle)
    link = f"\\protect\\hyperlink{{{_anchor(receipt.handle)}}}{{[{latex_escape(receipt.handle)}]}}"
    return (
        "\\begin{figure}[htbp]\n\\centering\n"
        f"\\includegraphics[width=\\linewidth,height=0.45\\textheight,keepaspectratio]{{{relpath}}}\n"
        f"\\caption{{{caption} {link}}}\\label{{fig:{number}}}\n\\end{{figure}}"
    )


def _mentions(text: str, word: str, number: int) -> bool:
    pattern = rf"\b{word}s?\.?\s*{number}\b" if word == "Table" else rf"\bFig(?:ure)?s?\.?\s*{number}\b"
    return re.search(pattern, text) is not None


def _reference(receipt: Receipt, number: int) -> str:
    parts = [latex_escape(receipt.title or receipt.handle)]
    ids = receipt.ids or {}
    if ids.get("arxiv"):
        parts.append(f"\\href{{{_url('https://arxiv.org/abs/' + ids['arxiv'])}}}{{arXiv:{latex_escape(ids['arxiv'])}}}")
    if ids.get("doi"):
        parts.append(f"\\href{{{_url('https://doi.org/' + ids['doi'])}}}{{doi:{latex_escape(ids['doi'])}}}")
    if ids.get("bibcode"):
        parts.append(
            f"\\href{{{_url('https://ui.adsabs.harvard.edu/abs/' + ids['bibcode'])}}}"
            f"{{{latex_escape(ids['bibcode'])}}}"
        )
    if ids.get("url") and not (ids.get("arxiv") or ids.get("doi") or ids.get("bibcode")):
        parts.append(f"\\href{{{_url(ids['url'])}}}{{{latex_escape(ids['url'])}}}")
    parts.append(f"\\protect\\hyperlink{{{_anchor(receipt.handle)}}}{{[{latex_escape(receipt.handle)}]}}")
    return f"\\item[\\hypertarget{{ref-{number}}}{{[{number}]}}] " + ". ".join(parts)


def _appendix(handles: Iterable[str], lookup: ReceiptLookup) -> str:
    rows: list[str] = []
    for handle in handles:
        receipt = lookup.get(handle)
        if receipt is None:
            continue
        # A text blob's file name is its hash, already in its own column; name only real files.
        what = receipt.title or receipt.caption or (
            Path(receipt.path).name if receipt.kind in ("data", "figure") else "--"
        )
        flags = ", ".join(receipt.flags) or "--"
        rows.append(
            f"\\hypertarget{{{_anchor(handle)}}}{{\\texttt{{{latex_escape(handle)}}}}} & "
            f"{latex_escape(receipt.kind)} & {latex_escape(receipt.produced_by)} & "
            f"\\texttt{{{receipt.sha256[:12]}}} & {latex_escape(flags)} & {latex_escape(what)} \\\\"
        )
    if not rows:
        return ""
    return (
        "\\appendix\n\\section{Provenance}\n"
        "Every artifact this report cites, as recorded by the runtime when it was produced. The "
        "full derivation chain of each is in the run's receipt store.\n\n"
        "{\\footnotesize\n\\begin{longtable}{lllllp{4.5cm}}\n\\toprule\n"
        "Handle & Kind & Produced by & SHA-256 & Flags & Artifact \\\\\n\\midrule\n\\endhead\n"
        + "\n".join(rows)
        + "\n\\bottomrule\n\\end{longtable}\n}"
    )


def report_attributions(
    report: ReviewerReport, lookup: ReceiptLookup, *, own_source: str
) -> list[PaperAttribution]:
    """Every value the report attributes to a paper, in reading order, each once (the verify list)."""
    seen: dict[tuple[str, str, str], PaperAttribution] = {}
    for value in report.prose_fields().values():
        for text in [value] if isinstance(value, str) else value:
            for item in paper_attributions(text, lookup, own_source=own_source):
                seen.setdefault((item.handle, item.value, item.locator), item)
    return list(seen.values())


def _verify_list(attributions: list[PaperAttribution], ctx: "_Context") -> str:
    """The values a human checks against the PDFs: they were attributed, not text-matched."""
    if not attributions:
        return ""
    rows = "\n".join(
        f"{ctx.cite(a.handle)} & {latex_escape(a.value)} & {latex_escape(a.locator.strip())} \\\\"
        for a in attributions
    )
    return (
        "\\section{Values to verify against the PDFs}\n"
        "Each value below is attributed to a paper and was not checked against its text. Open the "
        "paper at the stated place to confirm it.\n\n"
        "{\\small\n\\begin{longtable}{lll}\n\\toprule\nPaper & Value & Where \\\\\n\\midrule\n"
        "\\endhead\n" + rows + "\n\\bottomrule\n\\end{longtable}\n}"
    )


def _preamble(title: str, run_id: str) -> str:
    return (
        "\\documentclass[11pt]{article}\n"
        "\\usepackage{fontspec}\n"
        "\\directlua{luaotfload.add_fallback('reportfallback', {"
        + ", ".join(f"'{font}'" for font in FALLBACK_FONTS)
        + "})}\n"
        f"\\setmainfont{{{MAIN_FONT}}}[RawFeature={{fallback=reportfallback}}]\n"
        "\\usepackage[margin=1in]{geometry}\n"
        "\\usepackage{graphicx}\n"
        "\\usepackage{longtable}\n"
        "\\usepackage{booktabs}\n"
        "\\usepackage[colorlinks=true,linkcolor=blue,urlcolor=blue]{hyperref}\n"
        f"\\title{{{title}}}\n"
        f"\\author{{auto\\_research run \\texttt{{{latex_escape(run_id)}}}}}\n"
        "\\date{}\n"
    )


@dataclass
class RenderResult:
    """One render of a report: where the ``.tex`` went, and what the model has to fix."""

    tex_path: Path | None
    problems: list[str] = field(default_factory=list)
    cited: list[str] = field(default_factory=list)
    references: list[Receipt] = field(default_factory=list)
    figures: list[tuple[Receipt, str]] = field(default_factory=list)


def render_report(
    report: ReviewerReport,
    lookup: ReceiptLookup,
    report_dir: Path,
    *,
    run_id: str,
    own_source: str = "rev",
) -> RenderResult:
    """Write ``report_dir/report.tex`` from a verified report.

    Nothing is written while a model-fixable problem stands, so a stale ``.tex`` is never mistaken
    for this report's rendering.
    """
    problems: list[str] = []
    staged, stage_problems = stage_figure_receipts(report.figures, lookup, report_dir)
    problems.extend(stage_problems)
    ctx = _Context(lookup=lookup, own_source=own_source)
    ctx.figure_numbers = {receipt.handle: i for i, (receipt, _) in enumerate(staged, 1)}

    # References are numbered by first citation, across every field in reading order.
    for text in report.prose_fields().values():
        for item in [text] if isinstance(text, str) else text:
            for binding in parse_bindings(item, own_source=own_source):
                receipt = lookup.get(binding.handle)
                if receipt is not None and receipt.kind in ("paper", "web"):
                    ctx.reference_numbers.setdefault(binding.handle, len(ctx.reference_numbers) + 1)

    tables = [_table(t, i, ctx, problems) for i, t in enumerate(report.tables, 1)]
    if problems:
        return RenderResult(tex_path=None, problems=problems)

    figure_blocks = {i: _figure(r, rel, i, ctx) for i, (r, rel) in enumerate(staged, 1)}
    for receipt, _ in staged:
        ctx.note(receipt.handle)
    table_blocks = {i: block for i, block in enumerate(tables, 1) if block}
    placed: set[tuple[str, int]] = set()

    def floats_after(text: str) -> list[str]:
        out: list[str] = []
        for word, blocks in (("Figure", figure_blocks), ("Table", table_blocks)):
            for number, block in blocks.items():
                if (word, number) not in placed and _mentions(text, word, number):
                    placed.add((word, number))
                    out.append(block)
        return out

    body: list[str] = [
        _preamble(_inline(report.title, ctx) if report.title.strip() else "Research report", run_id),
        "\\begin{document}\n\\maketitle",
    ]
    if report.abstract.strip():
        body.append(f"\\begin{{abstract}}\n{_block(report.abstract, ctx)}\n\\end{{abstract}}")
    for section in report.sections:
        body.append(f"\\section{{{_inline(section.heading, ctx)}}}\n{_block(section.body, ctx)}")
        body.extend(floats_after(section.heading + "\n" + section.body))
    if report.key_findings:
        items = "\n".join(f"  \\item {_inline(f, ctx)}" for f in report.key_findings)
        body.append(f"\\section{{Key findings}}\n\\begin{{itemize}}\n{items}\n\\end{{itemize}}")
        body.extend(floats_after("\n".join(report.key_findings)))
    body.extend(b for n, b in figure_blocks.items() if ("Figure", n) not in placed)
    body.extend(b for n, b in table_blocks.items() if ("Table", n) not in placed)

    references = [lookup.get(h) for h in ctx.reference_numbers]
    references = [r for r in references if r is not None]
    if references:
        entries = "\n".join(_reference(r, ctx.reference_numbers[r.handle]) for r in references)
        body.append(f"\\section*{{References}}\n\\begin{{description}}\n{entries}\n\\end{{description}}")
    body.append(_appendix(ctx.cited, lookup))
    body.append(_verify_list(report_attributions(report, lookup, own_source=own_source), ctx))
    body.append("\\end{document}\n")

    report_dir.mkdir(parents=True, exist_ok=True)
    tex_path = report_dir / TEX_NAME
    tex_path.write_text("\n\n".join(part for part in body if part), encoding="utf-8")
    return RenderResult(
        tex_path=tex_path, cited=list(ctx.cited), references=references, figures=staged,
    )


@dataclass
class CompileResult:
    """``glyph_problems`` are the model's to fix; ``errors`` are the renderer's."""

    pdf_path: Path | None
    glyph_problems: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def compile_report(
    report_dir: Path,
    *,
    compiler: str = LATEX_COMPILER,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    timeout_s: int = 120,
) -> CompileResult:
    """Compile ``report.tex`` twice (labels and longtable widths settle on the second pass)."""
    if not shutil.which(compiler):
        return CompileResult(pdf_path=None, errors=[f"{compiler} is not on PATH, so no PDF was produced."])
    log_text = ""
    for _ in range(2):
        try:
            result = run(
                [compiler, "-interaction=nonstopmode", "-halt-on-error", TEX_NAME],
                cwd=report_dir, capture_output=True, text=True, timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return CompileResult(pdf_path=None, errors=[f"{compiler} timed out after {timeout_s} s"])
        log_path = report_dir / "report.log"
        log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else result.stdout
        if result.returncode != 0:
            errors = _errors_with_context(result.stdout) or [f"{compiler} exited with code {result.returncode}"]
            return CompileResult(pdf_path=None, errors=errors, glyph_problems=_glyphs(log_text))
    pdf = report_dir / "report.pdf"
    return CompileResult(pdf_path=pdf if pdf.is_file() else None, glyph_problems=_glyphs(log_text))


def _glyphs(log_text: str) -> list[str]:
    missing = dict.fromkeys(f"{char} ({code})" for char, code in _MISSING_GLYPH.findall(log_text))
    return [
        f"The report's font cannot typeset {what}. Rewrite that character in plain words or with a "
        "common symbol, and call turn_end again."
        for what in missing
    ]
