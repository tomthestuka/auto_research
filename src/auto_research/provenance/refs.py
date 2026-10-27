"""Receipt references in a toolkit call's arguments: one tool's output passed on by handle.

A worker writes ``{"$ref": "<handle>#<json-pointer>"}`` wherever an argument value goes; the runtime
replaces it, before the call runs, with the value at that pointer in the JSON the receipt holds, and
records the receipt as a parent of everything the call mints. The tool receives a plain value — the
same Python object the producing tool returned, every digit of it — and never sees the reference.

A reference resolves only a toolkit result the caller may use (its own receipts, the request, the
receipts cited in reports it received, and handles named in a message or brief to it); a receipt of
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


def handles_in_brief(store: ReceiptStore, text: str) -> list[str]:
    """The toolkit results a brief's text names (bracketed or bare), in order."""
    return [h for h in dict.fromkeys(_HANDLE_IN_TEXT.findall(text or "")) if passable(store, h)]


def unviewable_handles(text: str, viewable: Callable[[str], bool]) -> list[str]:
    """The bracketed handles a brief names that its author cannot view — mistyped or borrowed.

    Checked at the author's ``turn_end`` (the lead's ``what_to_probe``, the reviewer's
    ``follow_up``), so a brief hands on only results its author could open, as a message does.
    Bracketed only (``[analyst_worker_0:r12]``, as the prompts write them): prose such as
    ``powerlaw:p1`` has the shape of a handle and is no claim.
    """
    return [h for h in dict.fromkeys(_HANDLE_BRACKETED.findall(text or "")) if not viewable(h)]


def brief_handle_problems(field: str, text: str, viewable: Callable[[str], bool]) -> list[str]:
    """A brief names results by handle for the next team to open and pass on; each must be a
    result its author can open itself, as a message's must. A ``turn_end`` refusal, or nothing."""
    unknown = unviewable_handles(text, viewable)
    if not unknown:
        return []
    return [
        f"{field} names {', '.join(unknown)}, which you cannot open. Name the results you can open "
        "(your own, and those the reports you received cite), copying each handle exactly."
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
    if receipt is None or not usable(handle):
        raise RefError(
            f"{handle or '(no handle)'} is not a receipt you can pass on: pass your own results, "
            "the ones your reports and messages name, as {\"$ref\": \"<handle>#/<key>\"}."
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
                    given = {text for text, _ in _floats(self._args_of(handle))}
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

    def problem(self, typed_args: Mapping[str, Any], handles: Iterable[str]) -> str | None:
        """The refusal for a call whose typed floats a usable result holds, or ``None``."""
        typed = [(text, path) for text, path in _floats(dict(typed_args))
                 if _significant(text) >= _LINT_MIN_DIGITS]
        if not typed:
            return None
        # Exactly the results a reference could resolve, so a refusal never names a dead end.
        usable = [h for h in handles if passable(self.store, h)
                  and "failed" not in self.store.get(h).flags]
        lines: list[str] = []
        for text, arg_path in typed:
            holders = [(h, self._floats_of(h)[text]) for h in usable if text in self._floats_of(h)]
            if not holders:
                continue
            # The newest result first: the one a chain most likely continues from.
            holders.sort(key=lambda item: self.store.get(item[0]).created_at, reverse=True)
            refs = " or ".join(f'{{"$ref": "{h}#{pointer}"}}' for h, pointer in holders[:3])
            lines.append(f"argument {arg_path.lstrip('/') or '(whole)'} = {text}: pass {refs}")
        if not lines:
            return None
        shown = lines[:8] + ([f"... and {len(lines) - 8} more"] if len(lines) > 8 else [])
        return (
            "This call was not run: it types values that a tool result already holds. Pass each "
            "one by reference, so it arrives exactly as produced and the call records where it "
            "came from:\n- " + "\n- ".join(shown)
        )
