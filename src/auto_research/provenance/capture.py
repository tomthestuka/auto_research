"""Turning one tool call into receipts: its output, the figures and products it declared, what it read
and what it wrote in a store.

The tool providers call :func:`inputs_before` just before a call runs and :func:`receipt_call` just
after; what a call read and wrote in a store comes from the store's own records
(``store.records``) and is handed to :func:`receipt_call`. Nothing here trusts what a tool says
about itself: hashes are computed over the bytes on disk, a figure or product whose file is missing is dropped (and said so), and a declared hash that
disagrees with the file is overridden (and said so). Only what a tool declares is minted (user
ruling 2026-11-04, opt-in output): a file it writes and does not declare is its own scratch.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import subprocess
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from auto_research.provenance.store import ReceiptStore
from auto_research.toolkit_contract import is_toolkit_producer
from auto_research.schemas.artifacts import InputFile, Receipt, StoreObject
from auto_research.schemas.figures import FigureRef

#: The most products one call mints; a declared product past it is named in a note.
_MAX_PRODUCTS = 20
#: The keys a declared product carries (``{"products": [{"path": …, "caption": …}]}``): the
#: figure keys a tool can know. The runtime computes the rest itself: ``sha256`` from the bytes,
#: ``produced_by`` and the time from the call it ran, ``mime_type`` from the file name.
PRODUCT_KEYS: tuple[str, ...] = ("path", "caption")


@dataclass
class CallInputs:
    """What a call could read, hashed before it ran."""

    inputs: list[InputFile] = field(default_factory=list)
    started_ns: int = 0


@dataclass(frozen=True)
class DeclaredProduct:
    """One product a tool's return declares: a file it wrote and what the file is."""

    path: Path
    caption: str


def parse_products(raw: object) -> tuple[list[DeclaredProduct], list[str]]:
    """A tool's returned ``products`` value as declared products, each malformed entry dropped with
    a note the model reads (the figure contract's reading, ``sandbox.runner.parse_tool_figures``).
    The one reading, shared by :func:`receipt_call` and the gate's return check."""
    if not isinstance(raw, list):
        return [], ([f"'products' is a {type(raw).__name__}; return a list of "
                     '{"path": …, "caption": …}'] if raw else [])
    products: list[DeclaredProduct] = []
    notes: list[str] = []
    for index, entry in enumerate(raw):
        ok = isinstance(entry, Mapping) and set(entry) == set(PRODUCT_KEYS) and all(
            isinstance(entry[key], str) and entry[key].strip() for key in PRODUCT_KEYS)
        if not ok:
            notes.append(f"product[{index}] dropped: give exactly "
                         + ", ".join(PRODUCT_KEYS) + ", each non-empty text")
            continue
        products.append(DeclaredProduct(path=Path(entry["path"].strip()), caption=entry["caption"].strip()))
    return products, notes


@dataclass
class CallReceipts:
    result: Receipt
    figures: list[FigureRef] = field(default_factory=list)
    products: list[Receipt] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: The flags this call earned itself (``failed``, its own store records' flags, a read whose bytes
    #: differ from what its in-run writer recorded): what is stamped on its products in the store's
    #: catalog. The receipt's ``flags`` add what it carried from its inputs and parents.
    own_flags: frozenset[str] = frozenset()

    def header(self) -> str:
        """The line the model sees first, naming what it can cite from this call."""
        parts = [f"[receipt {self.result.handle}]"]
        if self.result.parents:
            parts[0] += " ← " + ", ".join(self.result.parents)
        for product in self.products:
            label = product.caption if product.kind == "figure" else Path(product.path).name
            # One line, whatever the tool put in a caption or a file name: a newline there would
            # forge a runtime line below the header.
            parts.append(f"[{product.handle}] {' '.join(str(label or '').split())}")
        return " · ".join(parts)


def call_started_ns():
    """A call's start time on the clock the kernel stamps file mtimes with.

    Linux stamps an mtime from the coarse realtime clock, which lags ``time.time_ns()`` by up to a
    tick: measured 2026-09-25, 200 of 200 files written right after ``time.time_ns()`` carried an
    earlier mtime, so a fast tool's output read as older than its own call and was never receipted.
    Stdlib-only: the gate's return-check recorder runs this same source in the analysis interpreter.
    """
    import sys
    import time

    # Python exposes no constant for it; 5 is CLOCK_REALTIME_COARSE in the Linux ABI (measured
    # 0 of 500 out of order in both interpreters, against 200 of 200 for ``time.time_ns()``).
    if sys.platform.startswith("linux"):
        return time.clock_gettime_ns(5)
    return time.time_ns()


def _stat(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except (OSError, ValueError):  # ValueError: an embedded NUL in a model-written path
        return None
    return (st.st_size, st.st_mtime_ns)


def _named_paths(args: Mapping[str, Any], cwd: Path | None, skip: Iterable[str]) -> list[Path]:
    skip = set(skip)
    found: list[Path] = []

    def visit(value: Any) -> None:
        if isinstance(value, str) and value and ("/" in value or "." in value) and len(value) < 4096:
            candidate = Path(value)
            if not candidate.is_absolute() and cwd is not None:
                candidate = cwd / candidate
            found.append(candidate)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    for key, value in args.items():
        if key not in skip:
            visit(value)
    return found


def inputs_before(
    store: ReceiptStore, args: Mapping[str, Any], *, cwd: Path | None = None, skip_keys: Iterable[str] = (),
) -> CallInputs:
    """Hash every existing file or folder the arguments name, before the call runs (skipping
    ``skip_keys``: outputs, records). What a call reads in a store is recorded from the store's own
    records after it ran (``store.records``), never from a path."""
    state = CallInputs(started_ns=call_started_ns())
    for path in _named_paths(args, cwd, skip_keys):
        if _stat(path) is None:
            continue
        try:
            state.inputs.append(store.hash_input(path))
        except Exception:  # noqa: BLE001 — an input that cannot be hashed is left out; the call runs (§9.3.8)
            continue
    return state


def toolkit_commit(root: Path | None) -> str | None:
    """The toolkit commit a call ran against (read-only ``rev-parse``; ``None`` when unknown)."""
    if root is None:
        return None
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = done.stdout.strip()
    return sha if done.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else None


def receipt_call(
    store: ReceiptStore,
    *,
    source: str,
    produced_by: str,
    args: Mapping[str, Any],
    text: str,
    failed: bool,
    state: CallInputs | None = None,
    figures: Sequence[FigureRef] = (),
    cwd: Path | None = None,
    toolkit_root: Path | None = None,
    kind: str = "result",
    fields: Mapping[str, Any] | None = None,
    reachable: Callable[[Path], bool] | None = None,
    env: str | None = None,
    env_default: bool = True,
    env_untested: bool = False,
    parents: Sequence[str] = (),
    returned: Mapping[str, Any] | None = None,
    call_id: str | None = None,
    products: Sequence[DeclaredProduct] = (),
    products_dir: Path | None = None,
    reads: Sequence[InputFile] = (),
    writes: Sequence[StoreObject] = (),
    flags: Iterable[str] = (),
    carried: Iterable[str] = (),
    commit: Any = ...,
) -> CallReceipts:
    """Mint the call's result receipt and one receipt per figure and declared product.

    ``text`` is the tool's own output, before any harness annotation. Returned figures carry the
    runtime's hash, never the tool's. Figures and ``products`` (:func:`parse_products`, the files
    the tool declares it wrote, each a ``data`` receipt over the file's own bytes) are opt-in
    artifacts under one rule: each is minted when it is a file inside ``products_dir``, the call's
    own folder (``provenance.calls.resolve_call_args``), reached through no link; any other is
    dropped with a note, and so is each product past ``_MAX_PRODUCTS``. Nothing else a call wrote
    is minted.

    A figure or product is receipted only where ``reachable`` says its **resolved** path lies
    (default: the configured allow-list). A tool's output, or a symlink planted in a writable
    folder, can name any host file; receipting one would put its bytes behind a handle any role
    can view (F22, 2026-10-06). Dropped with a note.

    ``parents``: the receipts the call's ``$ref`` arguments resolved from (``provenance.refs``);
    every receipt the call mints names them, beside the producers of the files it read.
    ``returned``: the keys a toolkit tool returned beside its stdout; stored as one JSON blob and
    named on the result receipt (``returned_sha256``), which is what a ``$ref`` resolves against.
    ``call_id``: the id the caller allocated before the call ran (it names the call's own products
    folder, ``provenance.calls.resolve_call_args``); a fresh one when the caller allocated none.

    ``reads``, ``writes``, ``flags``: what the call read and wrote in a store, from the store's own
    records (``store.records.StoreRecords.bracket``). Each object read joins the call's inputs,
    linked to the in-run receipt that wrote that version when its bytes are the ones written (else
    the call is marked ``unverified``, with a note); ``writes`` is named on the result receipt;
    ``flags`` (the call's own) and ``carried`` (from the products its reads descend from) mark every
    receipt the call mints; ``CallReceipts.own_flags`` keeps the call's own apart.

    ``commit``: the toolkit commit the process that ran the call imported (a REPL's boot commit,
    ``PersistentSandbox.call_commit``, the value its keyed tools were handed); omitted, the toolkit's
    HEAD now (:func:`toolkit_commit`).
    """
    reachable = reachable or _allow_listed
    state = state or CallInputs(started_ns=call_started_ns())
    call_id = call_id or uuid.uuid4().hex
    own = set(flags) | ({"failed"} if failed else set())
    linked, link_notes = _linked_reads(store, reads)
    if link_notes:
        own.add("unverified")
    marks = own | set(carried)
    commit = toolkit_commit(toolkit_root) if commit is ... else commit
    common = dict(
        source=source, produced_by=produced_by, call_id=call_id, toolkit_commit=commit,
        # The root names where a toolkit tool lives; a harness call (the shell, a read) has none.
        toolkit_root=(Path(toolkit_root).resolve()
                      if commit is not None and is_toolkit_producer(produced_by) else None),
        args=dict(args), inputs=[*state.inputs, *linked], failed=failed, env=env, env_default=env_default,
        parents=list(parents), flags=sorted(marks),
        **({"env_untested": True} if env_untested else {}),
    )
    extra = dict(fields or {})
    if returned:
        # Key order as the tool returned it: a consumer may iterate the dict.
        extra["returned_sha256"] = store.put_blob(json.dumps(dict(returned), default=str))[1]
        # The call's arguments under their own hash (`args_sha256`), so a value the call was
        # given and merely echoes is known as the caller's parameter (`refs.call_arguments`).
        from auto_research.provenance.calls import canonical_args

        store.put_blob(canonical_args(dict(args)))
    if writes:
        extra["writes"] = list(writes)
    result = store.mint(kind=kind, data=text, **common, **extra)
    out = CallReceipts(result=result, notes=link_notes, own_flags=frozenset(own))

    # One decision for both kinds, shared with the gate's return check (`admit_artifacts`).
    def joined(path: Path) -> Path:
        return path if path.is_absolute() or cwd is None else Path(cwd) / path

    artifacts = ([("figure", fig.caption, joined(Path(fig.path)), products_dir, cwd) for fig in figures]
                 + [("product", product.caption, joined(product.path), products_dir, cwd) for product in products])
    admitted, notes = admit_artifacts(artifacts, reachable)
    out.notes.extend(notes)
    for fig, path in zip(figures, admitted[:len(figures)]):
        if path is None:
            continue
        receipt = store.mint(
            kind="figure", path=path, caption=fig.caption, mime_type=fig.mime_type, **common,
        )
        if receipt.sha256 != fig.sha256:
            out.notes.append(
                f"figure '{fig.caption}': the tool declared a hash that does not match the file; "
                "the file's own hash is recorded"
            )
        out.products.append(receipt)
        # The runtime's handle, over any the tool declared: a reader opens the figure by it.
        out.figures.append(fig.model_copy(update={"sha256": receipt.sha256, "path": receipt.path,
                                                  "receipt": receipt.handle}))
    for product, path in zip(products, admitted[len(figures):]):
        if path is None:
            continue
        out.products.append(store.mint(
            kind="data", path=path, caption=product.caption,
            mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream", **common,
        ))
    return out


def _linked_reads(store: ReceiptStore, reads: Sequence[InputFile]) -> tuple[list[InputFile], list[str]]:
    """Each object read linked to the in-run receipt that wrote its version (its parent), when the
    bytes read are the bytes written; a note per version whose bytes changed since it was written."""
    linked: list[InputFile] = []
    notes: list[str] = []
    for item in reads:
        obj = item.object
        writer = None if obj is None else store.object_writer(obj.store, obj.bucket, obj.key, obj.version_id)
        if writer is None:
            linked.append(item)
            continue
        written = next(w for w in writer.writes if (w.store, w.bucket, w.key, w.version_id)
                       == (obj.store, obj.bucket, obj.key, obj.version_id))
        if written.sha256 is not None and written.sha256 != obj.sha256:
            notes.append(f"{obj.store} {obj.bucket}/{obj.key} version {obj.version_id} differs from what "
                         f"{writer.handle} wrote (written {written.sha256[:12]}, read {obj.sha256[:12]}): "
                         "it was changed in the store's folder after it was written")
            linked.append(item)
            continue
        linked.append(item.model_copy(update={"receipt": writer.handle}))
    return linked, notes


#: The action a worker has when its tool's product is dropped: the tool is the coding pipeline's.
_ASK = "name it in your report's tool_request so the tool writes its products into its call's folder"
#: The same, for products past the cap.
_ASK_FEWER = "ask in tool_request for the tool to gather them into one file or a few"


def admit_artifacts(
    artifacts: Sequence[tuple],
    reachable: Callable[[Path], bool], ask: str = _ASK, ask_fewer: str = _ASK_FEWER,
) -> tuple[list[Path | None], list[str]]:
    """Which of one call's figures and products are minted: ``(resolved path or None per artifact,
    a note per dropped one)``. Each artifact is ``(kind, caption, path, folder, workdir[, anchor])``
    (``anchor``: the folder's resolved place, when the caller recorded it before the call), kind
    ``"figure"`` or ``"product"``, in the order they are minted (figures first); ``ask`` and
    ``ask_fewer`` are the actions a note names (the worker's, by default). The one statement of the opt-in rule: ``receipt_call`` mints by it, and
    the gate's return check runs this same source in the test process (``sandbox.returns``), so a
    product the runtime would drop fails the gate. Standard library only."""
    admitted: list[Path | None] = []
    notes: list[str] = []
    minted: set[str] = set()
    count, total = 0, sum(1 for artifact in artifacts if artifact[0] == "product")
    seen, capped = 0, False
    for kind, caption, path, folder, workdir, *anchor in artifacts:
        seen += kind == "product"
        if capped and kind == "product":
            admitted.append(None)
            continue
        problem = _product_problem(path, folder, workdir, reachable, minted, ask, *anchor)
        if problem is None and kind == "product" and count >= _MAX_PRODUCTS:
            notes.append(f"{total - seen + 1} more products not receipted: one call receipts up to "
                         f"{_MAX_PRODUCTS}; {ask_fewer}")
            capped = True
            admitted.append(None)
            continue
        if problem is not None:
            notes.append(f"{kind} '{caption}' not receipted: {problem}")
            admitted.append(None)
            continue
        resolved = Path(path).resolve()  # the file the check named, minted by that name
        minted.add(str(resolved))
        admitted.append(resolved)
        count += kind == "product"
    return admitted, notes


def _product_problem(path: Path, folder: Path | None, workdir: Path | None,
                     reachable: Callable[[Path], bool], minted: set[str], ask: str = _ASK,
                     anchor: Path | None = None) -> str | None:
    """Why a declared product is not minted, in words the model acts on; ``None`` when it is.

    The folder is anchored where the runtime made it, below the unit's ``workdir`` (whose own
    path the unit cannot change), so a folder the tool replaced by a link is not followed; the
    declared path must then resolve to exactly its place in that folder (no link anywhere, a
    ``..`` read after the link it follows, as the kernel does)."""
    if folder is None:
        return f"this tool has no output argument, so its call has no products folder; {ask}"
    written, own = Path(os.path.abspath(path)), Path(os.path.abspath(folder))
    if not written.is_relative_to(own):
        return f"{path} lies outside this call's products folder {folder}; {ask}"
    try:
        if anchor is None:
            anchor = (Path(workdir).resolve() / own.relative_to(os.path.abspath(workdir))
                      if workdir is not None and own.is_relative_to(os.path.abspath(workdir)) else own.resolve())
        linked = Path(path).resolve() != anchor / written.relative_to(own)
    except (OSError, RuntimeError, ValueError):  # a NUL or a link loop
        return f"{path} does not resolve to a file"
    if linked:
        return f"{path} is reached through a link; {ask}"
    resolved = Path(path).resolve()
    if not resolved.is_file():
        return f"{path} is not a file when the call returns"
    if not reachable(path):
        return f"{path} resolves outside the allow-list"
    if str(resolved) in minted:
        return f"{path} is already receipted by this call"
    return None


def _allow_listed(path: Path) -> bool:
    """Whether ``path`` may be receipted: not itself a symlink, and resolving inside the allow-list.

    The host ``/tmp`` is excluded: a jailed process sees a private ``/tmp``, so a host ``/tmp`` path
    in its output can only be a planted reference to someone else's file (an author home's
    credential lives there). With no allow-list configured, everything is in.
    """
    from auto_research.sandbox.fs_guard import PathGuard, load_fs_allowlist

    if Path(path).is_symlink():
        return False
    host_tmp = Path("/tmp").resolve()
    configured = load_fs_allowlist()
    roots = [root for root in configured if root != host_tmp]
    if configured and not roots:  # only the host /tmp was listed: nothing is in
        return False
    return PathGuard(roots=roots).is_allowed(path)
