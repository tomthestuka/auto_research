"""Binding claims to receipts: the check every role's ``turn_end`` runs over its report prose.

Two forms of citation live in prose:

* ``[src:r12]`` cites a whole receipt. It scopes the text it sits in: every number in that text
  must appear, on token boundaries, in one of the receipts the text cites (*scoped grounding*).
* ``{{src:r12 "PD = 4.02 ± 0.21 %"}}`` pins an exact span. The quote must be a bounded substring
  of that receipt's stored text.
* ``{{src:r12#/pd/value}}`` is a value reference (user ruling 2026-10-27, every value passes as a
  handle): the runtime reads the value at that JSON pointer of the result the tool returned and
  renders it (:func:`display_value`), so the model never types it. A typed number a cited result
  returned is refused with the reference to write.

A number found in none of its cited receipts is a problem, named with its context, so the role
fixes it inside its loop: cite the receipt it came from, quote it, or compute it with a tool so the
arithmetic becomes a receipt too. A receipt flagged ``failed`` or ``synthetic`` grounds nothing.

Everything here is pure: the caller supplies the prose and a :class:`ReceiptLookup`. The same
functions serve every unit (worker, lead, reviewer, librarian); a caller with markup (LaTeX)
extracts prose first and passes ``exempt_spans`` for what is not prose.
"""

from __future__ import annotations

import difflib
import math
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from auto_research.provenance.store import INPUT_SOURCE, NOT_EVIDENCE_NAMES, is_evidence
from auto_research.schemas.artifacts import HANDLE_PATTERN, PaperAttribution, Receipt

_H = rf"(?:self|{HANDLE_PATTERN.split(':', 1)[0]}):[frdpwhc][1-9][0-9]*"
CITE_RE = re.compile(rf"\[(?P<h>{_H})\]")
QUOTE_RE = re.compile(rf'\{{\{{\s*(?P<h>{_H})\s+"(?P<q>[^"\n]+)"(?P<loc>[^}}\n]*)\}}\}}')
#: A value reference: ``{{analyst_worker_0:r12#/pd/value}}`` (RFC 6901 pointer, no spaces).
#: ``{{h#}}`` names the whole returned value; a key may hold spaces (``#/2-3 keV/q``). A full handle
#: only: ``self:`` would resolve to whoever copies the reference.
VALUE_RE = re.compile(rf"\{{\{{\s*(?P<h>{HANDLE_PATTERN})#(?P<p>(?:/[^{{}}\n]*?)?)\s*\}}\}}")
#: What is left of ``{{`` markup once every binding is taken out: written as a binding, read as none.
_MARKUP_LEFT = re.compile(r"\{\{|\}\}")

#: Kinds a value is ATTRIBUTED to rather than quoted from (human ruling 2026-09-24: papers are
#: cited, never text-checked). ``{{lib:p3 "4.0 ± 0.2 %" Table 2}}`` names the paper and where in it;
#: the value is recorded for a human to verify against the PDF, not matched against any text.
ATTRIBUTION_KINDS = frozenset({"paper"})

#: A number in prose. A sign counts only when it is not glued to a word or a closing bracket, so
#: the hyphen in "DU-3" is not read as a minus.
NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9_.])(?<![A-Za-z]-)(?:(?<![A-Za-z0-9_.)\]])[+\-−](?=\.?\d))?"
    r"\d+(?:\.\d+)?(?:[eE][+\-−]?\d+)?"
)

#: Numbers that name things rather than state results. Kept deliberately short (2026-09-24).
_EXEMPT_RES = (
    # "Figure 3", "Figs. 2–5", "Table 1", "Eq. 4"
    re.compile(
        r"\b(?:Fig(?:ure)?s?\.?|Tables?|Eqs?\.?|Equations?)\s*\(?\d+(?:\s*(?:[–\-]|and|,)\s*\d+)*\)?",
        re.IGNORECASE,
    ),
    # a year, which also covers "et al. 2022"
    re.compile(r"(?<![\d.])(?:19|20)\d{2}(?![\d.])"),
    # a sexagesimal right ascension or declination written as a name: "00h00m", "12h34m56.7s"
    re.compile(r"\b\d{1,2}h\d{1,2}m(?:\d{1,2}(?:\.\d+)?s)?"),
    # a list enumerator: "(1) …", "2) …", or "3. " opening a line
    re.compile(r"(?:(?<=^)|(?<=[\s:;,]))\(\d{1,2}\)|^\s*\d{1,2}[.)](?=\s)", re.MULTILINE),
    # a persistent identifier: "arXiv 2101.00001v2", "astro-ph/0601001", a DOI, an ADS bibcode.
    # An arXiv id needs its prefix, or a measured "3456.12345" would read as one.
    re.compile(
        r"\barXiv[:\s]*\d{4}\.\d{4,5}(?:v\d+)?|\b[a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?"
        r"|\b10\.\d{4,9}/[^\s,;)\]]+|\b\d{4}[A-Za-z&.]{5}[\d.]{4}[A-Z.][\d.]{4}[A-Z]\b"
    ),
    # a hex identifier (an environment fingerprint, a commit SHA): 12+ lowercase hex characters with
    # a letter other than e (`602214076000e12` is a number). The runtime prints these beside every result; restating one is not a value.
    re.compile(r"\b(?=[0-9a-f]*[a-df])[0-9a-f]{12,64}\b"),  # a-d/f: `e` alone is an exponent
    # an environment fingerprint as the runtime prints it, whatever its hex digits: "env:1234e5678901"
    re.compile(r"\benv:[0-9a-f]{6,64}\b"),
    # an HTTP status code named with its reason phrase: "401 Unauthorized", "HTTP 429"
    re.compile(
        r"\bHTTP\s*[1-5]\d\d\b|\b[1-5]\d\d\s+(?:Unauthorized|Forbidden|Not Found|Too Many Requests|"
        r"Bad Request|Internal Server Error|Service Unavailable|Bad Gateway|Gateway Timeout|"
        r"Payment Required|Request Timeout|Conflict|Unprocessable)\b",
        re.IGNORECASE,
    ),
    # an ordinal that names a role, a unit or a step: "worker 0", "DU 2", "round 3", "link 1"
    # (measured live 2026-09-24: "Worker 0 … worker 1" refused in four of ten lead cells)
    re.compile(
        r"\b(?:analyst[ _]?worker|worker|analyst|round|rounds|step|steps|link|links|iteration|"
        r"DU|detector unit|chain link|slot|trial|cell)[ _]?#?\d+\b",
        re.IGNORECASE,
    ),
    # an astronomical source designation: "GRS 1915+105", "4U 1630−47", "Swift J1727.8−1613"
    re.compile(r"\b[0-9]?[A-Z][A-Za-z]{0,6}\s?J?\d{3,4}(?:\.\d+)?[+\-−]\d{1,4}(?:\.\d+)?\b"),
)

_LIGATURES = str.maketrans({"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl"})

#: Mathematical Alphanumeric Symbols (U+1D400–U+1D7FF) fold to their plain letter or digit: PDF
#: extraction emits math-italic 𝜎 where a model types σ (measured on the Docling cache by the
#: librarian session). Only this block is compatibility-folded, never NFKC as a whole — superscripts
#: stay distinct, so 10²³ still never reads as 1023.
_MATH_ALNUM = {
    cp: unicodedata.normalize("NFKC", chr(cp))
    for cp in range(0x1D400, 0x1D800)
    if unicodedata.name(chr(cp), "")
}
_PDF_KINDS = frozenset({"paper", "web"})
_MAX_LISTED = 8



#: Significant digits a value reference displays (user ruling 2026-10-27: fixed by the runtime;
#: the full value is one click away in the report, and ``view_receipt`` for a model).
DISPLAY_DIGITS = 3

#: A typed number with at least this many significant digits that a cited result returned is a
#: copy: refused with the reference to write (the prose twin of ``refs.RetypeCheck``).
COPY_MIN_DIGITS = 3


def display_value(value: object) -> str:
    """A referenced value as every reader sees it: an integer whole, a float to
    :data:`DISPLAY_DIGITS` significant digits (``2.72``, ``0.0216``, ``12300``, ``4.3e6`` outside
    1e-3..1e6), a string or a boolean as written. Plain text; a renderer may typeset ``e``."""
    if isinstance(value, bool) or value is None:
        return str(value).lower() if value is not None else "null"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return str(value)
        if value == 0:
            return "0"
        exponent = math.floor(math.log10(abs(value)))
        if abs(round(value, DISPLAY_DIGITS - 1 - exponent)) >= 10.0 ** (exponent + 1):
            exponent += 1  # rounding carried into the next power of ten (9.9996 → 10.0)
        if -3 <= exponent < 6:
            decimals = max(0, DISPLAY_DIGITS - 1 - exponent)
            rounded = round(value, DISPLAY_DIGITS - 1 - exponent)
            return f"{rounded:.{decimals}f}"
        mantissa, _, power = f"{value:.{DISPLAY_DIGITS - 1}e}".partition("e")
        return f"{mantissa}e{int(power)}"
    return str(value)


def _significant_digits(token: str) -> int:
    """Significant digits as written: ``2.00`` has 3, ``100`` has 1 (an integer's trailing zeros
    are placeholders), ``0.0215`` has 3."""
    mantissa = re.split(r"[eE]", token.lstrip("+-−"))[0]
    digits = mantissa.replace(".", "").lstrip("0")
    return len(digits if "." in mantissa else digits.rstrip("0"))


def _as_float(token: str) -> float | None:
    try:
        return float(token.replace("−", "-"))
    except ValueError:
        return None


def _numeric_leaves(value: object, path: str = "") -> Iterable[tuple[float, str]]:
    """Every number in a returned value with its JSON pointer (booleans are not numbers)."""
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        yield float(value), path
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _numeric_leaves(item, f"{path}/{str(key).replace('~', '~0').replace('/', '~1')}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _numeric_leaves(item, f"{path}/{index}")


#: A returned value larger than this is not parsed for references or copies (each turn_end reads it).
_MAX_RETURNED_BYTES = 2_000_000


def _blob_size(store: object, sha256: str) -> int:
    try:
        return store.blob_path(sha256).stat().st_size  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return 0


class ReceiptLookup(Protocol):
    """What the check needs from a receipt store (:class:`~auto_research.provenance.store.ReceiptStore`)."""

    def get(self, handle: str) -> Receipt | None: ...
    def text(self, handle: str) -> str | None: ...
    def verify(self, handle: str) -> str | None: ...


@dataclass(frozen=True)
class Binding:
    """One citation in prose: ``[start, end)`` span, the resolved handle, the quote if pinned."""

    start: int
    end: int
    handle: str
    quote: str | None
    locator: str | None = None
    #: A value reference's JSON pointer (``/pd/value``); ``None`` for a quote or a cite.
    pointer: str | None = None


@dataclass(frozen=True)
class NumberSource:
    """One number in prose, ``[start, end)``, and the receipts that carry it on token boundaries:
    the ones the text cites first, then the rest of its scope. Empty = ungrounded."""

    start: int
    end: int
    value: str
    handles: tuple[str, ...]


@dataclass
class BindingResult:
    problems: list[str] = field(default_factory=list)
    bindings: list[Binding] = field(default_factory=list)
    #: Filled when asked (``sources=True``): every scanned number of the text with the receipts
    #: holding it, keyed by the text (``check_report_fields`` merges every field's).
    numbers: dict[str, list[NumberSource]] = field(default_factory=dict)

    @property
    def cited(self) -> list[str]:
        return list(dict.fromkeys(b.handle for b in self.bindings))


def normalise(text: str, kind: str) -> str:
    """The one normalisation both sides of every comparison go through (never what is rendered).

    NFC, never NFKC: NFKC maps ``10²³`` to ``1023`` and would let a quote match a different
    number. A U+2212 minus before a digit becomes ``-`` (sign-preserving; tools print ASCII).
    Text: whitespace runs collapse to one space. Paper/web (PDF extraction splits number tokens,
    ``4 . 0 ± 0 . 2``): line-end hyphens rejoin, ligatures expand, and whitespace is removed except
    between two digits, where it stays one separator so ``4 02`` never reads as ``402``.
    """
    out = unicodedata.normalize("NFC", text).translate(_MATH_ALNUM)
    out = re.sub(r"−(?=\.?\d)", "-", out)
    if kind in _PDF_KINDS:
        # A hyphen before whitespace and a lowercase letter is a line break inside a word, on either
        # side: the raw ``respec-\ntively`` and the collapsed ``respec- tively`` a hint displays
        # both read as ``respectively``. A letter must precede it, so no number is touched.
        out = re.sub(r"(?<=[A-Za-z])-\s+(?=[a-z])", "", out).translate(_LIGATURES)

        def _space(m: re.Match) -> str:
            before = out[m.start() - 1] if m.start() else ""
            after = out[m.end()] if m.end() < len(out) else ""
            beyond = out[m.end() + 1] if m.end() + 1 < len(out) else ""
            if before.isdigit() and after.isdigit():
                return " "
            # A sign glued to its own number after a number (a table row "0.2 −22") keeps a
            # separator, or the value reads as a range "0.2-22". A sign with space on both sides
            # ("2 - 8") is an operator or a range and still closes up. (Found live by the reviewer.)
            if before.isdigit() and after in "+-" and (beyond.isdigit() or beyond == "."):
                return " "
            return ""

        return re.sub(r"\s+", _space, out)
    return re.sub(r"\s+", " ", out).strip()


def _ok_before(hay: str, i: int, first: str) -> bool:
    if i == 0:
        return True
    prev = hay[i - 1]
    if first.isdigit() or first == ".":
        if prev.isdigit() or prev == ".":
            return False
        if prev in "+-":
            # "+/-0.2" is an error bar and "2-8" a range; only a sign with no number before it
            # makes "0.51" inside "-0.51" a different value.
            before = hay[i - 2] if i >= 2 else ""
            return before == "/" or before.isdigit()
        return True
    if first in "+-":
        return not (prev.isdigit() or prev == ".")  # "10-20" is a range, not "-20"
    return True


def _ok_after(hay: str, j: int, last: str) -> bool:
    if j >= len(hay) or not last.isdigit():
        return True
    nxt = hay[j]
    rest = hay[j + 1 : j + 3]
    if nxt.isdigit():
        return False
    if nxt == "." and rest[:1].isdigit():
        return False
    if nxt in "eE" and re.match(r"[+\-]?\d", rest):
        return False
    return True


def bounded_find(hay: str, needle: str) -> bool:
    """``needle`` occurs in ``hay`` on token boundaries (no "0.51" inside "-0.51" or "10.51")."""
    if not needle:
        return False
    start = 0
    while True:
        i = hay.find(needle, start)
        if i < 0:
            return False
        if _ok_before(hay, i, needle[0]) and _ok_after(hay, i + len(needle), needle[-1]):
            return True
        start = i + 1


def ungrounded_numbers(
    prose: str,
    usable: Mapping[str, str],
    *,
    masked: Sequence[tuple[int, int]] = (),
) -> list[re.Match]:
    """Each number in ``prose`` that no normalised text in ``usable`` carries on token boundaries.

    The one grounding rule for a number, shared by ``check_bindings`` (a report against its receipts)
    and the gate's return check (a tool's returned values against its own receipted output). A
    number inside ``masked`` or an exempt shape (Figure/Table/Eq. numbers, years, …) is skipped.
    """
    return [m for m in _scanned_numbers(prose, masked) if not any(
        bounded_find(hay, normalise(m.group(), "result")) for hay in usable.values()
    )]


def _scanned_numbers(prose: str, masked: Sequence[tuple[int, int]]) -> list[re.Match]:
    """The numbers the grounding rule checks: every number outside ``masked`` and the exempt shapes."""
    spans = list(masked)
    for pattern in _EXEMPT_RES:
        spans.extend(m.span() for m in pattern.finditer(prose))
    return [m for m in NUMBER_RE.finditer(prose) if not any(s <= m.start() < e for s, e in spans)]


def number_sources(
    prose: str,
    usable: Mapping[str, str],
    *,
    masked: Sequence[tuple[int, int]] = (),
) -> list[NumberSource]:
    """Each number the grounding rule checks, with every receipt in ``usable`` (in its order) that
    carries it on token boundaries — what a reader is shown as the number's source."""
    out: list[NumberSource] = []
    for m in _scanned_numbers(prose, masked):
        token = normalise(m.group(), "result")
        held = tuple(h for h, hay in usable.items() if bounded_find(hay, token))
        out.append(NumberSource(m.start(), m.end(), m.group(), held))
    return out


def parse_bindings(prose: str, *, own_source: str | None = None) -> list[Binding]:
    """Every citation in ``prose``, in order, with ``self:`` resolved to ``own_source``."""

    def resolve(handle: str) -> str:
        if handle.startswith("self:") and own_source:
            return f"{own_source}:{handle.split(':', 1)[1]}"
        return handle

    found = [
        Binding(m.start(), m.end(), m.group("h"), None, pointer=m.group("p"))  # a full handle, never self:
        for m in VALUE_RE.finditer(prose)
    ]
    covered = [(b.start, b.end) for b in found]
    for m in QUOTE_RE.finditer(prose):
        if not any(s < m.end() and m.start() < e for s, e in covered):
            found.append(Binding(m.start(), m.end(), resolve(m.group("h")), m.group("q"),
                                 (m.group("loc") or "").strip() or None))
    covered = [(b.start, b.end) for b in found]
    for m in CITE_RE.finditer(prose):
        if not any(s <= m.start() < e for s, e in covered):
            found.append(Binding(m.start(), m.end(), resolve(m.group("h")), None))
    return sorted(found, key=lambda b: b.start)


def _quote_hint(resolved: "_Resolved", handle: str, quote: str, kind: str) -> str:
    """What to tell a model whose quote failed: where it does appear, or the nearest passage.

    Two live cases shaped it (2026-09-24). A quote copied from a *different* receipt than the one
    cited is named with the receipt that holds it. And a checked text that lost a sign the quote
    carries (a PDF text layer dropping a table row's minus) must not be offered as the thing to
    copy, since copying it would put the wrong sign in the report.
    """
    handles = getattr(resolved.lookup, "handles", None)
    if callable(handles):
        for other in handles():
            if other == handle:
                continue
            receipt, _, hay = resolved.entry(other)
            if receipt is not None and hay is not None and bounded_find(
                hay, normalise(quote, receipt.kind)
            ):
                return f" It does appear in {other}; cite that receipt instead."
    resolved.entry(handle)  # the checked read; the hint is drawn from those bytes, not a re-read
    closest = _closest_passage(resolved.raw.get(handle) or "", quote, kind)
    if not closest:
        return ""
    quoted, shown = normalise(quote, kind), normalise(closest, kind)
    for magnitude in re.findall(r"-(\d[\d.]*)", quoted):
        if magnitude in shown and not bounded_find(shown, "-" + magnitude):
            return (
                f" The checked text of {handle} shows {magnitude} without the sign your quote "
                "carries, so its text extraction may have lost the sign. Do not copy the unsigned "
                "form; state the value as the page shows it in prose without a quote, and say in "
                "the uncertainty that the receipt's text lacks the sign."
            )
    return f' The closest passage in {handle} reads: "{closest}".'


def _closest_passage(text: str, quote: str, kind: str = "result") -> str:
    """The receipt passage nearest a failed quote, so a model that cannot see the checked text can
    re-copy it. Quotable by construction: the span is snapped to whole words, a word broken across a
    line is dropped from either end, and words are trimmed until the span itself passes the check
    (found live: a hint ending in ``respec-`` could not match text whose hyphen had been rejoined).
    """
    display = re.sub(r"\s+", " ", unicodedata.normalize("NFC", text).translate(_MATH_ALNUM))
    needle = re.sub(r"\s+", " ", unicodedata.normalize("NFC", quote).translate(_MATH_ALNUM))
    if not display or not needle:
        return ""
    match = difflib.SequenceMatcher(None, display, needle, autojunk=False).find_longest_match(
        0, len(display), 0, len(needle)
    )
    if match.size < 3:
        return ""
    start = max(0, match.a - match.b - 10)
    end = min(len(display), start + len(needle) + 20)
    words = display[start:end].split(" ")
    if start > 0 and display[start - 1] != " ":
        words = words[1:]  # began mid-word
    if end < len(display) and display[end] != " ":
        words = words[:-1]  # ended mid-word
    hay = normalise(text, kind)
    while words and (words[-1].endswith("-") and words[-1] != "-"):
        words = words[:-1]
    while words and words[0].endswith("-") and words[0] != "-" and len(words) > 1:
        words = words[1:]
    trim_end = True
    while words:
        span = " ".join(words).strip()
        if span and bounded_find(hay, normalise(span, kind)):
            return span
        words = words[:-1] if trim_end else words[1:]
        trim_end = not trim_end
    return ""


_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+(?=[A-Z(\[{\"'])|\n\s*\n")


def _sentences(prose: str) -> list[tuple[int, int]]:
    """Sentence spans: a break after . ! ? ; followed by an opening capital or bracket, or a blank
    line. A decimal point never ends one (no whitespace follows it)."""
    spans, start = [], 0
    for m in _SENTENCE_END.finditer(prose):
        spans.append((start, m.start()))
        start = m.end()
    spans.append((start, len(prose)))
    return spans


def _context(prose: str, start: int, end: int) -> str:
    return prose[max(0, start - 30) : end + 15].replace("\n", " ").strip()


class _Resolved:
    """One lookup's answers, computed once per check (hashing and normalising a paper is not free)."""

    def __init__(self, lookup: ReceiptLookup) -> None:
        self.lookup = lookup
        self._entries: dict[str, tuple[Receipt | None, str | None, str | None]] = {}
        #: The raw text each entry was checked against: hints are drawn from these same bytes (F26).
        self.raw: dict[str, str] = {}
        self._returned: dict[str, tuple[object, str | None]] = {}
        self._leaves: dict[str, dict[float, str]] = {}

    def entry(self, handle: str) -> tuple[Receipt | None, str | None, str | None]:
        """``(receipt, problem, normalised text)``; ``receipt`` is None when it cannot be used."""
        if handle not in self._entries:
            receipt = self.lookup.get(handle)
            if receipt is None:
                self._entries[handle] = (None, f"cites {handle}, which is not a receipt you can cite.", None)
            elif not is_evidence(receipt):
                # Recorded, never evidence (human ruling 2026-09-25): what a role read, searched,
                # was told or ran in the shell shows where to look; only what the analysis produced
                # backs a number. In scope it is skipped silently; cited, it is named.
                what = NOT_EVIDENCE_NAMES.get(receipt.produced_by, f"a {receipt.produced_by} return")
                self._entries[handle] = (None, (
                    f"cites {handle}, {what}: it shows where to look, not evidence. Cite the toolkit "
                    "result the value came from, or attribute the paper with its place in it; state "
                    "anything else without a value."
                ), None)
            else:
                problem, text = self._checked_text(handle, receipt)
                if problem:
                    self._entries[handle] = (None, problem, None)
                else:
                    if text is not None:
                        self.raw[handle] = text
                    hay = normalise(text, receipt.kind) if text is not None else None
                    self._entries[handle] = (receipt, None, hay)
        return self._entries[handle]

    def returned(self, handle: str) -> tuple[object, str | None]:
        """``(value, problem)``: what the tool returned for a usable cited result. ``problem`` says
        why a reference into it cannot be read (not a toolkit result, text only, changed bytes)."""
        if handle not in self._returned:
            from auto_research.provenance.refs import RefError, passable, returned_value

            receipt, problem, _ = self.entry(handle)
            store = getattr(self.lookup, "store", self.lookup)
            if receipt is None:
                self._returned[handle] = (None, problem.removeprefix("cites ") if problem else None)
            elif receipt.returned_sha256 and _blob_size(store, receipt.returned_sha256) > _MAX_RETURNED_BYTES:
                self._returned[handle] = (None, (
                    f"{handle} returned more than {_MAX_RETURNED_BYTES // 1_000_000} MB; reference a "
                    "result that summarises it"))
            elif not passable(store, handle):
                self._returned[handle] = (None, (
                    f"{handle} is a {receipt.kind} receipt, not a toolkit result that returned values; "
                    "cite it as [handle] or quote its text"))
            else:
                try:
                    self._returned[handle] = (returned_value(store, handle), None)
                except RefError as exc:
                    self._returned[handle] = (None, str(exc))
                except ValueError as exc:  # stored bytes that are not JSON
                    self._returned[handle] = (None, f"{handle}'s returned value cannot be read ({exc})")
                except AttributeError:  # a duck-typed lookup with no stored bytes to read
                    self._returned[handle] = (None, f"{handle} keeps no returned values here")
        return self._returned[handle]

    def leaves(self, handle: str) -> dict[float, str]:
        """``{number: pointer}`` for every number a cited result returned that its call was not
        given (an echoed argument is the caller's parameter, not a result to copy)."""
        if handle not in self._leaves:
            value, problem = self.returned(handle)
            held: dict[float, str] = {}
            if problem is None:
                from auto_research.provenance.refs import RetypeCheck

                store = getattr(self.lookup, "store", self.lookup)
                try:
                    given = {number for number, _ in _numeric_leaves(RetypeCheck(store)._args_of(handle))}
                except (ValueError, AttributeError):
                    given = set()
                for number, pointer in _numeric_leaves(value):
                    if number not in given:
                        held.setdefault(number, pointer)
            self._leaves[handle] = held
        return self._leaves[handle]

    def _checked_text(self, handle: str, receipt) -> tuple[str | None, str | None]:
        """The problem refusing this citation, or the text it is checked against.

        One read: the bytes checked against the receipt are the bytes the quote is matched in
        (F26), so nothing can be swapped between the two. A citation whose bytes changed since its
        receipt is refused -- it would ground a number on bytes no receipt vouches for.
        """
        from auto_research.provenance.store import TEXT_KINDS, checkable_text

        if receipt.source_path is not None:  # a paper/page: its original file is checked too
            problem = self.lookup.verify(handle)
            if problem:
                return problem, None
        read_fn = getattr(self.lookup, "read", None)
        if read_fn is None:  # a duck-typed lookup without the one-read path
            problem = self.lookup.verify(handle)
            return (problem, None) if problem else (None, self.lookup.text(handle))
        read = read_fn(handle)
        if read is None or read.data is None:
            return (read.note if read is not None else f"{handle} is not a receipt in this run."), None
        if read.changed:
            return (f"{handle}: the bytes at {receipt.path} no longer match the hash recorded when "
                    "it was produced."), None
        if receipt.kind in TEXT_KINDS or receipt.kind in ("data", "commit"):
            return None, checkable_text(read.data)
        return None, None


def _reference_problem(resolved: "_Resolved", binding: Binding) -> str | None:
    """Why a value reference cannot stand, or ``None``: its result is flagged, returned no values,
    or holds no single value at the pointer."""
    from auto_research.provenance.refs import RefError, _pointer

    receipt, _, _ = resolved.entry(binding.handle)
    ref = f"{{{{{binding.handle}#{binding.pointer}}}}}"
    if receipt is not None and receipt.flags:
        return (f"{ref} reads {binding.handle}, which is marked {', '.join(receipt.flags)}; a value "
                "from it cannot support a claim. Reference a result without that mark.")
    value, problem = resolved.returned(binding.handle)
    if problem:
        return f"{ref} cannot be read: {problem}."
    try:
        found = _pointer(value, binding.pointer, binding.handle)
    except RefError as exc:
        return f"{ref} cannot be read: {exc}"
    if isinstance(found, (dict, list)):
        inside = ", ".join(map(str, list(found)[:20])) if isinstance(found, dict) else f"0..{len(found) - 1}"
        return (f"{ref} points at a {type(found).__name__}, not one value; point at a value inside "
                f"it ({inside}).")
    return None


def referenced_value(resolved_or_lookup: "_Resolved | ReceiptLookup", binding: Binding) -> object:
    """The value a reference the check accepted names (raises when it cannot be read)."""
    from auto_research.provenance.refs import _pointer

    resolved = resolved_or_lookup if isinstance(resolved_or_lookup, _Resolved) else _Resolved(resolved_or_lookup)
    value, problem = resolved.returned(binding.handle)
    if problem:
        raise ValueError(problem)
    return _pointer(value, binding.pointer or "", binding.handle)


def _copies(
    resolved: "_Resolved", text: str, handles: Iterable[str], *, min_digits: int = COPY_MIN_DIGITS
) -> str:
    """The references for each number in ``text`` of ``min_digits``+ significant digits that one of
    ``handles`` returned, joined; empty when none is a copy."""
    found: list[str] = []
    handles = list(handles)
    for m in _scanned_numbers(text, ()):
        token = m.group()
        number = _as_float(token)
        if number is None or _significant_digits(token) < min_digits or not re.search(r"[.eE]", token):
            continue  # an integer (a count, an ObsID, a channel) is a parameter, as in a tool call
        refs = [f"{{{{{h}#{leaves[number]}}}}}" for h in handles
                if number in (leaves := resolved.leaves(h))]
        if refs:
            found.append(" or ".join(dict.fromkeys(refs[:3])))
    return "; ".join(dict.fromkeys(found))


def _rounded_from(resolved: "_Resolved", token: str, handles: Iterable[str]) -> str:
    """The reference whose returned value ``token`` rounds, when one cited result holds it: a model
    that typed ``2.72`` for 2.7197… is shown the reference that renders it."""
    number = _as_float(token)
    if number is None or ("." not in token and _significant_digits(token) < 3):
        return ""
    decimals = len(token.split(".", 1)[1]) if "." in token and "e" not in token.lower() else 0
    found = [
        f"{{{{{handle}#{pointer}}}}}" for handle in dict.fromkeys(handles)
        for value, pointer in resolved.leaves(handle).items()
        if round(value, decimals) == number and value != number
    ]
    # One candidate is a lead; several mean the digits fit more than one value, so none is named.
    return f"it rounds {found[0]}; write that reference if that is the value meant" if len(found) == 1 else ""


def check_bindings(
    prose: str,
    lookup: ReceiptLookup,
    *,
    own_source: str | None = None,
    scope: Iterable[str] = (),
    exempt_spans: Sequence[tuple[int, int]] = (),
    label: str = "this text",
    support: Iterable[str] = (),
    sources: bool = False,
    _resolved: _Resolved | None = None,
) -> BindingResult:
    """Check one piece of prose; return its problems and its bindings (and, with ``sources``, every
    number's receipts: :func:`number_sources` over exactly the receipts that ground this text).

    ``scope`` adds receipts cited elsewhere that also ground this text's numbers (a report's
    summary is scoped by everything the report cites). A scope receipt that is missing, stale or
    flagged simply grounds nothing: only a receipt the text itself cites can produce a problem,
    because only that one the author can fix. ``exempt_spans`` are ranges that are not prose
    (markup) and are skipped by the number scan.
    """
    resolved = _resolved or _Resolved(lookup)
    result = BindingResult(bindings=parse_bindings(prose, own_source=own_source))
    usable: dict[str, str] = {}  # handle -> normalised text, for receipts that may ground numbers
    cited_here: list[str] = []

    for binding in result.bindings:
        receipt, problem, hay = resolved.entry(binding.handle)
        if receipt is None:
            result.problems.append(f"{label}: {problem}" if not problem.startswith("cites ")
                                   else f"{label} {problem}")
            continue
        if receipt.flags:
            if binding.pointer is not None:
                result.problems.append(f"{label}: {_reference_problem(resolved, binding)}")
            elif binding.quote is not None:
                result.problems.append(
                    f"{label} quotes {binding.handle}, which is marked {', '.join(receipt.flags)}; a "
                    "value from it cannot support a claim. Cite it to describe what happened, and "
                    "take the numbers from a receipt without that mark."
                )
            continue
        if hay is not None:
            usable[binding.handle] = hay
            cited_here.append(binding.handle)
        if binding.pointer is not None:
            problem = _reference_problem(resolved, binding)
            if problem:
                result.problems.append(f"{label}: {problem}")
            continue
        if binding.quote is None:
            continue
        if receipt.kind in ATTRIBUTION_KINDS:
            if not binding.locator:
                result.problems.append(
                    f'{label} attributes "{binding.quote}" to {binding.handle} without saying where '
                    "in it the value is. Add the table, figure, section or page after the quote, "
                    f'e.g. {{{{{binding.handle} "{binding.quote}" Table 2}}}}.'
                )
            continue
        if binding.locator:
            # Only a paper takes a place after its quote. Anything else there was text the check
            # never read: in run faraday-refs-20261026, 46 of 83 numbers sat after a quoted key
            # (``{{h "key": 2.71}}``), unchecked and dropped from the rendered report.
            value, _ = resolved.returned(binding.handle)
            key = binding.quote.strip()
            ref = (_copies(resolved, binding.locator, [binding.handle], min_digits=1)
                   or (f"{{{{{binding.handle}#/{key}}}}}" if isinstance(value, dict) and key in value
                       else f"{{{{{binding.handle}#/<key>}}}}"))
            result.problems.append(
                f'{label}: {{{{{binding.handle} "{binding.quote}"…}}}} has text after its quote '
                f'("{binding.locator}"). Only a paper takes a place there. For a value the tool '
                f"returned, write {ref}; the runtime fills the value in."
            )
            continue
        copied = _copies(resolved, binding.quote, [binding.handle])
        if copied:
            result.problems.append(
                f'{label}: the quote "{binding.quote}" restates values {binding.handle} returned. '
                f"Write each as a reference, which the runtime fills in: {copied}."
            )
            continue
        if hay is None:
            result.problems.append(
                f"{label} quotes {binding.handle}, a {receipt.kind} with no text to quote. Cite it "
                f"as [{binding.handle}] instead."
            )
        elif not bounded_find(hay, normalise(binding.quote, receipt.kind)):
            hint = _quote_hint(resolved, binding.handle, binding.quote, receipt.kind)
            result.problems.append(
                f'{label}: the quote "{binding.quote}" is not in {binding.handle} as written. Copy '
                f"it exactly from that receipt, sign and digits included.{hint}"
            )
    rest = prose
    for binding in reversed(result.bindings):
        rest = rest[: binding.start] + " " * (binding.end - binding.start) + rest[binding.end :]
    if _MARKUP_LEFT.search(rest):
        at = _MARKUP_LEFT.search(rest).start()
        result.problems.append(
            f"{label}: …{_context(prose, at, at + 2)}… has {{{{ }}}} markup the check reads as nothing. "
            'Write a value reference as {{handle#/key}} with the full handle, a quote as {{handle "exact '
            'text"}}, or a paper\'s value as {{lib:p4 "value" Table 2}}.'
        )
    for handle in scope:
        if handle in usable:
            continue
        receipt, _, hay = resolved.entry(handle)
        if receipt is not None and not receipt.flags and hay is not None:
            usable[handle] = hay

    # ``support``: handles backing the text as a whole (a finding's support list); they ground its
    # numbers like scope. A paper in it grounds nothing (it has no text) and exempts nothing: each
    # paper value is attributed explicitly (human ruling 2026-09-24, superseding a per-sentence
    # exemption that let one paper citation carry every number beside it).
    for handle in support:
        receipt, _, hay = resolved.entry(handle)
        if receipt is not None and not receipt.flags and hay is not None:
            usable.setdefault(handle, hay)
    masked = [(b.start, b.end) for b in result.bindings] + list(exempt_spans)
    if sources:
        result.numbers[prose] = number_sources(prose, usable, masked=masked)
    ungrounded = ungrounded_numbers(prose, usable, masked=masked)
    unmet = {m.start() for m in ungrounded}
    # A value the cited request states is the request's, restated where it is cited (2026-09-24).
    asked = [usable[h] for h in cited_here if h.startswith(f"{INPUT_SOURCE}:")]
    typed = [
        f'"{m.group()}" → {refs}' for m in _scanned_numbers(prose, masked)
        if m.start() not in unmet
        and not any(bounded_find(hay, normalise(m.group(), "result")) for hay in asked)
        and (refs := _copies(resolved, m.group(), usable))
    ]
    if typed:
        result.problems.append(
            f"{label} types values a tool returned: {'; '.join(typed[:_MAX_LISTED])}"
            f"{f' and {len(typed) - _MAX_LISTED} more' if len(typed) > _MAX_LISTED else ''}. Write "
            "each as its reference in place of the number; the runtime fills the value in, so "
            "every reader sees the same value and can follow it to its result."
        )
    missing: list[str] = [
        f'"{m.group()}" (…{_context(prose, m.start(), m.end())}…)'
        + (f" — {near}" if (near := _rounded_from(resolved, m.group(), cited_here)) else "")
        for m in ungrounded
    ]
    if missing:
        named = list(dict.fromkeys(cited_here))
        others = len(usable) - len(named)
        where = ", ".join(f"[{h}]" for h in named) or "none cited in this text"
        if others > 0:
            where += f", plus {others} more in scope"
        if not usable:
            where = "no receipt at all"
        shown = "; ".join(missing[:_MAX_LISTED])
        more = f" and {len(missing) - _MAX_LISTED} more" if len(missing) > _MAX_LISTED else ""
        papers = [
            b.handle for b in result.bindings
            if (r := resolved.entry(b.handle)[0]) is not None and r.kind in ATTRIBUTION_KINDS
        ] + [h for h in support if (r := resolved.entry(h)[0]) is not None and r.kind in ATTRIBUTION_KINDS]
        attribute = ""
        if papers:
            first = missing[0].split('"')[1]
            attribute = (
                " If a value comes from a paper, attribute it with where it is in the paper: "
                f'{{{{{papers[0]} "{first}" Table 2}}}}.'
            )
        result.problems.append(
            f"{label} states numbers that appear in none of the receipts it cites ({where}): "
            f"{shown}{more}. Write a value a tool returned as {{{{handle#/key}}}} — the runtime "
            'fills it in — quote a text output exactly as {{handle "…"}}, or compute a derived '
            f"number with a tool and reference that result.{attribute}"
        )
    return result


def check_report_fields(
    fields: Mapping[str, str | Sequence[str] | None],
    lookup: ReceiptLookup,
    *,
    own_source: str | None = None,
    itemised: Iterable[str] = ("findings",),
    scope: Iterable[str] = (),
    sources: bool = False,
) -> BindingResult:
    """Check a report's prose fields together.

    Each item of an ``itemised`` field (a finding) is grounded only by the receipts it cites
    itself. Every other field is grounded by everything the report cites anywhere, plus ``scope``
    (a role that may ground a number in any receipt it can read passes all of them here).
    """
    itemised = set(itemised)
    everything: list[str] = []
    for value in fields.values():
        for text in ([value] if isinstance(value, str) else (value or [])):
            everything.extend(b.handle for b in parse_bindings(text, own_source=own_source))
    # A task's input grounds only the text that cites it (human ruling 2026-09-24), never the rest of
    # the report: citing [input:h1] in one field must not let another field repeat the request's
    # numbers unmarked. Found live by the reviewer session (an ObsID in a title).
    everything = [
        h for h in dict.fromkeys([*everything, *scope]) if not h.startswith(f"{INPUT_SOURCE}:")
    ]
    resolved = _Resolved(lookup)
    merged = BindingResult()
    for name, value in fields.items():
        if not value:
            continue
        if isinstance(value, str):
            items = [(name, value, () if name in itemised else everything)]
        else:
            items = [
                (f"{name[:-1] if name.endswith('s') else name} {i}", text,
                 () if name in itemised else everything)
                for i, text in enumerate(value, 1)
            ]
        for label, text, grounding in items:
            part = check_bindings(
                text, lookup, own_source=own_source, scope=grounding, label=label,
                sources=sources, _resolved=resolved,
            )
            merged.problems.extend(part.problems)
            merged.bindings.extend(part.bindings)
            merged.numbers.update(part.numbers)
    return merged


def render_bindings(
    prose: str,
    *,
    own_source: str | None = None,
    quote: Callable[[str], str] = lambda q: q,
    cite: Callable[[str], str] = lambda h: f"[{h}]",
    value: Callable[[Binding], str] | None = None,
) -> str:
    """Replace each binding with its rendered form: a quote by ``quote(text)``, a cite by
    ``cite(handle)``, a value reference by ``value(binding)`` (left as written without one)."""
    out = prose
    for binding in reversed(parse_bindings(prose, own_source=own_source)):
        if binding.pointer is not None:
            if value is None:
                continue
            text = value(binding)
        else:
            text = quote(binding.quote) if binding.quote is not None else cite(binding.handle)
        out = out[: binding.start] + text + out[binding.end :]
    return out


def render_values(prose: str, lookup: ReceiptLookup, *, own_source: str | None = None) -> str:
    """The text a model reads: each value reference followed by its value, ``{{h#/k}} (= 2.72)``,
    so it can reason with the number and cite it onward by the reference. A reference that cannot
    be read stays as written."""

    def show(binding: Binding) -> str:
        written = prose[binding.start:binding.end]
        try:
            return f"{written} (= {display_value(referenced_value(resolved, binding))})"
        except Exception:  # noqa: BLE001 — the writer's check already named it
            return written

    resolved = _Resolved(lookup)
    out = prose
    for binding in reversed(parse_bindings(prose, own_source=own_source)):
        if binding.pointer is not None:  # a quote or a cite stays as written, handle and place
            out = out[: binding.start] + show(binding) + out[binding.end :]
    return out


def paper_attributions(
    prose: str, lookup: ReceiptLookup, *, own_source: str | None = None
) -> list[PaperAttribution]:
    """Every value the prose attributes to a paper, for the report's verify-against-the-PDF list.

    Only attributions that resolve to a visible paper receipt and carry a locator are returned —
    the ones the check accepted. An attributed value is **not** verified by anything here; this list
    is what makes checking it cheap for a human.
    """
    out: list[PaperAttribution] = []
    for binding in parse_bindings(prose, own_source=own_source):
        if binding.quote is None or not binding.locator:
            continue
        receipt = lookup.get(binding.handle)
        if receipt is not None and receipt.kind in ATTRIBUTION_KINDS:
            out.append(PaperAttribution(
                handle=binding.handle, value=binding.quote, locator=binding.locator
            ))
    return out


#: Former name, kept so an import written against it still resolves.
Attribution = PaperAttribution
