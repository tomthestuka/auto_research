"""The arguments a toolkit call ran with, rebuilt from the run ledger.

A worker's call runs with the model's arguments completed by the runtime (a configured mission
folder, a figure path under the worker's folder, the shell's folder and environment). The
runtime's part is the call's *context*, recorded on its ``tool_result`` ledger line; the model's
part is on its ``tool_call`` line. :func:`resolve_call_args` is the one statement of the
completion: the tool provider builds every call with it, and the same function rebuilds a call
from the ledger, which then hashes (:func:`args_digest`) to its receipt's ``args_sha256`` exactly
when it is the call that ran. Standard library only.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from auto_research.schemas.artifacts import Receipt

#: Figure-output arguments (by name) and the extension each is given; any toolkit tool following
#: the convention has its figures placed under the calling worker's figures folder.
FIGURE_PATH_ARGS: dict[str, str] = {"save_path": ".png", "save_path_prefix": ""}

_FIGURE_EXTENSIONS = (".png", ".pdf", ".jpg", ".jpeg", ".svg")


def args_digest(args: Any) -> str:
    """The hash a receipt stamps for its call's arguments (``Receipt.args_sha256``)."""
    from auto_research.provenance.store import sha256_bytes

    return sha256_bytes(json.dumps(args, sort_keys=True, default=str).encode("utf-8"))


def resolve_call_args(arguments: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    """The arguments a call runs with: the model's, completed by the runtime's ``context``.

    ``context`` keys (each present only when it applies; see
    ``agents.react.SandboxToolProvider._call_context``):
    - ``cwd``: filled when the model names no ``cwd`` (``bash_exec``);
    - ``shell_env``: the runtime's record alone; a model-supplied value is dropped first;
    - ``observation_paths``: ``{argument: folder}``, filled when the model's value is empty;
    - ``figure_args`` + ``figures_dir`` + ``fn`` + ``stamp_ms``: each figure argument is placed in
      ``figures_dir``, keeping the model's file name (``<fn>_<stamp_ms>`` when it gave none) with
      the extension ``FIGURE_PATH_ARGS`` names;
    - ``runtime_only``: argument names the runtime consumes and never sends (``env``,
      ``shell_env``); they stay in the result, which is what the receipt hashes
      (``LedgerCall.sent_args`` drops them).
    """
    args = dict(arguments)
    if "cwd" in context and "cwd" not in args:
        args["cwd"] = context["cwd"]
    if "shell_env" in context:
        args.pop("shell_env", None)
        if context["shell_env"] is not None:
            args["shell_env"] = context["shell_env"]
    for name, path in (context.get("observation_paths") or {}).items():
        if not args.get(name):
            args[name] = path
    for name in context.get("figure_args") or ():
        value = args.get(name)
        base = os.path.basename(value.strip()) if isinstance(value, str) and value.strip() else ""
        if not base:
            base = f"{context['fn']}_{context['stamp_ms']}"
        for known in _FIGURE_EXTENSIONS:
            if base.lower().endswith(known):
                base = base[: -len(known)]
                break
        args[name] = str(Path(context["figures_dir"]) / f"{base}{FIGURE_PATH_ARGS[name]}")
    return args


@dataclass(frozen=True)
class LedgerCall:
    """One tool call that minted receipts, as the ledger recorded it, and the arguments it ran with.

    ``ran_args`` is what the call's receipts hash (``args_sha256``): the model's arguments completed
    by the runtime. ``sent_args`` is what the tool received, ``ran_args`` without the runtime-only
    entries: ``env`` picks a toolkit call's environment; ``shell_env`` names the environment a
    ``bash_exec`` command ran in, and that command reached the shell wrapped in the environment's
    activation (``sandbox.envs.in_environment``), which ``sent_args`` does not spell out.
    """

    actor: str
    tool: str
    tool_call_id: str
    call_id: str
    model_args: dict[str, Any]
    context: dict[str, Any]
    ran_args: dict[str, Any]
    args_sha256: str

    @property
    def sent_args(self) -> dict[str, Any]:
        runtime_only = self.context.get("runtime_only")
        dropped = set(runtime_only) if isinstance(runtime_only, list) else set()
        return {key: value for key, value in self.ran_args.items() if key not in dropped}


def ledger_calls(path: Path) -> list[LedgerCall]:
    """Every tool call in a run ledger (``monitor.jsonl``) that minted receipts, in ledger order.

    A call is a ``<role>.<action>.tool_call`` line and the ``.tool_result`` line of the same loop
    sharing its actor, tool and ``tool_call_id``; it minted receipts when the result line names their ``call_id`` (a call the
    loop answered without running — invalid arguments, or after an accepted ``turn_end`` — names
    none and is left out). A line that does not parse (a torn final line), or a call whose recorded
    arguments or context cannot be rebuilt, is left out rather than guessed.
    """
    pending: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    calls: list[LedgerCall] = []
    with Path(path).open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            _take(line, pending, calls)
    return calls


def _take(line: str, pending: dict, calls: list) -> None:
    """Read one ledger line into ``pending`` (an issued call) or ``calls`` (a call that minted)."""
    if '.tool_call"' not in line and '.tool_result"' not in line:
        return  # most of a ledger: never parsed
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return
    if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
        return
    name, data = str(event.get("event", "")), event["data"]
    # One loop's line pair: the same actor, loop (`<role>.<action>`), tool and call id, so two
    # loops of one actor that reuse an id never cross.
    loop, _, suffix = name.rpartition(".")
    key = (str(event.get("actor", "")), loop, str(data.get("tool", "")), str(data.get("tool_call_id", "")))
    if suffix == "tool_call":
        try:
            args = json.loads(data.get("args") or "")
        except (TypeError, json.JSONDecodeError):
            return
        if isinstance(args, dict):
            pending[key] = {"tool": key[2], "args": args}
    elif suffix == "tool_result" and key in pending:
        issued = pending.pop(key)
        call_id, context = data.get("call_id"), data.get("call_context") or {}
        if not isinstance(call_id, str) or not isinstance(context, dict):
            return
        try:
            ran = resolve_call_args(issued["args"], context)
        except (AttributeError, KeyError, TypeError, ValueError):
            return
        calls.append(LedgerCall(
            actor=key[0], tool=issued["tool"], tool_call_id=key[3], call_id=call_id,
            model_args=issued["args"], context=dict(context), ran_args=ran,
            args_sha256=args_digest(ran),
        ))


def calls_by_receipt(calls: Iterable[LedgerCall], receipts: Iterable[Receipt]) -> dict[str, LedgerCall]:
    """Match receipts to the ledger calls that minted them: ``{handle: call}``.

    By identity: the receipt's ``call_id`` is the one its call's ``tool_result`` line names, and its
    source is that line's actor. The rebuilt arguments must then hash to the receipt's
    ``args_sha256``; a receipt whose call is absent, or whose arguments do not rebuild, is left
    out. Any subset of receipts, in any order, may be passed. The reviewer and the librarian mint
    under a source that is not their ledger actor (``rev``, ``lib``), so their receipts are left
    out; neither runs toolkit tools.
    """
    by_id = {(call.actor, call.call_id): call for call in calls}
    matched: dict[str, LedgerCall] = {}
    for receipt in receipts:
        if not receipt.call_id:
            continue
        call = by_id.get((receipt.source, receipt.call_id))
        if call is not None and receipt.args_sha256 and call.args_sha256 == receipt.args_sha256:
            matched[receipt.handle] = call
    return matched
