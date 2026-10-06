"""Turning one tool call into receipts: its output, the figures and files it wrote, what it read.

The tool providers call :func:`inputs_before` just before a call runs and :func:`receipt_call` just
after. Nothing here trusts what a tool says about itself: hashes are computed over the bytes on
disk, a figure whose file is missing is dropped, and a declared hash that disagrees with the file
is overridden (and said so).
"""

from __future__ import annotations

import re
import subprocess
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from auto_research.provenance.store import ReceiptStore
from auto_research.schemas.artifacts import InputFile, Receipt
from auto_research.schemas.figures import FigureRef

#: An absolute path mentioned in a tool's output; a file it names that was written during the
#: call is a product of that call.
_PATH_IN_TEXT = re.compile(r"(?<![\w.])(/[^\s'\"`,;()\[\]{}<>|]+)")
_MAX_PRODUCTS = 20


@dataclass
class CallInputs:
    """What a call could read, hashed before it ran, plus the pre-call state of every named path."""

    inputs: list[InputFile] = field(default_factory=list)
    before: dict[str, tuple[int, int] | None] = field(default_factory=dict)
    started_ns: int = 0


@dataclass
class CallReceipts:
    result: Receipt
    figures: list[FigureRef] = field(default_factory=list)
    products: list[Receipt] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def header(self) -> str:
        """The line the model sees first, naming what it can cite from this call."""
        parts = [f"[receipt {self.result.handle}]"]
        for product in self.products:
            label = product.caption if product.kind == "figure" else Path(product.path).name
            parts.append(f"[{product.handle}] {label}")
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
    except OSError:
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
    store: ReceiptStore, args: Mapping[str, Any], *, cwd: Path | None = None,
    skip_keys: Iterable[str] = (),
) -> CallInputs:
    """Hash every existing path the arguments name (skipping ``skip_keys``: data roots, outputs)."""
    state = CallInputs(started_ns=call_started_ns())
    for path in _named_paths(args, cwd, ()):
        state.before[str(path)] = _stat(path)
    for path in _named_paths(args, cwd, skip_keys):
        if state.before.get(str(path)) is None:
            continue
        try:
            state.inputs.append(store.hash_input(path))
        except OSError:
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
) -> CallReceipts:
    """Mint the call's result receipt and one receipt per figure / data file it produced.

    ``text`` is the tool's own output, before any harness annotation. Returned figures carry the
    runtime's hash, never the tool's; a figure whose file is absent is dropped with a note. A data
    product is a file named in the arguments that the call created or changed, or an absolute path
    in the output written after the call started.

    A figure or data file is receipted only where ``reachable`` says its **resolved** path lies
    (default: the configured allow-list). A tool's output, or a symlink planted in a writable
    folder, can name any host file; receipting one would put its bytes behind a handle any role
    can view (F22, 2026-10-06). Dropped with a note.
    """
    reachable = reachable or _allow_listed
    state = state or CallInputs(started_ns=call_started_ns())
    call_id = uuid.uuid4().hex
    commit = toolkit_commit(toolkit_root)
    common = dict(
        source=source, produced_by=produced_by, call_id=call_id, toolkit_commit=commit,
        args=dict(args), inputs=state.inputs, failed=failed,
    )
    extra = dict(fields or {})
    result = store.mint(kind=kind, data=text, **common, **extra)
    out = CallReceipts(result=result)

    figure_paths: set[str] = set()
    for fig in figures:
        path = Path(fig.path)
        if not path.is_file():
            out.notes.append(f"figure '{fig.caption}' dropped: {path} is not a file")
            continue
        if not reachable(path):
            out.notes.append(f"figure '{fig.caption}' dropped: {path} resolves outside the allow-list")
            continue
        receipt = store.mint(
            kind="figure", path=path, caption=fig.caption, mime_type=fig.mime_type, **common,
        )
        if receipt.sha256 != fig.sha256:
            out.notes.append(
                f"figure '{fig.caption}': the tool declared a hash that does not match the file; "
                "the file's own hash is recorded"
            )
        figure_paths.add(str(Path(receipt.path)))
        out.products.append(receipt)
        out.figures.append(fig.model_copy(update={"sha256": receipt.sha256, "path": receipt.path}))

    for path in data_products(state, text, failed=failed, figure_paths=figure_paths):
        if not reachable(path):
            out.notes.append(f"data file {path} not receipted: it resolves outside the allow-list")
            continue
        out.products.append(store.mint(kind="data", path=path, **common))
    return out


def _allow_listed(path: Path) -> bool:
    """Whether ``path`` may be receipted: not itself a symlink, and resolving inside the allow-list.

    The host ``/tmp`` is excluded while the OS jail is on: a jailed process sees a private ``/tmp``,
    so a host ``/tmp`` path in its output can only be a planted reference to someone else's file
    (an author home's credential lives there). With no allow-list configured, everything is in.
    """
    from auto_research.sandbox.fs_guard import PathGuard, load_fs_allowlist
    from auto_research.sandbox.jail import jail_enabled

    if Path(path).is_symlink():
        return False
    roots = load_fs_allowlist()
    if jail_enabled():
        host_tmp = Path("/tmp").resolve()
        roots = [root for root in roots if root != host_tmp]
    return PathGuard(roots=roots).is_allowed(path)


def data_products(
    state: CallInputs,
    text: str,
    *,
    failed: bool,
    figure_paths: Iterable[str] = (),
    stat: Callable[[Path], tuple[int, int] | None] = _stat,
    is_file: Callable[[Path], bool] = Path.is_file,
) -> list[Path]:
    """The data files a call produced, in the order they are receipted: a path the arguments name
    that the call created or changed, then an absolute path in its output written after it started.

    The one statement of the rule, shared by `receipt_call` and the gate's return check, which
    passes ``stat``/``is_file`` over what the tool's test run recorded instead of the live disk.
    """
    candidates: list[Path] = []
    for raw, before in state.before.items():
        path = Path(raw)
        now = stat(path)
        if now is not None and now != before and is_file(path):
            candidates.append(path)
    for match in _PATH_IN_TEXT.finditer(text if not failed else ""):
        path = Path(match.group(1).rstrip("."))
        now = stat(path)
        if now is not None and now[1] >= state.started_ns and is_file(path):
            candidates.append(path)
    figures = set(figure_paths)
    seen: set[str] = set(figures)
    products: list[Path] = []
    for path in candidates:
        key = str(path.resolve())
        if key in seen or len(seen) - len(figures) >= _MAX_PRODUCTS:
            continue
        seen.add(key)
        products.append(path)
    return products
