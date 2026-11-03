"""Receipt references in a toolkit call's arguments: one tool's output passed on by handle.

A worker writes ``{"$ref": "<handle>#<json-pointer>"}`` wherever an argument value goes; the runtime
replaces it, before the call runs, with the value at that pointer in the JSON the receipt holds, and
records the receipt as a parent of everything the call mints. The tool receives a plain value — the
same Python object the producing tool returned, every digit of it — and never sees the reference.

A reference resolves only the caller's own toolkit results (user ruling 2026-11-03): any unit may
open any receipt, but a value from a peer's result enters a call once the caller's own tool has
produced it, so a tool echoing it back cannot make it the caller's evidence. A receipt of
something a role read is a place to look and is refused, and so is a failed result or one whose
bytes changed. Every refusal is a sentence the model reads and acts on. Standard library only.
"""

from __future__ import annotations

import json
import re
import weakref
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from auto_research.provenance.store import (
    INPUT_SOURCE,
    NOT_EVIDENCE_NAMES,
    ReceiptStore,
    is_evidence,
)
from auto_research.provenance.bindings import bounded_find
from auto_research.schemas.artifacts import HANDLE_PATTERN

#: A receipt handle written anywhere in a text, bracketed or bare.
_HANDLE_IN_TEXT = re.compile(rf"(?<![\w:\-])({HANDLE_PATTERN})(?![\w])")
#: A handle cited in brackets.
_HANDLE_BRACKETED = re.compile(rf"\[({HANDLE_PATTERN})\]")

#: The one key of a reference object.
REF_KEY = "$ref"

#: Where ``sandbox.runner.normalize_tool_raw`` folds a tool's console output under its JSON return
#: (the same text; a test drives that function to keep them equal).
CONSOLE_SEPARATOR = "\n--- console ---\n"

#: A list index in a pointer: RFC 6901's decimal form, bounded (``"²".isdigit()`` is true).
_INDEX = re.compile(r"0|[1-9][0-9]{0,8}")

#: A typed float this long (significant digits) that a usable receipt holds is a copy (RetypeCheck).
_LINT_MIN_DIGITS = 4
#: A typed float this long that rounds one of the caller's own results is a copy too, and the
#: shortest a peer's value must be to count as one (a shorter value is a constant or a choice).
_ROUNDED_MIN_DIGITS = 6


class RefError(ValueError):
    """A reference that cannot be resolved; the message is what the model reads."""


def is_ref(value: Any) -> bool:
    """Whether ``value`` is a reference: exactly ``{"$ref": "<text>"}``, or that object written as a
    JSON string (a model fills a number-typed argument with the reference's text; measured live)."""
    return _ref_text(value) is not None


def _ref_text(value: Any) -> str | None:
    if isinstance(value, str) and value.lstrip().startswith("{") and REF_KEY in value:
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if isinstance(value, dict) and len(value) == 1 and isinstance(value.get(REF_KEY), str):
        return value[REF_KEY]
    return None


def split_ref(text: str) -> tuple[str, str]:
    """``"h#/a/0"`` → ``("h", "/a/0")``; no ``#`` names the whole returned value (pointer ``""``)."""
    handle, _, pointer = text.strip().partition("#")
    return handle.strip(), pointer.strip()


def refs_in(args: Any) -> list[str]:
    """Every reference string in an argument tree, in order."""
    found: list[str] = []

    def walk(value: Any) -> None:
        if is_ref(value):
            found.append(_ref_text(value))
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(args)
    return found


def passable(store: ReceiptStore, handle: str) -> bool:
    """Whether a receipt is one a reference may name: a toolkit tool's result."""
    receipt = store.get(handle)
    return (receipt is not None and receipt.kind == "result" and receipt.source != INPUT_SOURCE
            and is_evidence(receipt))


def handles_named(text: str) -> list[str]:
    """Every handle-shaped string a text names (bracketed or bare), in order, once each."""
    return list(dict.fromkeys(_HANDLE_IN_TEXT.findall(text or "")))


def unviewable_handles(text: str, viewable: Callable[[str], bool]) -> list[str]:
    """The bracketed handles a brief names that are not receipts of this run — mistyped.

    Checked at the author's ``turn_end`` (the lead's ``what_to_probe``, the reviewer's
    ``follow_up``), so a brief hands on only handles the next team can open.
    Bracketed only (``[analyst_worker_0:r12]``, as the prompts write them): prose such as
    ``powerlaw:p1`` has the shape of a handle and is no claim.
    """
    return [h for h in dict.fromkeys(_HANDLE_BRACKETED.findall(text or "")) if not viewable(h)]


def brief_handle_problems(field: str, text: str, viewable: Callable[[str], bool]) -> list[str]:
    """A brief names results by handle for the next team to open (the unit that made one passes it
    on by reference; another reproduces it); each must be a receipt of this run. A ``turn_end``
    refusal, or nothing."""
    unknown = unviewable_handles(text, viewable)
    if not unknown:
        return []
    return [
        f"{field} names {', '.join(unknown)}, which {'is not a receipt' if len(unknown) == 1 else 'are not receipts'} "
        "of this run. Copy each handle exactly as a tool result or report shows it."
    ]


def _changed(handle: str) -> RefError:
    return RefError(
        f"{handle}: its stored output no longer matches its receipt, so its values cannot be "
        "passed on; run the producing tool again and pass that result."
    )


def returned_value(store: ReceiptStore, handle: str) -> Any:
    """The value a toolkit result receipt holds: the keys its tool returned (``returned_sha256``),
    else the JSON of its text with the console tail set aside (a tool that printed nothing else).

    Raises :class:`RefError` when the stored bytes changed since the receipt or hold no JSON.
    """
    from auto_research.provenance.store import read_receipted

    receipt = store.get(handle)
    if receipt is not None and receipt.returned_sha256:
        read = read_receipted(store.blob_path(receipt.returned_sha256), receipt.returned_sha256,
                              label=f"{handle} (returned value)")
        if read.data is None or read.changed:
            raise _changed(handle)
        return json.loads(read.data.decode("utf-8"))
    read = store.read(handle)
    if read is None or read.data is None or read.changed:
        raise _changed(handle)
    text = read.data.decode("utf-8", errors="replace")
    for candidate in (text, text.split(CONSOLE_SEPARATOR, 1)[0]):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise RefError(
        f"{handle} holds text, not a returned value, so a pointer cannot reach into it; pass the "
        "values a tool returned as a dict, or ask for a tool that returns them."
    )


def _pointer(doc: Any, pointer: str, handle: str) -> Any:
    """RFC 6901: ``""`` is the whole value, ``/a/0`` the key ``a`` then item 0."""
    if pointer in ("", "/") and not (isinstance(doc, dict) and "" in doc):
        return doc  # "#/" is how a model writes "the whole value"; no tool returns a "" key
    if not pointer.startswith("/"):
        raise RefError(f"{handle}#{pointer}: a pointer starts with '/', e.g. {handle}#/bins/0.")
    value, path = doc, ""
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and token in value:
            value, path = value[token], f"{path}/{raw}"
            continue
        if isinstance(value, list) and _INDEX.fullmatch(token) and int(token) < len(value):
            value, path = value[int(token)], f"{path}/{raw}"
            continue
        if isinstance(value, dict):
            there = ", ".join(sorted(map(str, value))[:40]) or "none"
            raise RefError(f"{handle}#{pointer}: no key '{token}' at '{path or '/'}'; keys there: {there}.")
        if isinstance(value, list):
            raise RefError(f"{handle}#{pointer}: '{token}' at '{path or '/'}' is not an index below {len(value)}.")
        raise RefError(f"{handle}#{pointer}: '{path or '/'}' is a single value; the pointer ends there.")
    return value


def resolve_one(store: ReceiptStore, text: str, usable: Callable[[str], bool]) -> tuple[Any, str]:
    """``(value, handle)`` for one reference string, or :class:`RefError` saying why not."""
    handle, pointer = split_ref(text)
    receipt = store.get(handle) if handle else None
    if receipt is None:
        raise RefError(
            f"{handle or '(no handle)'} is not a receipt of this run: copy the handle exactly as a "
            "tool result shows it, as {\"$ref\": \"<handle>#/<key>\"}."
        )
    if receipt.source == INPUT_SOURCE:
        what = "the request" if receipt.produced_by == "research_request" else NOT_EVIDENCE_NAMES.get(
            receipt.produced_by, "a task text")
        raise RefError(f"{handle} is {what}: type a value it states as a parameter.")
    if passable(store, handle) and not usable(handle):
        raise RefError(
            f"{handle} is {receipt.source}'s result. A call passes on its own agent's results: "
            "reproduce the value with a tool run of yours, then pass your result by reference."
        )
    if not passable(store, handle):
        what = NOT_EVIDENCE_NAMES.get(receipt.produced_by, f"a {receipt.kind} receipt")
        raise RefError(
            f"{handle} is {what}, a place to look: pass the result of a toolkit tool, or run the "
            "tool that produces these values and pass its result."
        )
    if "failed" in receipt.flags:
        raise RefError(f"{handle} is flagged failed (its call, or a result it came from, failed); pass a "
                       "result from a chain that succeeded.")
    return _pointer(returned_value(store, handle), pointer, handle), handle


def resolve_refs(
    args: Mapping[str, Any], store: ReceiptStore, usable: Callable[[str], bool]
) -> tuple[dict[str, Any], list[str]]:
    """The arguments with every reference replaced by its value, and the receipts they came from.

    Raises :class:`RefError` on the first reference that does not resolve; nothing runs then.
    """
    parents: list[str] = []

    def walk(value: Any) -> Any:
        if is_ref(value):
            resolved, handle = resolve_one(store, _ref_text(value), usable)
            parents.append(handle)
            return resolved
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    resolved = {key: walk(value) for key, value in args.items()}
    return resolved, list(dict.fromkeys(parents))


def _floats(value: Any, path: str = "") -> Iterable[tuple[str, str]]:
    """Every measured-looking number in a value tree (floats) with its JSON pointer, as
    ``(shortest round-trip text, pointer)``. Integers (counts, ObsIDs, grid sizes) are parameters
    a model types, so they are left out."""
    if isinstance(value, bool):
        return
    if isinstance(value, float):
        yield repr(value), path
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _floats(item, f"{path}/{str(key).replace('~', '~0').replace('/', '~1')}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _floats(item, f"{path}/{index}")


def _significant(text: str) -> int:
    mantissa = text.lstrip("-").split("e")[0].split("E")[0].replace(".", "")
    return len(mantissa.strip("0"))


#: A returned value larger than this is not scanned for copies (memory; such blocks are arrays a
#: model passes by reference anyway).
_MAX_SCANNED_BYTES = 2_000_000


def _size(path: Any) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


_HELD: "weakref.WeakKeyDictionary[ReceiptStore, dict[str, dict[str, str]]]" = weakref.WeakKeyDictionary()


class RetypeCheck:
    """Refuses a typed number that a usable toolkit result already holds (user ruling 2026-10-26).

    A float of 4+ significant digits typed into a toolkit call, equal to a value a result the
    caller may pass on returned, is a copy: the call is refused before it runs and told the
    reference to write. Measured live: a note after the call was ignored, and the copied value
    left its call without a parent. Parameters a model chooses (``2.0``, ``0.99``, ``4.3e6``) and
    integers pass. Never an edge by itself: a value can sit in many receipts, so the refusal names
    where to point and the model's reference makes the link. Each receipt read once per store.
    """

    def __init__(self, store: ReceiptStore) -> None:
        self.store = store
        self._held = _HELD.setdefault(store, {})
        self._texts: dict[str, str] = {}
        self._numbers: dict[str, list[tuple[float, str]]] = {}
        self._given: dict[str, set[str]] = {}

    def _floats_of(self, handle: str) -> dict[str, str]:
        """``{float text: pointer}`` for the 4+-digit floats a result returned that its own call
        was not given: an echoed argument is that caller's parameter, never a value to copy."""
        if handle not in self._held:
            held: dict[str, str] = {}
            receipt = self.store.get(handle)
            too_big = receipt is not None and receipt.returned_sha256 is not None and _size(
                self.store.blob_path(receipt.returned_sha256)) > _MAX_SCANNED_BYTES
            if not too_big:
                try:
                    given = self._chosen_by(handle)
                    for text, path in _floats(returned_value(self.store, handle)):
                        if _significant(text) >= _LINT_MIN_DIGITS and text not in given:
                            held.setdefault(text, path)
                except (RefError, ValueError):
                    held = {}
            self._held[handle] = held
        return self._held[handle]

    def _args_of(self, handle: str) -> Any:
        """The arguments the receipt's call was given, from their blob (``args_sha256``)."""
        from auto_research.provenance.store import read_receipted

        receipt = self.store.get(handle)
        if receipt is None or not receipt.args_sha256 or not self.store.blob_path(receipt.args_sha256).is_file():
            return {}  # a receipt minted before arguments were stored
        read = read_receipted(self.store.blob_path(receipt.args_sha256), receipt.args_sha256,
                              label=f"{handle} (arguments)")
        if read.data is None or read.changed:
            return {}
        return json.loads(read.data.decode("utf-8"))

    def _numbers_of(self, handle: str) -> list[tuple[float, str]]:
        """``(value, pointer)`` for the held floats of :meth:`_floats_of`, for the rounded and
        sign-flipped comparisons."""
        if handle not in self._numbers:
            self._numbers[handle] = [(float(text), pointer) for text, pointer in self._floats_of(handle).items()]
        return self._numbers[handle]

    def _chosen_by(self, handle: str) -> set[str]:
        """The floats a result's own call was given, read as the tool received them (cached)."""
        if handle not in self._given:
            self._given[handle] = {text for text, _ in _floats(_as_received(self._args_of(handle)))}
        return self._given[handle]

    def _read_text(self, handle: str) -> str:
        """The stored text of a receipt of something the caller read or ran (cached; empty when
        its bytes changed or are not text)."""
        if handle not in self._texts:
            read = self.store.read(handle)
            text = ""
            if read is not None and read.data is not None and not read.changed and len(read.data) <= _MAX_SCANNED_BYTES:
                text = read.data.decode("utf-8", errors="replace")
            self._texts[handle] = text
        return self._texts[handle]

    def problem(self, typed_args: Mapping[str, Any], handles: Iterable[str]) -> str | None:
        """The refusal for a call whose typed floats are copies, or ``None``.

        A copy is a 4+-digit float that one of the caller's own results (``handles``) holds
        exactly, with its sign flipped, or rounded to the digits typed (6+) (run
        faraday-refs-20261026: ``|ci_lo|`` typed as ``0.37959418926306543`` passed the exact
        check); a 6+-digit float a peer's result holds exactly or sign-flipped, unless the caller
        gave it to an earlier call or a task text states it (answered with "reproduce it"; shorter
        values are constants and chosen parameters, and a rounded match run-wide would collide with
        them); or a value found in the text of any file read or shell output of the run (30 values
        typed from a CSV a worker had read reached a fit without the CSV as an input).
        """
        # The arguments as the tool will receive them: the REPL parses a string holding a JSON
        # container, and a number written as a string is a number to a tool that converts it.
        typed = [(text, path) for text, path in _floats(_as_received(dict(typed_args)))
                 if _significant(text) >= _LINT_MIN_DIGITS]
        if not typed:
            return None
        # Exactly the results a reference could resolve, so a refusal never names a dead end.
        usable = [h for h in handles if passable(self.store, h)
                  and "failed" not in self.store.get(h).flags]
        usable.sort(key=lambda h: self.store.get(h).created_at, reverse=True)  # newest first
        mine = set(usable)
        # Any seat may open any receipt (view is global), so a value seen in a peer's read or shell
        # output is a copy as much as one from the caller's own.
        # A task text (the request, a brief) states parameters; a file the human sent is material
        # read, like any file (the human gives direction, never materials, ruling 2026-09-25).
        read = [r.handle for r in self.store.receipts() if r.produced_by in NOT_EVIDENCE_NAMES
                and (r.source != INPUT_SOURCE or r.produced_by == "human_reply")]
        # A peer's result holding the value: typing it would make the peer's value this caller's.
        peers = [r.handle for r in self.store.receipts(kind="result")
                 if r.handle not in mine and passable(self.store, r.handle) and "failed" not in r.flags]
        # A value the caller chose before (an argument of its own earlier calls) or a task text
        # states (the request, a brief prescribing a starting value) is a parameter, whatever a
        # peer's result also returned.
        chosen = set().union(*(self._chosen_by(h) for h in usable)) if usable else set()
        asked = [self._read_text(r.handle) for r in self.store.receipts(source=INPUT_SOURCE)
                 if r.produced_by != "human_reply"]
        lines: list[str] = []
        for text, arg_path in typed:
            name = arg_path.lstrip("/") or "(whole)"
            value, half = float(text), _last_place(text) / 2
            exact = [(h, self._floats_of(h)[text]) for h in usable if text in self._floats_of(h)]
            if exact:
                refs = " or ".join(f'{{"$ref": "{h}#{pointer}"}}' for h, pointer in exact[:3])
                lines.append(f"argument {name} = {text}: pass {refs}")
                continue
            # Rounding counts only for a long typed value: a short one collides with chosen parameters.
            rounded_ok = _significant(text) >= _ROUNDED_MIN_DIGITS
            near = [(h, p, "with its sign flipped" if v == -value else "rounded")
                    for h in usable for v, p in self._numbers_of(h)
                    if v == -value or (rounded_ok and v != value and abs(v - value) <= half)]
            if near:
                h, pointer, how = near[0]
                lines.append(
                    f'argument {name} = {text} is {{"$ref": "{h}#{pointer}"}} {how}: pass the '
                    "result by reference and let the tool take what it needs from it (its sign, its "
                    "precision); when no argument does that, ask for a tool that does in "
                    "tool_request"
                )
                continue
            if (rounded_ok and text not in chosen
                    and not any(bounded_find(hay, text) for hay in asked)):
                flipped = repr(-value)
                peer = next(((h, held[text], "") if text in held else (h, held[flipped], " with its sign flipped")
                             for h in peers if text in (held := self._floats_of(h)) or flipped in held), None)
                if peer is not None:
                    h, pointer, how = peer
                    lines.append(
                        f"argument {name} = {text} is {self.store.get(h).source}'s result "
                        f"({h}#{pointer}){how}: reproduce it with a tool run of yours, then pass "
                        "your result by reference"
                    )
                    continue
            seen = next((h for h in read if bounded_find(self._read_text(h), text)), None)
            if seen is not None:
                what = NOT_EVIDENCE_NAMES[self.store.get(seen).produced_by]
                lines.append(
                    f"argument {name} = {text} is copied from {seen}, {what}: pass the file itself "
                    "to an argument that reads it, or ask for a tool that reads it in tool_request"
                )
        if not lines:
            return None
        shown = lines[:8] + ([f"... and {len(lines) - 8} more"] if len(lines) > 8 else [])
        return (
            "This call was not run: it types values that a tool result or a file already holds. "
            "Each needs to arrive from where it was produced, so the call records where it came "
            "from:\n- " + "\n- ".join(shown)
        )


_NUMBER = re.compile(r"[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?")


def _as_received(value: Any) -> Any:
    """An argument tree as a tool receives it: a string holding a JSON container parsed (the REPL
    does, ``sandbox.persistent``), a string holding one decimal number read as that float."""
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in ("{", "["):
            try:
                return _as_received(json.loads(text))
            except ValueError:
                return value
        if _NUMBER.fullmatch(text) and any(c in text for c in ".eE"):
            return float(text)
        return value
    if isinstance(value, dict):
        return {key: _as_received(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_as_received(item) for item in value]
    return value


def _last_place(text: str) -> float:
    """The value of one unit in the last digit typed: ``0.3796`` → 1e-4, ``2.5e6`` → 1e5."""
    mantissa, _, exponent = text.lower().partition("e")
    decimals = len(mantissa.split(".", 1)[1]) if "." in mantissa else 0
    return 10.0 ** ((int(exponent) if exponent else 0) - decimals)
