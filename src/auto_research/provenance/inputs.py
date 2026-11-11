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
* ``setting`` — typed: text, flags, options, identifiers, operational numbers;
* ``output`` — where the tool writes what it produces: the runtime fills it, in the caller's own
  per-call folder (``provenance.calls.resolve_call_args``). Its manifest entry names its ``shape``
  (:data:`OUTPUT_SHAPES`): a ``folder``, a ``file`` (the model's value keeps only its file name) or a
  ``prefix`` for file names (the same). A folder ignores the model's value. A file or prefix may
  declare a ``default``, whose file name is the one used when the model gives none (so the manifest
  sets the extension); a folder has no name to default;
* ``record`` — an object in a named store (step 0b): ``{"store": "<name>", "bucket": "<bucket>",
  "key": "<key>", "version_id": "<version id>"}``, or a non-empty list of them. ``version_id`` is
  optional as the model writes it: the runtime pins a record without one to the store's current version before the call
  (:func:`record_pins`, through a :data:`CurrentVersion` the store seam supplies) and records the
  pinned value with the call, so the call's arguments name the exact version it read.

A tool entry may also declare ``writes`` (the stores it creates products in, a list of store names)
and ``algorithm_version`` (text the tool changes whenever its outputs change; part of a product's
identity), required with ``writes`` (:func:`spec_problems`). A tool names records, never a store's
location: the retired kind ``store`` is refused with ``record`` as their replacement.

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

#: The kinds an argument declares (the toolkit README's four, ``output`` 2026-11-04, ``record`` 2026-11-10).
ARG_KINDS: tuple[str, ...] = ("data", "parameter", "path", "setting", "output", "record")

#: The keys of a ``record`` value: ``store``, ``bucket`` and ``key`` required, ``version_id`` (the
#: S3 name, as the catalog's object rows use) pinned by the runtime when the model leaves it out.
RECORD_KEYS: tuple[str, ...] = ("store", "bucket", "key", "version_id")
#: The keys a record must name.
_RECORD_REQUIRED = frozenset({"store", "bucket", "key"})

#: Per-tool manifest keys (beside ``module``, ``fn``, ``description``, ``args_schema``): the stores
#: the tool creates products in, and the version of its algorithm (part of a product's identity).
WRITES_KEY = "writes"
#: The stores a tool reads without a ``record`` argument (through the catalog, say): symmetric with
#: ``writes``, so its package counts as a store package (the CI pipeline gives its tests a store).
READS_KEY = "reads"
ALGORITHM_VERSION_KEY = "algorithm_version"

#: Argument names that once received a store's location filled in by the runtime (the obs-db root,
#: retired in step 0b): a tool names records instead (kind ``record``).

#: The kind that once named a store's location (planned for step 0b, retired by ruling 2026-11-10).
RETIRED_KINDS: tuple[str, ...] = ("store",)

#: ``(store, bucket, key) -> version_id``: the version a store holds as current for ``key`` in
#: ``bucket``. Raises :class:`NoSuchRecord` when the store holds no such object (any other
#: exception is a store fault). Supplied by the store seam.
CurrentVersion = Callable[[str, str, str], str]


class NoSuchRecord(LookupError):
    """Raised by a :data:`CurrentVersion` when the store holds no object at the key."""

_RECORD_REPLACEMENT = (
    'declare it as kind record (a value {"store": "<name>", "bucket": "<bucket>", "key": "<key>"}; the '
    "runtime pins its version_id), and list the stores the tool writes products in under the tool's writes"
)

#: The manifest key an ``output`` argument names its shape under, and the shapes it may name.
OUTPUT_SHAPE_KEY = "shape"
OUTPUT_SHAPES: tuple[str, ...] = ("folder", "file", "prefix")


def declared_kinds(schema: Mapping[str, Any] | None) -> dict[str, Any]:
    """``{argument: kind}`` as an ``args_schema`` declares them (``None`` where a property has none)."""
    properties = (schema or {}).get("properties") or {}
    return {name: (prop.get("kind") if isinstance(prop, Mapping) else None) for name, prop in properties.items()}


def declared_outputs(schema: Mapping[str, Any] | None) -> dict[str, str]:
    """``{argument: shape}`` for every ``output`` argument an ``args_schema`` declares with a shape
    (:data:`OUTPUT_SHAPES`): the arguments the runtime places in the call's own folder."""
    properties = (schema or {}).get("properties") or {}
    return {name: prop[OUTPUT_SHAPE_KEY] for name, prop in properties.items()
            if isinstance(prop, Mapping) and prop.get("kind") == "output"
            and prop.get(OUTPUT_SHAPE_KEY) in OUTPUT_SHAPES}


def output_name(value: Any) -> str:
    """The file name an output keeps from ``value``: its last part, or ``""`` when it names none
    (not text, blank, ``.`` or ``..``, which would leave the call's folder)."""
    import os

    base = os.path.basename(value.strip()) if isinstance(value, str) else ""
    return "" if base in (".", "..") else base


def declared_output_names(schema: Mapping[str, Any] | None) -> dict[str, str]:
    """``{argument: file name}`` for every file or prefix output whose manifest ``default`` names
    one: the name the runtime uses when the model gives none."""
    properties = (schema or {}).get("properties") or {}
    return {name: output_name(properties[name]["default"])
            for name, shape in declared_outputs(schema).items()
            if shape != "folder" and output_name(properties[name].get("default"))}


def declared_defaults(schema: Mapping[str, Any] | None) -> dict[str, Any]:
    """``{argument: default}`` for every parameter that declares one: what the runtime passes when
    the model leaves the argument out."""
    properties = (schema or {}).get("properties") or {}
    return {name: prop["default"] for name, prop in properties.items()
            if isinstance(prop, Mapping) and prop.get("kind") == "parameter" and "default" in prop}


def declared_records(schema: Mapping[str, Any] | None) -> list[str]:
    """The arguments an ``args_schema`` declares as kind ``record``."""
    return [name for name, kind in declared_kinds(schema).items() if kind == "record"]


def record_value(value: Any) -> dict[str, str] | None:
    """``value`` as one record (``{"store", "bucket", "key"[, "version_id"]}``, each a non-blank
    string, no other key), or ``None`` when it is not one."""
    if not isinstance(value, Mapping) or not _RECORD_REQUIRED <= set(value) or not set(value) <= set(RECORD_KEYS):
        return None
    if not all(isinstance(v, str) and v.strip() and not is_ref(v) for v in value.values()):
        return None  # each field is the text itself: a reference resolves after the shape check
    return {k: value[k] for k in RECORD_KEYS if k in value}


def _records(value: Any) -> list[dict[str, str]] | None:
    """The records ``value`` holds (one, or a non-empty list of them), or ``None`` when it is
    neither."""
    one = record_value(value)
    if one is not None:
        return [one]
    if isinstance(value, list) and value:
        many = [record_value(item) for item in value]
        if all(r is not None for r in many):
            return many  # type: ignore[return-value]
    return None


class RecordPinError(LookupError):
    """A record the runtime could not pin to a version (its message is the model's)."""


def _store_fault(name: str, record: Mapping[str, str], why: str) -> str:
    return (f"argument {name}: store {record['store']!r} could not name the current version of "
            f"{record['key']!r} in bucket {record['bucket']!r} ({why}); state the store fault in your report, with the record "
            "you needed")


def record_pins(
    arguments: Mapping[str, Any], schema: Mapping[str, Any] | None, current: CurrentVersion | None,
) -> dict[str, Any]:
    """``{argument: pinned value}`` for every ``record`` argument holding a record without a
    ``version_id``: the same record (or list) with each missing ``version_id`` set to what
    ``current`` names for its store, bucket and key. The runtime records the result with the call
    (``calls.resolve_call_args``, context key ``records``), so the arguments it hashes name the
    version read. A value that is not a record is left to :func:`check_arguments`. Raises
    :class:`RecordPinError` when a record needs pinning and ``current`` is ``None`` (this call path
    reaches no store), or ``current`` finds no object (:class:`NoSuchRecord`) or fails."""
    pins: dict[str, Any] = {}
    for name in declared_records(schema):
        records = _records(arguments.get(name))
        if records is None or all("version_id" in r for r in records):
            continue
        if current is None:
            raise RecordPinError(
                f"argument {name} names a record without its version_id, and this call path reaches "
                "no store to pin it: give the record's version_id")
        pinned = []
        for record in records:
            if "version_id" not in record:
                try:
                    version = current(record["store"], record["bucket"], record["key"])
                except NoSuchRecord:
                    raise RecordPinError(
                        f"argument {name}: store {record['store']!r} holds no object at key "
                        f"{record['key']!r} in bucket {record['bucket']!r}; name a bucket and key "
                        "the store holds") from None
                except Exception as exc:  # noqa: BLE001 — a store fault is this call's error (§9.3.8)
                    raise RecordPinError(_store_fault(name, record, type(exc).__name__)) from exc
                if not isinstance(version, str) or not version.strip():
                    raise RecordPinError(_store_fault(name, record, "it returned no version"))
                record = {**record, "version_id": version}
            pinned.append(record)
        pins[name] = pinned[0] if record_value(arguments.get(name)) is not None else pinned
    return pins


def spec_problems(tool: str, spec: Any) -> list[str]:
    """Why a tool's manifest entry cannot be validated: its ``args_schema``
    (:func:`manifest_problems`), a ``writes`` or ``reads`` that is not a list of distinct store names,
    or an ``algorithm_version`` that is not text (required when the tool writes). Empty when it can."""
    if not isinstance(spec, Mapping):
        return manifest_problems(tool, None)
    problems = manifest_problems(tool, spec.get("args_schema"))
    writes = spec.get(WRITES_KEY)
    if writes is not None and not (
            isinstance(writes, list) and writes
            and all(isinstance(w, str) and w.strip() for w in writes) and len(set(writes)) == len(writes)):
        problems.append(f"{tool}: {WRITES_KEY} is {_shown(writes)}; list the names of the stores the "
                        "tool creates products in, each once")
    reads = spec.get(READS_KEY)
    if reads is not None and not (
            isinstance(reads, list) and reads
            and all(isinstance(r, str) and r.strip() for r in reads) and len(set(reads)) == len(reads)):
        problems.append(f"{tool}: {READS_KEY} is {_shown(reads)}; list the names of the stores the tool "
                        "reads without a record argument (through the catalog), each once")
    version = spec.get(ALGORITHM_VERSION_KEY)
    if version is not None and not (isinstance(version, str) and version.strip()):
        problems.append(f"{tool}: {ALGORITHM_VERSION_KEY} is {_shown(version)}; write it as text, "
                        "such as \"1\"")
    elif version is None and writes:
        problems.append(f"{tool}: it declares {WRITES_KEY} and no {ALGORITHM_VERSION_KEY}; add one, "
                        "such as \"1\", and change it whenever the tool's outputs change")
    return problems


def manifest_problems(tool: str, schema: Mapping[str, Any] | None) -> list[str]:
    """Why a tool's ``args_schema`` cannot be validated: a property with no kind or an unknown
    one, an optional parameter with no ``default``, an output with no shape, or a store's location
    (the retired kind ``store``). Empty when it can."""
    problems: list[str] = []
    if schema is not None and not isinstance(schema, Mapping):
        return [f"{tool}: args_schema is a {type(schema).__name__}, not an object"]
    properties = (schema or {}).get("properties") or {}
    if not isinstance(properties, Mapping):
        return [f"{tool}: args_schema.properties is a {type(properties).__name__}, not an object"]
    required = set(r for r in ((schema or {}).get("required") or ()) if isinstance(r, str))
    for name, prop in properties.items():
        kind = prop.get("kind") if isinstance(prop, Mapping) else None
        if kind in RETIRED_KINDS:
            problems.append(f"{tool}: argument {name} names a store's location, and a tool names "
                            f"records instead: {_RECORD_REPLACEMENT}")
        elif kind is None:
            problems.append(f"{tool}: argument {name} declares no kind (one of {', '.join(ARG_KINDS)})")
        elif kind not in ARG_KINDS:
            problems.append(f"{tool}: argument {name} declares kind {kind!r}, not one of {', '.join(ARG_KINDS)}")
        elif kind == "parameter" and name not in required and "default" not in prop:
            problems.append(f"{tool}: optional parameter {name} declares no default")
        elif kind == "output" and prop.get(OUTPUT_SHAPE_KEY) not in OUTPUT_SHAPES:
            problems.append(f"{tool}: output argument {name} declares no {OUTPUT_SHAPE_KEY} "
                            f"(one of {', '.join(OUTPUT_SHAPES)})")
        elif kind == "output" and "default" in prop and prop[OUTPUT_SHAPE_KEY] == "folder":
            problems.append(f"{tool}: output folder {name} declares a default; the runtime gives "
                            "each call its own folder, so declare none")
        elif kind == "record" and "default" in prop:
            problems.append(f"{tool}: record argument {name} declares a default; the runtime pins "
                            "only the records a call names, so make it required or leave it optional "
                            "with no default")
        elif kind == "output" and "default" in prop and not output_name(prop["default"]):
            problems.append(f"{tool}: output argument {name} declares a default that names no file; "
                            "give a file name such as 'fit.log' (its file name is what is used), or none")
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
        elif kind == "record" and _records(value) is None:
            lines.append(
                f'argument {name} is a record in a store: pass {{"store": "<store name>", "bucket": '
                '"<bucket>", "key": "<object key>"}, adding "version_id": "<version id>" to read an '
                "earlier version (a list passes several)"
            )
        elif kind == "output" and refs_in(value):
            lines.append(f"argument {name} is an output the runtime places: write a file name alone, "
                         "or leave it out")
        elif kind != "parameter" and typed_parameter(value) is not None:
            lines.append(
                f'argument {name} is {"an" if kind == "output" else "a"} {kind}, which takes no '
                'reason: write its value alone'
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
    "ALGORITHM_VERSION_KEY", "ARG_KINDS", "CurrentVersion", "NoSuchRecord", "OUTPUT_SHAPES", "OUTPUT_SHAPE_KEY", "REASON_KEY",
    "RECORD_KEYS", "RETIRED_KINDS", "RecordPinError", "RefError", "ToolValues", "VALUE_KEY",
    "READS_KEY", "WRITES_KEY", "check_arguments", "declared_defaults", "declared_kinds", "declared_output_names",
    "declared_outputs", "declared_records", "manifest_problems", "output_name", "record_pins", "record_value",
    "spec_problems", "tool_values", "typed_parameter",
]
