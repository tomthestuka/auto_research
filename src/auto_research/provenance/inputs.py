"""The input validator's tool-input job: a toolkit call checked against its manifest, then resolved.

Every argument of a toolkit tool declares a **kind** in its manifest (``args_schema`` property key
``kind``, user rulings 2026-11-03), and the kind says how a value may arrive:

* ``data`` — a scientific artifact another tool returned: a reference, ``{"$ref": "<handle>#…"}``,
  to one of the caller's own results (or a list or dict of references);
* ``parameter`` — a number that changes the science: a reference to the caller's own result, or a
  typed value with its reason, ``{"value": 0.99, "reason": "…"}`` (a value a brief states is typed
  this way: a brief is informal, never a source), or omitted, so the manifest's declared ``default``
  applies (the runtime passes it and records it with the call);
* ``path`` — a file or folder path, typed or a reference to a file the caller's own tool wrote;
* ``setting`` — typed: text, flags, options, identifiers, operational numbers.

:func:`check_arguments` judges the arguments as the model wrote them, before the runtime completes
the call. :func:`tool_values` then turns the completed arguments into the plain values the tool
receives: references resolved (``provenance.refs``, owner must be the caller), each typed
parameter's reason set aside. The reason stays in the call's arguments, which the receipt hashes,
so a chosen value and why it was chosen live behind the handle of the call that used it.
It is the counterpart of the ``turn_end`` validator: one checks what enters a call, the other what
leaves the unit (CLAUDE.md §9.3.9). Standard library only.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from auto_research.provenance.refs import (
    REASON_KEY,
    REF_KEY,
    VALUE_KEY,
    RefError,
    is_ref,
    refs_in,
    resolve_refs,
    typed_parameter,
)
from auto_research.provenance.store import ReceiptStore

#: The kinds an argument declares, in the order the toolkit README lists them.
ARG_KINDS: tuple[str, ...] = ("data", "parameter", "path", "setting")


def declared_kinds(schema: Mapping[str, Any] | None) -> dict[str, Any]:
    """``{argument: kind}`` as an ``args_schema`` declares them (``None`` where a property has none)."""
    properties = (schema or {}).get("properties") or {}
    return {name: (prop.get("kind") if isinstance(prop, Mapping) else None) for name, prop in properties.items()}


def declared_defaults(schema: Mapping[str, Any] | None) -> dict[str, Any]:
    """``{argument: default}`` for every parameter that declares one: what the runtime passes when
    the model leaves the argument out."""
    properties = (schema or {}).get("properties") or {}
    return {name: prop["default"] for name, prop in properties.items()
            if isinstance(prop, Mapping) and prop.get("kind") == "parameter" and "default" in prop}


def manifest_problems(tool: str, schema: Mapping[str, Any] | None) -> list[str]:
    """Why a tool's ``args_schema`` cannot be validated: a property with no kind or an unknown
    one, or an optional parameter with no ``default``. Empty when it can."""
    problems: list[str] = []
    if schema is not None and not isinstance(schema, Mapping):
        return [f"{tool}: args_schema is a {type(schema).__name__}, not an object"]
    properties = (schema or {}).get("properties") or {}
    if not isinstance(properties, Mapping):
        return [f"{tool}: args_schema.properties is a {type(properties).__name__}, not an object"]
    required = set(r for r in ((schema or {}).get("required") or ()) if isinstance(r, str))
    for name, prop in properties.items():
        kind = prop.get("kind") if isinstance(prop, Mapping) else None
        if kind is None:
            problems.append(f"{tool}: argument {name} declares no kind (one of {', '.join(ARG_KINDS)})")
        elif kind not in ARG_KINDS:
            problems.append(f"{tool}: argument {name} declares kind {kind!r}, not one of {', '.join(ARG_KINDS)}")
        elif kind == "parameter" and name not in required and "default" not in prop:
            problems.append(f"{tool}: optional parameter {name} declares no default")
    return problems


def _all_refs(value: Any) -> bool:
    """A reference, or a non-empty list or dict whose every leaf is one (several results passed
    together)."""
    if is_ref(value):
        return True
    items = list(value.values()) if isinstance(value, dict) else value if isinstance(value, list) else None
    return bool(items) and all(_all_refs(item) for item in items)


def _shown(value: Any) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= 60 else text[:57] + "..."


def _data_ref_problem(value: Any) -> bool:
    """Whether a data argument's references reach a value someone typed rather than what a tool
    returned: a call's arguments (``#/$args``) or a task text (``input:``)."""
    from auto_research.provenance.refs import ARGS_POINTER, INPUT_SOURCE, split_ref

    for text in refs_in(value):
        handle, pointer = split_ref(text)
        if handle.startswith(INPUT_SOURCE + ":") or pointer == ARGS_POINTER or pointer.startswith(ARGS_POINTER + "/"):
            return True
    return False


def check_arguments(
    tool: str, arguments: Mapping[str, Any], schema: Mapping[str, Any] | None,
    *, runtime: frozenset[str] = frozenset(),
) -> str | None:
    """The refusal for a call whose arguments, as the model wrote them, break their kinds; ``None``
    when every argument arrives the way its kind allows. ``runtime``: argument names the runtime
    consumes itself (``env``). An argument the schema does not declare is refused: the manifest
    is the tool's interface, and an undeclared value would reach the tool unchecked."""
    unusable = manifest_problems(tool, schema)
    if unusable:
        return (
            "This tool cannot run: its manifest does not say how each argument arrives ("
            + "; ".join(unusable) + "). Raise it as a toolkit defect in tool_request."
        )
    kinds = declared_kinds(schema)
    lines: list[str] = []
    for name, value in arguments.items():
        kind = kinds.get(name)
        if name not in kinds:
            if name not in runtime:
                listed = ", ".join(sorted(kinds)) or "none"
                lines.append(f"argument {name} is not one this tool takes; its arguments: {listed}")
        elif kind == "data" and (not _all_refs(value) or _data_ref_problem(value)):
            lines.append(
                f'argument {name} is a tool\'s output: pass {{"{REF_KEY}": "<your result handle>#/<key>"}}, '
                "a value your own call returned; data the task states comes from the tool that produces it"
            )
        elif kind == "parameter" and not _all_refs(value) and typed_parameter(value) is None:
            lines.append(
                f'argument {name} is a parameter: pass {{"{REF_KEY}": "<handle>#/<key>"}} for a value one '
                "of your results holds, or "
                f'{{"{VALUE_KEY}": <the value>, "{REASON_KEY}": "<why this value>"}} for a value you '
                "choose, or leave it out for the default its description names"
            )
        elif kind != "parameter" and typed_parameter(value) is not None:
            lines.append(
                f'argument {name} is a {kind}, which takes no reason: write its value alone'
            )
    if not lines:
        return None
    return (
        "This call was not run. Each value needs to arrive from where it was produced or with why "
        "it was chosen, so the call records it:\n- " + "\n- ".join(lines)
    )


@dataclass(frozen=True)
class ToolValues:
    """What the tool receives (``sent``: plain values, no reference and no reason left) and the
    receipts the call's references named (``parents``)."""

    sent: dict[str, Any]
    parents: list[str]


def tool_values(
    args: Mapping[str, Any], schema: Mapping[str, Any] | None, store: ReceiptStore | None,
    usable: Callable[[str], bool],
) -> ToolValues:
    """The completed arguments as the tool receives them: each typed parameter's value without its
    reason, every reference resolved (``refs.resolve_refs``: the caller's own results and files). Raises :class:`RefError` on a reference that does not resolve."""
    kinds = declared_kinds(schema)
    plain: dict[str, Any] = {}
    for name, value in args.items():
        typed = typed_parameter(value) if kinds.get(name) == "parameter" else None
        plain[name] = typed[0] if typed is not None else value
    if not refs_in(plain):
        return ToolValues(sent=plain, parents=[])
    if store is None:
        raise RefError("This call path passes typed values only; write each value itself.")
    sent, parents = resolve_refs(plain, store, usable)
    return ToolValues(sent=sent, parents=parents)


__all__ = [
    "ARG_KINDS", "REASON_KEY", "VALUE_KEY", "RefError", "ToolValues", "check_arguments",
    "declared_defaults", "declared_kinds", "manifest_problems", "tool_values", "typed_parameter",
]
