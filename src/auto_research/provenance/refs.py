"""Receipt references in a toolkit call's arguments: one tool's output passed on by handle.

A worker writes ``{"$ref": "<handle>#<json-pointer>"}`` wherever an argument value goes; the runtime
replaces it, before the call runs, with the value at that pointer in the JSON the receipt holds, and
records the receipt as a parent of everything the call mints. The tool receives a plain value — the
same Python object the producing tool returned, every digit of it — and never sees the reference.

A reference resolves the caller's own toolkit results and the files its own tools wrote (as their
path, while the file still hashes to its receipt). A brief is an informal text, like a message,
and is never a source (user ruling 2026-11-03). Any unit may open any receipt (user ruling 2026-11-03), but a
value from a peer's result enters a call once the caller's own tool has produced it, so a tool
echoing it back cannot make it the caller's evidence. A receipt of something a role read is a
place to look and is refused, and so is a failed result or one whose bytes changed. Every refusal
is a sentence the model reads and acts on. Which arguments must be references is the input
validator's rule (``provenance.inputs``). Standard library only.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
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

#: A handle in brackets, the retired citation form, still read as a name in a brief.
_HANDLE_BRACKETED = re.compile(rf"\[({HANDLE_PATTERN})\]")

#: The one key of a reference object.
REF_KEY = "$ref"
#: The two keys of a typed parameter (``provenance.inputs``).
VALUE_KEY, REASON_KEY = "value", "reason"
#: A pointer starting here reaches into the arguments a toolkit result's call ran with, not into
#: what it returned: ``<handle>#/$args/<argument>/…`` (user ruling 2026-11-03, one handle per number).
ARGS_POINTER = "/$args"

#: Where ``sandbox.runner.normalize_tool_raw`` folds a tool's console output under its JSON return
#: (the same text; a test drives that function to keep them equal).
CONSOLE_SEPARATOR = "\n--- console ---\n"

#: A list index in a pointer: RFC 6901's decimal form, bounded (``"²".isdigit()`` is true).
_INDEX = re.compile(r"0|[1-9][0-9]{0,8}")



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


def _as_object(value: Any) -> Any:
    """A JSON object written as a string, parsed (a model fills an object argument with its text;
    the REPL would parse it the same way), else ``value``."""
    if isinstance(value, str) and value.lstrip().startswith("{"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _has_ref(value: Any) -> bool:
    if is_ref(value):
        return True
    if isinstance(value, dict):
        return any(_has_ref(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_ref(item) for item in value)
    return False


def typed_parameter(value: Any) -> tuple[Any, str] | None:
    """``(value, reason)`` for a typed parameter, ``{"value": …, "reason": "<non-empty>"}`` holding
    no reference, else ``None``."""
    value = _as_object(value)
    if (isinstance(value, dict) and set(value) == {VALUE_KEY, REASON_KEY}
            and isinstance(value[REASON_KEY], str) and value[REASON_KEY].strip()
            and not _has_ref(value[VALUE_KEY])):
        return value[VALUE_KEY], value[REASON_KEY].strip()
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
    """The handles a brief names that are not receipts of this run — mistyped.

    Checked at the author's ``turn_end`` (the lead's ``what_to_probe``, the reviewer's
    ``follow_up``), so a brief hands on only handles the next team can open. A brief names a
    result as a citation does, ``{{analyst_worker_0:r12}}`` (one citing syntax, 2026-11-03); prose
    such as ``powerlaw:p1`` has the shape of a handle and is no claim.
    """
    from auto_research.provenance.bindings import parse_bindings

    named = [b.handle for b in parse_bindings(text or "") if not b.handle.startswith("self:")]
    named += _HANDLE_BRACKETED.findall(text or "")  # the retired [h] still names a handle
    return [h for h in dict.fromkeys(named) if not viewable(h)]


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
        raise RefError(
            f'{handle} is {what}: type a value it states as {{"value": …, "reason": "<where it is stated>"}}.'
        )
    if receipt.kind == "data" and is_evidence(receipt):
        if not usable(handle):
            raise RefError(
                f"{handle} is a file {receipt.source}'s tool wrote. A call passes on its own agent's "
                "files: reproduce it with a tool run of yours, then pass your file by reference."
            )
        if pointer not in ("", "/"):
            raise RefError(f'{handle} is a file; it passes as its path, {{"$ref": "{handle}"}}.')
        if "failed" in receipt.flags:
            raise RefError(f"{handle} is flagged failed; pass a file from a chain that succeeded.")
        try:
            unchanged = _file_digest(receipt.path) == receipt.sha256
        except OSError:
            unchanged = False
        if not unchanged:
            raise RefError(
                f"{handle}: the file at its path no longer matches its receipt (another call wrote "
                "it since); run the tool that writes it again and pass that file."
            )
        return str(receipt.path), handle
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
    return point(store, handle, pointer), handle


#: ``(path, size, mtime_ns) -> sha256`` of files a reference passed: a large reduction product is
#: read once while it stays as it was, not once per reference (review 2026-11-03).
_DIGESTS: dict[tuple[str, int, int], str] = {}


def _file_digest(path: Any) -> str:
    from pathlib import Path

    from auto_research.provenance.store import sha256_file

    stat = Path(path).stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _DIGESTS:
        _DIGESTS[key] = sha256_file(Path(path))
    return _DIGESTS[key]


def point(store: ReceiptStore, handle: str, pointer: str) -> Any:
    """The value at ``pointer`` in a toolkit result: in what it returned, or, under
    :data:`ARGS_POINTER`, in the arguments its call ran with (:func:`argument_value`)."""
    if pointer == ARGS_POINTER or pointer.startswith(ARGS_POINTER + "/"):
        return argument_value(store, handle, pointer)
    return _pointer(returned_value(store, handle), pointer, handle)


def argument_value(store: ReceiptStore, handle: str, pointer: str) -> Any:
    """The value at ``/$args/<argument>/…``: a typed parameter's value without its reason, or a
    reference as the value it named. Any other argument (a setting, a path, a default) is
    refused: it carries no reason and no source."""
    args = call_arguments(store, handle)
    names = ", ".join(sorted(map(str, args))[:40]) if isinstance(args, dict) and args else "none recorded"
    rest = pointer[len(ARGS_POINTER):]
    if not rest.strip("/"):
        raise RefError(f"{handle}#{pointer}: name an argument, {handle}#{ARGS_POINTER}/<argument>; "
                       f"its arguments: {names}.")
    raw, _, deeper = rest[1:].partition("/")
    name = raw.replace("~1", "/").replace("~0", "~")
    if not isinstance(args, dict) or name not in args:
        raise RefError(f"{handle}#{pointer}: its call took no argument '{name}'; its arguments: {names}.")
    value = args[name]
    typed = typed_parameter(value)
    if typed is not None:
        value = typed[0]
    elif refs_in(value):
        value = resolve_refs({name: value}, store, lambda _h: True)[0][name]
    else:
        # A setting, a path, a default the runtime passed: typed with no reason and no source, so
        # never a value to pass on or to state in a report (review 2026-11-03).
        raise RefError(
            f"{handle}#{pointer}: argument '{name}' was typed with no reason and no reference, so it "
            "carries no record of where its value came from; pass a result's value, or a value with "
            "its reason."
        )
    return _pointer(value, "/" + deeper if deeper else "", handle)


def argument_source(store: ReceiptStore, handle: str, pointer: str, *, _depth: int = 0) -> str:
    """Where the argument value at ``/$args/<name>/…`` came from, in words: its reason when it was
    typed, else the reference that carried it (followed through an earlier call's ``$args`` to
    the reason at its end), else that it is an argument of the call."""
    args = call_arguments(store, handle)
    tokens = [t.replace("~1", "/").replace("~0", "~") for t in pointer[len(ARGS_POINTER) + 1:].split("/")]
    value: Any = args if isinstance(args, dict) else {}
    for token in tokens:
        typed = typed_parameter(value)
        if typed is not None:
            return f"chosen by {handle.split(':', 1)[0]}: {typed[1]}"
        text = _ref_text(value)
        if text is not None:
            break
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and _INDEX.fullmatch(token) and int(token) < len(value):
            value = value[int(token)]
        else:
            return "an argument of the call"
    typed = typed_parameter(value)
    if typed is not None:
        return f"chosen by {handle.split(':', 1)[0]}: {typed[1]}"
    text = _ref_text(value)
    if text is None:
        return "an argument of the call"
    named, inner = split_ref(text)
    if (inner == ARGS_POINTER or inner.startswith(ARGS_POINTER + "/")) and _depth < 8:
        return f"passed from {text}, " + argument_source(store, named, inner, _depth=_depth + 1)
    return f"passed from {text}"


def received_arguments(store: ReceiptStore, handle: str) -> dict:
    """The arguments a call ran with, as its tool received them: each typed parameter's value, each
    reference as the value it named (a reference that no longer resolves stays as written)."""
    args = call_arguments(store, handle)
    if not isinstance(args, dict):
        return {}
    out: dict = {}
    for name, value in args.items():
        typed = typed_parameter(value)
        if typed is not None:
            out[name] = typed[0]
        elif refs_in(value):
            try:
                out[name] = resolve_refs({name: value}, store, lambda _h: True)[0][name]
            except (RefError, OSError, ValueError):
                out[name] = value
        else:
            out[name] = value
    return _as_received(out)


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


def call_arguments(store: ReceiptStore, handle: str) -> Any:
    """The arguments a receipt's call ran with, from their blob (``args_sha256``): the model's
    arguments completed by the runtime, each typed parameter with its reason. ``{}`` for a receipt
    minted before arguments were stored, or whose blob changed."""
    from auto_research.provenance.store import read_receipted

    receipt = store.get(handle)
    if receipt is None or not receipt.args_sha256 or not store.blob_path(receipt.args_sha256).is_file():
        return {}
    read = read_receipted(store.blob_path(receipt.args_sha256), receipt.args_sha256,
                          label=f"{handle} (arguments)")
    if read.data is None or read.changed:
        return {}
    return json.loads(read.data.decode("utf-8"))


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
