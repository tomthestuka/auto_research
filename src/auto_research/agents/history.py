"""Session history: each agent's conversation, recorded in the run ledger and rebuilt from it (B1).

The run ledger (``monitor.jsonl``) already held every tool call's arguments and every result's
text. What it lacked for a lossless rebuild was the model's own turn (its prose, thinking and tool
calls as the provider returned them), the turn's opening messages, and the edits the harness makes
to a live conversation. ``AgentRole._react_loop`` now records those as **history parts**: every
ledger event that contributes to a conversation carries ``data["history"]``, a small dict whose
``part`` names what it adds. The ledger is then the session history, and this module reads it back.

Parts, in the order a loop records them::

    turn_start   opens a turn: its id, an optional base (a previous turn, and the view applied
                 to it), the seed messages that follow the base, the call-time figures (sent on
                 the turn's first call only) and the task's receipt handle
    user         a user message the loop appended (a steer, an inbox text, a nudge, the grace
                 notice); the text is the event's ``data["message"]``
    model        one model call's returned turn, whole (``ModelResponse`` without ``raw``);
                 ``in_history`` says whether the loop appended it (a turn that ends the loop is not)
    tool_result  one tool result; text, error flag, name and call id are the event's own fields;
                 the part adds its figures (each with its receipt handle) and documents
    results_end  closes one batch of tool results into a tool turn; ``suffix`` is text the loop
                 appended to every result in it (the grace countdown)
    rewrite      the harness edited the live conversation (the lead's coding-span compaction);
                 replayed with the same function the loop used

A figure is recorded by its receipt handle where the run's store holds one, and always by its full
reference (the presentation caption included), so a rebuild renders the same request.

Messages and returned turns are stored as JSON **strings** (``seed_json``, ``response_json``) inside
the part: the ledger writes its lines with sorted keys, and a resend from the ledger has to carry a
tool call's arguments and a provider block's keys in the order the provider produced them.

Each part names its turn explicitly, so two loops of one seat running at once (a background resume
beside a new turn) never interleave in a rebuild; a tool batch whose results do not answer the
model turn before it is refused as inconsistent.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from auto_research.models import ModelMessage, ModelResponse, ToolResultMessage, ToolResultPart
from auto_research.schemas import DocumentRef, FigureRef

#: The ``data`` key every history part lives under.
HISTORY_KEY = "history"

#: Views a turn may apply to its base. ``verbatim`` replays the base as it stood; ``indexed``
#: shrinks every tool output in it to one line naming its receipt (B3, :func:`index_outputs`).
VIEWS = ("verbatim", "indexed")

#: An output at most this long is already about one index line's length, so it is kept whole.
INDEX_KEEP_CHARS = 300
#: How much of a call's arguments an index line repeats (the call itself stays in the history).
INDEX_ARGS_CHARS = 200
#: Every index line starts with this, so indexing an indexed history changes nothing.
INDEX_MARK = "[earlier output]"
#: How much of an output with no receipt an index keeps: nothing else holds it for the agent.
INDEX_HEAD_CHARS = 1_500


class HistoryError(RuntimeError):
    """The ledger does not hold the history a rebuild was asked for, or holds it inconsistently.

    Raised rather than papered over: a conversation rebuilt with a part missing is a request the
    model never saw, and seeding a turn from it would put words in the agent's history.
    """


def dumps(value: Any) -> str:
    """JSON text in insertion order (never sorted), for the parts a resend replays."""
    return json.dumps(_plain(value), ensure_ascii=False)


def new_turn_id(actor: str) -> str:
    """A turn's id: the actor, then a random suffix (unique across processes and resumes)."""
    return f"{actor}#{uuid4().hex[:12]}"


# --------------------------------------------------------------------------------------------
# Encoding — the JSON form a message takes in the ledger
# --------------------------------------------------------------------------------------------

_BYTES_TAG = "$bytes_b64"


def _plain(value: Any) -> Any:
    """A JSON-safe copy; ``bytes`` (a Gemini ``thought_signature``) survive as a tagged base64."""
    if isinstance(value, bytes | bytearray):
        return {_BYTES_TAG: base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if hasattr(value, "isoformat") and callable(value.isoformat):
        return value.isoformat()
    return value


def _unplain(value: Any) -> Any:
    if isinstance(value, Mapping):
        if set(value) == {_BYTES_TAG}:
            return base64.b64decode(value[_BYTES_TAG])
        return {k: _unplain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unplain(v) for v in value]
    return value


def encode_response(response: ModelResponse) -> dict[str, Any]:
    """One returned model turn, whole, without the SDK object (``raw``) or a parsed schema."""
    return _plain(response.model_dump(mode="python", exclude={"raw", "parsed"}))


def decode_response(data: Mapping[str, Any]) -> ModelResponse:
    return ModelResponse.model_validate(_unplain(dict(data)))


def encode_message(message: Any) -> dict[str, Any]:
    """A conversation message in its ledger form."""
    if isinstance(message, ModelResponse):
        return {"kind": "response", **encode_response(message)}
    if isinstance(message, ToolResultMessage):
        return {"kind": "tool", **_plain(message.model_dump(mode="python"))}
    if isinstance(message, ModelMessage):
        out = {"kind": "message", "role": message.role, "content": message.content}
        if message.figures:
            out["figures"] = [_plain(f.model_dump(mode="python")) for f in message.figures]
        if message.documents:
            out["documents"] = [_plain(d.model_dump(mode="python")) for d in message.documents]
        return out
    raise TypeError(f"not a conversation message: {type(message).__name__}")


def decode_message(data: Mapping[str, Any]) -> Any:
    body = {k: v for k, v in data.items() if k != "kind"}
    kind = data.get("kind")
    if kind == "response":
        return decode_response(body)
    if kind == "tool":
        return ToolResultMessage.model_validate(_unplain(body))
    if kind == "message":
        return ModelMessage.model_validate(body)
    raise HistoryError(f"unknown message kind in the ledger: {kind!r}")


def figure_entries(
    figures: Sequence[FigureRef], store: Any | None = None, *, prefer: str | None = None
) -> list[dict[str, Any]]:
    """Figures as the ledger records them: the full reference, plus the receipt handle when known.

    The reference is kept whole because the caption a request renders is the presentation's
    (``[source:index] …`` in a figure label, ``Figure N — …`` in a report view), not the
    receipt's; the handle is what a reader cites and what ``view_receipt`` opens.
    """
    by_sha: dict[str, str] = {}
    if store is not None and figures:
        try:
            # A seat's own receipt wins over a byte-identical one another seat minted, which this
            # seat may be unable to view.
            for r in store.receipts(kind="figure"):
                if r.sha256 not in by_sha or (prefer and r.handle.startswith(f"{prefer}:")):
                    by_sha[r.sha256] = r.handle
        except Exception:  # noqa: BLE001 — a handle is a convenience; the reference is the record
            by_sha = {}
    return [
        {"handle": by_sha.get(f.sha256), "ref": _plain(f.model_dump(mode="python"))}
        for f in figures
    ]


def figures_from(entries: Iterable[Mapping[str, Any]]) -> list[FigureRef]:
    return [FigureRef.model_validate(entry["ref"]) for entry in entries or ()]


def document_entries(documents: Sequence[DocumentRef]) -> list[dict[str, Any]]:
    return [_plain(d.model_dump(mode="python")) for d in documents]


# --------------------------------------------------------------------------------------------
# Reading the ledger
# --------------------------------------------------------------------------------------------


def ledger_events(source: Any, actor: str, *, run_id: str | None = None) -> list[dict[str, Any]]:
    """The actor's history-bearing ledger events, oldest first, as plain dicts.

    ``source`` is a ``RunMonitor`` (its JSONL file when it writes one; otherwise its in-memory
    events, which hold everything only at ``chatter=10``), a path to a ``monitor.jsonl``, or an
    iterable of event dicts. A JSONL line is parsed only when it names the actor, so a large
    ledger is filtered before it is decoded.
    """
    path = getattr(source, "jsonl_path", None) if not isinstance(source, str | Path) else source
    if path is not None:
        rows = _from_jsonl(Path(path), actor)
        # A reused ledger file holds earlier runs too, whose handles name other stores' artifacts.
        return [r for r in rows if run_id is None or r.get("run_id") == run_id]
    if hasattr(source, "events") and not isinstance(source, list | tuple):
        if getattr(source, "chatter", 10) < 10:
            raise HistoryError(
                "this monitor writes no JSONL and keeps only events up to chatter "
                f"{source.chatter} in memory, so the history parts above it are gone"
            )
        rows = [e.model_dump(mode="json") for e in source.events]
    else:
        rows = [dict(e) for e in source]
    return [r for r in rows if r.get("actor") == actor and HISTORY_KEY in (r.get("data") or {})]


def _from_jsonl(path: Path, actor: str) -> list[dict[str, Any]]:
    needle = json.dumps(actor)
    out: list[dict[str, Any]] = []
    try:
        handle = path.open("r", encoding="utf-8")
    except FileNotFoundError:
        return out
    with handle:
        for line in handle:
            if needle not in line or f'"{HISTORY_KEY}"' not in line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn tail line from a hard kill
            if row.get("actor") == actor and HISTORY_KEY in (row.get("data") or {}):
                out.append(row)
    return out


# --------------------------------------------------------------------------------------------
# Rebuilding
# --------------------------------------------------------------------------------------------


@dataclass
class ModelCall:
    """What one model call was sent: the conversation, the call-time figures, the documents."""

    messages: list
    images: list[FigureRef]
    documents: list[DocumentRef]
    tools: list[dict] | None = None
    """The tool definitions declared on this call (``None`` when the loop recorded none)."""


@dataclass
class Turn:
    """One rebuilt turn: its conversation as it stood when the turn ended, and every call it made."""

    id: str
    actor: str
    event: str
    task_receipt: str | None
    messages: list = field(default_factory=list)
    calls: list[ModelCall] = field(default_factory=list)
    images: list[FigureRef] = field(default_factory=list)
    documents: list[DocumentRef] = field(default_factory=list)
    handles: dict[str, dict[str, str]] = field(
        default_factory=lambda: {"results": {}, "figures": {}}
    )
    """Receipt handles of everything in :attr:`messages`, inherited from the base: each tool
    call's output (``results``, by call id) and each figure (``figures``, by sha256)."""


def apply_view(
    messages: Sequence[Any], view: str, *, receipts: Mapping[str, Mapping[str, str]] | None = None
) -> list:
    """A base conversation as a later turn sees it. ``verbatim`` is the conversation unchanged;
    ``indexed`` is :func:`index_outputs` over it with ``receipts`` (a :attr:`Turn.handles`)."""
    if view == "verbatim":
        return list(messages)
    if view == "indexed":
        return index_outputs(messages, receipts or {})
    raise HistoryError(f"unknown history view {view!r}")


def index_outputs(messages: Sequence[Any], receipts: Mapping[str, Mapping[str, str]]) -> list:
    """Earlier turns as a new turn carries them: every tool output shrunk to one line (B3).

    The line names the tool, its arguments (cut at :data:`INDEX_ARGS_CHARS`), how the call ended,
    how long the output was, and the receipt ``view_receipt`` opens it by (``ToolResultPart.receipt``,
    carried on the part because call ids repeat on some paths); a figure the output carried is
    named by its handle (``receipts["figures"]``, by sha256) instead of re-sent. An output with no
    receipt keeps its first :data:`INDEX_HEAD_CHARS` characters, since nothing else holds it for
    the agent. An output already about one line long (:data:`INDEX_KEEP_CHARS`) is kept as it was.
    The model's own turns are replayed unchanged — they are echoed as received — and a trailing
    turn whose calls were never answered (a turn cut off mid-batch) is left out, since a call
    without its result is a request no provider accepts.
    """
    figure_handles = dict(receipts.get("figures") or {})
    out: list = []
    calls: dict[str, Any] = {}
    for message in messages:
        if isinstance(message, ModelResponse):
            calls = {c.id: c for c in message.tool_calls}  # the turn these results answer
        elif isinstance(message, ToolResultMessage):
            message = ToolResultMessage(results=[
                _index_part(p, calls.get(p.tool_call_id), figure_handles) for p in message.results
            ])
        out.append(message)
    while out and isinstance(out[-1], ModelResponse) and (
        out[-1].tool_calls or out[-1].has_unresolved_server_tool
    ):
        out.pop()
    return out


def _index_part(part: ToolResultPart, call: Any, figures: Mapping[str, str]) -> ToolResultPart:
    if not part.figures and not part.documents and (
        len(part.content) <= INDEX_KEEP_CHARS or part.content.startswith(INDEX_MARK)
    ):
        # A short output is kept whole, with its receipt named, so a value in it stays citable.
        tag = f" [{part.receipt}]"
        if part.receipt and not part.content.startswith(INDEX_MARK) and not part.content.endswith(tag):
            return part.model_copy(update={"content": part.content + tag})
        return part
    args = json.dumps(dict(getattr(call, "arguments", None) or {}), ensure_ascii=False, default=str)
    if len(args) > INDEX_ARGS_CHARS:
        args = args[:INDEX_ARGS_CHARS] + "…"
    status = "returned an error" if part.is_error else "returned"
    line = f"{INDEX_MARK} {part.name or 'tool'} {status}; arguments {args}; {len(part.content):,} characters."
    if part.receipt:
        line += f' view_receipt("{part.receipt}") shows it in full.'
    elif len(part.content) > INDEX_HEAD_CHARS:
        line += f" Its first {INDEX_HEAD_CHARS:,} characters:\n{part.content[:INDEX_HEAD_CHARS]}"
    else:
        line += f"\n{part.content}"
    if part.figures:
        named = [figures.get(f.sha256) or f.path.name for f in part.figures]
        line += f" Figures: {', '.join(named)}; view_receipt shows each again."
    if part.documents:
        line += f" It attached {', '.join(d.title or d.path.name for d in part.documents)}."
    return part.model_copy(update={"content": line, "figures": [], "documents": []})


#: The first line of a turn-boundary transcript; a transcript inside a later one is kept as it is.
TRANSCRIPT_MARK = "[Your earlier turns in this session, as a record."


#: How a condensed transcript's first line reads (``AgentRole._condensed_transcript``).
CONDENSED_NOTE = "The earlier turns were condensed into the summary below.]"


def transcript(messages: Sequence[Any], figure_handles: Mapping[str, str]) -> str:
    """Earlier turns as ONE text (ruling B6.2): what was asked, what the agent wrote and called,
    each tool output as its index line (B3). No assistant turn is replayed, so no thinking,
    reasoning item or tool-use block of an earlier turn is sent again, on any provider."""
    out = [
        f"{TRANSCRIPT_MARK} Each earlier tool output is one line naming the receipt view_receipt "
        "opens; figures are named by handle, the newest attached below.]"
    ]
    calls: dict[str, Any] = {}
    for message in messages:
        if isinstance(message, ModelMessage):
            if message.role == "system":
                continue
            if message.content.startswith(TRANSCRIPT_MARK):
                head, _, body = message.content.partition("\n")
                if head.endswith(CONDENSED_NOTE):
                    body = "[The turns before these were condensed into this summary.]\n" + body
                out.append(body)
                continue
            text = f"--- to you ---\n{message.content}"
            if message.figures:
                named = [figure_handles.get(f.sha256) or Path(f.path).name for f in message.figures]
                text += f"\n[Figures attached: {', '.join(named)}; view_receipt shows each again.]"
            out.append(text)
        elif isinstance(message, ModelResponse):
            calls = {c.id: c for c in message.tool_calls}
            lines = [message.content] if message.content else []
            for call in message.tool_calls:
                args = json.dumps(dict(call.arguments or {}), ensure_ascii=False, default=str)
                lines.append(f"→ you called {call.name} {args}")
            if lines:
                out.append("--- you ---\n" + "\n".join(lines))
        elif isinstance(message, ToolResultMessage):
            for part in message.results:
                indexed = _index_part(part, calls.get(part.tool_call_id), figure_handles)
                out.append(f"← {indexed.content}")
    return "\n".join(out)


def turn_figure_sets(messages: Sequence[Any]) -> list[list[FigureRef]]:
    """Every figure set of a conversation, oldest first: a tool result's, a message's."""
    sets: list[list[FigureRef]] = []
    for message in messages:
        if isinstance(message, ModelMessage) and message.figures:
            sets.append(list(message.figures))
        elif isinstance(message, ToolResultMessage):
            sets.extend(list(p.figures) for p in message.results if p.figures)
    return sets


def rebuild(events: Iterable[Mapping[str, Any]]) -> dict[str, Turn]:
    """Every turn the events hold, keyed by turn id, in recording order (dicts keep order)."""
    turns: dict[str, Turn] = {}
    pending: dict[str, list[ToolResultPart]] = {}
    expected: dict[str, list[str]] = {}  # the call ids the next results batch must answer
    declared: dict[str, tuple[str | None, list[dict] | None]] = {}
    for row in events:
        data = row.get("data") or {}
        part = data.get(HISTORY_KEY) or {}
        kind = part.get("part")
        turn_id = part.get("turn")
        if kind == "turn_start":
            base = part.get("base")
            messages: list = []
            if base and int(base.get("count", 0)) > 0:
                prior = turns.get(base.get("through"))
                if prior is None:
                    raise HistoryError(
                        f"turn {turn_id} continues {base.get('through')!r}, which the ledger lacks"
                    )
                messages = apply_view(
                    prior.messages, base.get("view", "verbatim"), receipts=prior.handles
                )
                if len(messages) != int(base.get("count", len(messages))):
                    raise HistoryError(
                        f"turn {turn_id}: its base rebuilt to {len(messages)} messages, the loop "
                        f"recorded {base.get('count')}"
                    )
            messages.extend(decode_message(m) for m in _loads(part.get("seed_json")))
            turns[turn_id] = Turn(
                id=turn_id, actor=row.get("actor") or "", event=row.get("event") or "",
                task_receipt=part.get("task_receipt"), messages=messages,
                images=figures_from(part.get("images") or ()),
            )
            if base and base.get("through") in turns:
                inherited = turns[base["through"]].handles
                turns[turn_id].handles = {k: dict(v) for k, v in inherited.items()}
            pending[turn_id] = []
            expected[turn_id] = []
            declared[turn_id] = (None, None)
            continue
        turn = turns.get(turn_id)
        if turn is None:
            raise HistoryError(f"a {kind!r} part names turn {turn_id!r}, which never started")
        if kind == "user":
            turn.messages.append(ModelMessage(
                role="user", content=str(data.get("message", "")),
                figures=figures_from(part.get("figures") or ()),
                documents=[DocumentRef.model_validate(d) for d in part.get("documents") or ()],
            ))
        elif kind == "model":
            if "tools_json" in part:
                declared[turn_id] = (part.get("tools_sha256"), _loads(part["tools_json"]))
            sha, tools = declared[turn_id]
            if part.get("tools_sha256") and part.get("tools_sha256") != sha:
                raise HistoryError(f"turn {turn_id}: a call names a tool set the ledger never held")
            # The call's input is the conversation as it stands right now: the loop records the
            # turn it received before appending anything after it, and within a turn it only
            # appends (ruling B6), so the record is the request.
            turn.calls.append(ModelCall(
                messages=list(turn.messages), images=[], documents=[],
                tools=tools,
            ))
            response = decode_response(_loads(part["response_json"]))
            if part.get("in_history", True):
                turn.messages.append(response)
                expected[turn_id] = [c.id for c in response.tool_calls]
        elif kind == "tool_result":
            pending[turn_id].append(ToolResultPart(
                tool_call_id=str(data.get("tool_call_id") or ""),
                name=str(data.get("tool") or ""),
                content=str(data.get("text") or ""),
                is_error=bool(data.get("is_error")),
                receipt=part.get("result_receipt"),
                figures=figures_from(part.get("figures") or ()),
                documents=[DocumentRef.model_validate(d) for d in part.get("documents") or ()],
            ))
            for entry in part.get("figures") or ():
                if entry.get("handle"):
                    turn.handles["figures"][entry["ref"]["sha256"]] = entry["handle"]
            turn.documents.extend(pending[turn_id][-1].documents)
        elif kind == "results_end":
            suffix = str(part.get("suffix") or "")
            results = pending[turn_id]
            if [r.tool_call_id for r in results] != expected[turn_id]:
                raise HistoryError(
                    f"turn {turn_id}: a results batch answers {[r.tool_call_id for r in results]}, "
                    f"the model turn before it called {expected[turn_id]}"
                )
            expected[turn_id] = []
            if suffix:
                results = [p.model_copy(update={"content": p.content + suffix}) for p in results]
            turn.messages.append(ToolResultMessage(results=results))
            pending[turn_id] = []
        elif kind == "rewrite":
            turn.messages = _apply_rewrite(turn.messages, part)
        else:
            raise HistoryError(f"unknown history part {kind!r}")
    return turns


#: Provider part types that carry a model's thinking (Anthropic blocks).
THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})


def strip_thinking(messages: Sequence[Any]) -> list:
    """The conversation with every assistant turn's thinking blocks removed (ruling B6.5)."""
    out: list = []
    for message in messages:
        if isinstance(message, ModelResponse) and any(
            isinstance(p, dict) and p.get("type") in THINKING_TYPES for p in message.provider_parts
        ):
            message = message.model_copy(update={"provider_parts": [
                p for p in message.provider_parts
                if not (isinstance(p, dict) and p.get("type") in THINKING_TYPES)
            ]})
        out.append(message)
    return out


def _loads(text: Any) -> Any:
    return _unplain(json.loads(text)) if text else []


def _apply_rewrite(messages: list, part: Mapping[str, Any]) -> list:
    op = part.get("op")
    if op == "coding_span":
        from auto_research.agents.roles import _compact_coding_span

        return _compact_coding_span(messages, int(part["mark"]), str(part["summary"]))
    if op == "compact":
        from auto_research.agents.compaction import apply_compaction

        return apply_compaction(messages, part)
    if op == "strip_thinking":
        return strip_thinking(messages)
    raise HistoryError(f"unknown history rewrite {op!r}")


def turns_for(source: Any, actor: str, *, run_id: str | None = None) -> dict[str, Turn]:
    """Every turn of ``actor`` recorded in ``source`` (see :func:`ledger_events`)."""
    return rebuild(ledger_events(source, actor, run_id=run_id))


def latest_turn(source: Any, actor: str, *, run_id: str | None = None) -> Turn | None:
    """The actor's most recently started turn, or ``None`` when it has none."""
    turns = turns_for(source, actor, run_id=run_id)
    return next(reversed(turns.values()), None) if turns else None


def conversation(
    source: Any, actor: str, turn_id: str | None = None, *, view: str = "verbatim",
    run_id: str | None = None,
) -> list:
    """The conversation as it stood when ``turn_id`` (default: the latest turn) ended, in ``view``."""
    turns = turns_for(source, actor, run_id=run_id)
    if not turns:
        return []
    turn = turns.get(turn_id) if turn_id is not None else next(reversed(turns.values()))
    if turn is None:
        raise HistoryError(f"{actor} has no turn {turn_id!r} in the ledger")
    return apply_view(turn.messages, view, receipts=turn.handles)


__all__ = [
    "CONDENSED_NOTE",
    "HISTORY_KEY",
    "HistoryError",
    "INDEX_MARK",
    "TRANSCRIPT_MARK",
    "ModelCall",
    "Turn",
    "VIEWS",
    "apply_view",
    "conversation",
    "decode_message",
    "decode_response",
    "document_entries",
    "dumps",
    "encode_message",
    "encode_response",
    "figure_entries",
    "figures_from",
    "index_outputs",
    "latest_turn",
    "ledger_events",
    "new_turn_id",
    "rebuild",
    "transcript",
    "turn_figure_sets",
    "turns_for",
]
