"""The run's receipt store: mints, persists and re-verifies receipts.

One store per run, shared by every role, so a handle means the same artifact everywhere in the
run and survives a resume (``receipts.jsonl`` is append-only and is replayed on construction).
Text blobs are content-addressed under ``blobs/``; files a tool wrote (figures, data products)
stay where they are and are hashed in place.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import threading
from collections.abc import Callable, Iterable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from auto_research.schemas.artifacts import KIND_LETTER, InputFile, Receipt

#: A marked synthetic ObsID (§9.5): ``…/synth<YYMMDD>-<sha>/…``.
_SYNTH_RE = re.compile(r"^synth\d{6}-[0-9a-f]+$")

#: The source of every input a unit is handed: the research request and each dispatched task.
#: Every unit may cite it, since what a role was asked is part of what it may repeat.
INPUT_SOURCE = "input"

#: The harness shell. Its output is recorded like any call's and is never evidence
#: (:func:`is_evidence`): a derived number comes from a toolkit tool (human ruling 2026-09-25).
SHELL_TOOLS = frozenset({"mcp.builtin.bash_exec"})

#: Kinds whose bytes are text a quote or a grounded number can be checked against.
TEXT_KINDS = frozenset({"result", "web", "paper", "human"})

#: The producer prefix of every toolkit tool (its registry kind, ``research_toolkit.<pkg>.<tool>``).
TOOLKIT_PRODUCER_PREFIX = "research_toolkit."

#: The one input that grounds (where cited): the human's research request, restating the question.
REQUEST_PRODUCER = "research_request"


def is_evidence(receipt: Receipt) -> bool:
    """Whether a receipt can back a number: what the analysis produced, never what a role read.

    Human ruling 2026-09-25: "citing in a report must be to sth it has produced, not just read."
    Evidence is a toolkit tool's return and the files and figures it produced; a ``commit`` the
    coding pipeline minted; a paper (attributed with its place, never text-matched); and the
    human's research request, which grounds only where cited. Everything else is the record of
    where a role looked or what it was told — ``read_file``, ``list_dir``, searches, web pages,
    the shell, a dispatched brief, a file the human sent, a published report — and grounds nothing.
    """
    if receipt.kind in ("commit", "paper"):
        return True
    if receipt.source == INPUT_SOURCE:
        return receipt.produced_by == REQUEST_PRODUCER
    return receipt.produced_by.startswith(TOOLKIT_PRODUCER_PREFIX)


#: How a refusal names a receipt that is not evidence, by producer (the rest: "a tool return").
NOT_EVIDENCE_NAMES = {
    "read_file": "a file you read", "list_dir": "a directory listing",
    "text_search": "a search result", "web_search": "a search result", "web_fetch": "a web page",
    "mcp.builtin.bash_exec": "shell output", "mcp.builtin.read_file": "a file you read",
    "mcp.builtin.list_dir": "a directory listing", "dispatch": "a task brief",
    "human_reply": "a file the human sent", "report_renderer": "an earlier published report",
    "search_literature": "a search result",
    "list_env": "an environment listing",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ReceiptedRead:
    """The bytes a reader actually uses, and what checking them against the receipt found.

    ``data`` is ``None`` only when the file could not be read without following a symlink, or is
    not a regular file: the case where reading would mean reading something else entirely.
    """

    data: bytes | None
    note: str | None = None
    changed: bool = False


#: The run's alert sink (``install_drift_alert``): called with a summary and a payload whenever a
#: receipted file is read back and no longer matches its receipt.
_DRIFT_SINK: ContextVar[Callable[[str, dict], None] | None] = ContextVar(
    "auto_research_receipt_drift", default=None
)


def install_drift_alert(monitor: object | None) -> None:
    """Make ``monitor`` the run's receipt-drift alert sink (set beside the cost meter)."""
    if monitor is None:
        _DRIFT_SINK.set(None)
        return

    import asyncio

    try:
        home_loop = asyncio.get_running_loop()
    except RuntimeError:
        home_loop = None
    seen: set[tuple[str, str | None]] = set()
    lock = threading.Lock()
    pending: set = set()  # strong references, so a scheduled alert is not collected before it runs

    def sink(summary: str, data: dict) -> None:
        # Once per run per (file, bytes): the media layer re-reads the whole history on every
        # model call, and one changed figure would otherwise alert on every later request.
        key = (str(data.get("path")), data.get("actual_sha256"))
        with lock:
            if key in seen:
                return
            seen.add(key)
        coro = monitor.record(  # type: ignore[attr-defined]
            "provenance", "receipt.drift", level=2, summary=summary, data=data,
        )
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and running is home_loop:
            task = running.create_task(coro)
            pending.add(task)
            task.add_done_callback(pending.discard)
        elif home_loop is not None and not home_loop.is_closed():
            # Raised from a worker thread (e.g. asyncio.to_thread): hand it to the run's loop.
            future = asyncio.run_coroutine_threadsafe(coro, home_loop)
            pending.add(future)
            future.add_done_callback(pending.discard)
        else:
            coro.close()

    _DRIFT_SINK.set(sink)


def read_receipted(path: Path | str, expected_sha256: str | None, *, label: str = "") -> ReceiptedRead:
    """Read a receipted file once and check those very bytes against its receipt (F26).

    A receipt records a file's hash when it is minted; the file itself stays where the producing
    agent can still write it. Every later reader — figures and documents sent to a model, report
    staging, files sent to the human, receipt text — goes through here. The path (canonical, as
    recorded at mint) is opened **one component at a time without following any symlink**, so a
    file or a folder on its path swapped for a link since is refused, not followed; the file is read
    once and the bytes that will be used are hashed. Human ruling 2026-10-06: a changed file is
    **still used, with an alert** (a model-visible note, a ``receipt.drift`` ledger event). A
    symlink anywhere on the path, or a non-regular file, is not read at all — reading it would read
    something else — and is alerted the same way. ``expected_sha256=None`` (an unreceipted file)
    only gets the symlink protection.
    """
    name = label or Path(path).name
    try:
        fd = _open_without_symlinks(Path(path))
    except FileNotFoundError:
        return ReceiptedRead(None, f"[unreadable] {name}: the file is no longer on disk.", changed=True)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            note = f"[changed] {name}: its path now runs through a symlink; not read."
        else:
            note = f"[unreadable] {name}: {exc.strerror or exc}."
        _alert(note, path, expected_sha256, None)
        return ReceiptedRead(None, note, changed=True)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            note = f"[changed] {name} is no longer a regular file; not read."
            _alert(note, path, expected_sha256, None)
            return ReceiptedRead(None, note, changed=True)
        try:
            data = handle.read()
        except OSError as exc:
            return ReceiptedRead(None, f"[unreadable] {name}: {exc.strerror or exc}.", changed=True)
    actual = sha256_bytes(data)
    if expected_sha256 is not None and actual != expected_sha256:
        note = (f"[changed] {name} differs from its receipt (receipted {expected_sha256[:12]}, "
                f"now {actual[:12]}): it was modified after it was produced.")
        _alert(note, path, expected_sha256, actual)
        return ReceiptedRead(data, note, changed=True)
    return ReceiptedRead(data)


def _open_without_symlinks(path: Path) -> int:
    """An fd for ``path``, opened component by component with ``O_NOFOLLOW`` from ``/``.

    ``O_NOFOLLOW`` alone checks only the last component; a folder on the path replaced by a link
    would still be followed. Walking with ``openat`` makes each step refuse a link, so the file
    reached is the one at the canonical path recorded at mint, or nothing.
    """
    path = Path(os.path.abspath(path))
    parts = path.parts
    # O_PATH for the folders: it needs only search permission, so a traverse-only folder (mode
    # 711) on the way is walked, not refused; the file itself is opened for reading.
    walk = getattr(os, "O_PATH", os.O_RDONLY)
    fd = os.open("/", walk | os.O_DIRECTORY)
    try:
        for part in parts[1:-1]:
            next_fd = os.open(part, walk | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=fd)
    finally:
        os.close(fd)


def _alert(note: str, path: Path | str, expected: str | None, actual: str | None) -> None:
    sink = _DRIFT_SINK.get()
    if sink is not None:
        try:
            sink(note, {"path": str(path), "expected_sha256": expected, "actual_sha256": actual})
        except Exception:  # noqa: BLE001 — an alert never breaks the read it reports on
            pass


def is_synthetic_path(path: Path) -> bool:
    return any(_SYNTH_RE.match(part) for part in Path(path).parts)


_STORES: dict[Path, "ReceiptStore"] = {}
_REGISTRY_LOCK = threading.Lock()


def checkable_text(raw: bytes) -> str | None:
    """The text a quote or a number is checked against in a stored file's bytes, or ``None``.

    Shared by ``ReceiptStore.text`` and the gate's return check, so both read a file the same way.
    """
    # A PDF is never checkable text, even one whose bytes happen to decode (human ruling
    # 2026-09-24: papers are cited, not quoted).
    if raw[:5] == b"%PDF-":
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None



#: Most files a directory input is listed by; a larger one is not hashed as an input.
_MAX_LISTED_FILES = 20_000

class ReceiptStore:
    """Append-only receipts for one run, keyed by handle."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        #: The environment fingerprint stamped on a receipt whose minting call names none: the
        #: orchestrator's own, for in-process tools (set by the run, `run_research`).
        self.default_env: str | None = None
        self.blobs = self.root / "blobs"
        self.ledger = self.root / "receipts.jsonl"
        self._lock = threading.Lock()
        self._receipts: dict[str, Receipt] = {}
        self._counts: dict[tuple[str, str], int] = {}
        self._by_sha: dict[str, str] = {}
        self._input_cache: dict[tuple[str, int, int], InputFile] = {}
        if self.ledger.is_file():
            for line in self.ledger.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._index(Receipt.model_validate_json(line))

    @classmethod
    def for_run(cls, run_directory: Path) -> "ReceiptStore":
        """The ONE store of a run, shared by every caller in the process.

        It lives beside the run's ledger, **outside** the workspace any role may read (the same
        split that took the ledger out of reach). A role that could read the store could open
        another source's blob and re-mint its text as its own receipt, laundering an input past the
        "cite it where you use it" rule or a flagged receipt past its mark — found live by the
        reviewer session reading ``receipts/blobs`` with ``read_file`` (2026-09-24).

        Two instances over one directory would each keep their own handle counters and mint the
        same handle twice, so every role gets its store here rather than constructing one.
        """
        from auto_research.sandbox.workspace import record_beside

        root = record_beside(Path(run_directory), "receipts", legacy_ok=False).resolve()
        with _REGISTRY_LOCK:
            store = _STORES.get(root)
            if store is None:
                store = _STORES[root] = cls(root)
        return store

    # -- minting -----------------------------------------------------------------------------

    def mint(
        self,
        *,
        source: str,
        kind: str,
        produced_by: str,
        data: bytes | str | None = None,
        path: Path | None = None,
        call_id: str | None = None,
        toolkit_commit: str | None = None,
        env: str | None = None,
        env_default: bool = True,
        args: object | None = None,
        inputs: Sequence[InputFile] = (),
        parents: Iterable[str] = (),
        failed: bool = False,
        **fields: object,
    ) -> Receipt:
        """Store ``data`` as a blob (or hash the file at ``path``) and record its receipt.

        Exactly one of ``data`` / ``path``. ``args`` is hashed as canonical JSON. Parents are the
        given handles plus the receipts that produced any input's bytes; flags are this artifact's
        own (``failed``, a synthetic-marked input) plus every parent's.
        """
        if (data is None) == (path is None):
            raise ValueError("mint takes exactly one of data= or path=")
        if data is not None:
            raw = data.encode("utf-8") if isinstance(data, str) else data
            digest = sha256_bytes(raw)
            stored = self.blobs / digest[:2] / digest
            if not stored.is_file():
                stored.parent.mkdir(parents=True, exist_ok=True)
                tmp = stored.with_suffix(".tmp")
                tmp.write_bytes(raw)
                os.replace(tmp, stored)
        else:
            stored = Path(path).resolve()
            digest = sha256_file(stored)
        args_sha = (
            sha256_bytes(json.dumps(args, sort_keys=True, default=str).encode("utf-8"))
            if args is not None else None
        )
        parent_set = list(dict.fromkeys([*parents, *(i.receipt for i in inputs if i.receipt)]))
        flags: set[str] = set()
        if failed:
            flags.add("failed")
        if any(is_synthetic_path(i.path) for i in inputs):
            flags.add("synthetic")
        with self._lock:
            for parent in parent_set:
                known = self._receipts.get(parent)
                if known is not None:
                    flags.update(known.flags)
            key = (source, kind)
            n = self._counts.get(key, 0) + 1
            receipt = Receipt(
                handle=f"{source}:{KIND_LETTER[kind]}{n}",
                kind=kind,
                path=stored,
                sha256=digest,
                produced_by=produced_by,
                created_at=datetime.now(timezone.utc),
                call_id=call_id,
                toolkit_commit=toolkit_commit,
                # The run's default only for a call that names no environment of its own; a call
                # whose environment could not be fingerprinted stays unstamped, never mislabelled.
                env=env if env is not None or not env_default else self.default_env,
                args_sha256=args_sha,
                inputs=list(inputs),
                parents=parent_set,
                flags=sorted(flags),
                **fields,
            )
            self.root.mkdir(parents=True, exist_ok=True)
            with open(self.ledger, "a", encoding="utf-8") as handle:
                handle.write(receipt.model_dump_json() + "\n")
            self._index(receipt)
        return receipt

    def mint_input(
        self, text: str, *, kind: str = "result", produced_by: str = "dispatch"
    ) -> Receipt:
        """The receipt of a text a unit was handed (idempotent: one receipt per distinct text).

        The research request is ``kind="human", produced_by="research_request"`` and grounds where a
        text cites it (restating the question). A task a role dispatches (a reviewer's follow-up, a
        lead's probe) is the default: a brief, recorded as what the unit was handed, which grounds
        nothing (:func:`is_evidence`, human ruling 2026-09-25).
        """
        digest = sha256_bytes(text.encode("utf-8"))
        for receipt in self.receipts(source=INPUT_SOURCE):
            if receipt.sha256 == digest and receipt.kind == kind:
                return receipt
        # Nothing ran to produce the task: no environment, not the run's default.
        return self.mint(source=INPUT_SOURCE, kind=kind, produced_by=produced_by, data=text,
                         env_default=False)

    def _index(self, receipt: Receipt) -> None:
        self._receipts[receipt.handle] = receipt
        key = (receipt.source, receipt.kind)
        n = int(receipt.handle.split(":", 1)[1][1:])
        self._counts[key] = max(self._counts.get(key, 0), n)
        self._by_sha.setdefault(receipt.sha256, receipt.handle)

    # -- inputs ------------------------------------------------------------------------------

    def hash_input(self, path: Path) -> InputFile:
        """Hash one input once per run, keyed by (path, size, mtime); directories by listing."""
        target = Path(path).resolve()
        stat = target.stat()
        key = (str(target), stat.st_size, stat.st_mtime_ns)
        cached = self._input_cache.get(key)
        if cached is not None:
            return cached
        if target.is_dir():
            listing = []
            for p in target.rglob("*"):
                if p.is_file():
                    listing.append(f"{p.relative_to(target)}\t{p.stat().st_size}\t{p.stat().st_mtime_ns}")
                    if len(listing) > _MAX_LISTED_FILES:
                        # Not an input anyone can trace (a whole scratch tree): left unhashed.
                        raise OSError(f"{target} holds more than {_MAX_LISTED_FILES} files")
            # File names need not be UTF-8 (B5 live cell 2026-10-07: one ended the run here).
            entry = InputFile(
                path=target, sha256=sha256_bytes("\n".join(sorted(listing)).encode("utf-8", "surrogateescape")),
                basis="listing",
            )
        else:
            digest = sha256_file(target)
            entry = InputFile(path=target, sha256=digest, receipt=self._by_sha.get(digest))
        self._input_cache[key] = entry
        return entry

    # -- lookup ------------------------------------------------------------------------------

    def get(self, handle: str) -> Receipt | None:
        return self._receipts.get(handle)

    def text(self, handle: str) -> str | None:
        """The text a quote in this receipt is checked against, or ``None`` for binary kinds."""
        receipt = self._receipts.get(handle)
        if receipt is None:
            return None
        if receipt.kind not in TEXT_KINDS and receipt.kind not in ("data", "commit"):
            return None
        text, _note = self.text_with_note(handle)
        return text

    def read(self, handle: str) -> ReceiptedRead | None:
        """The receipt's bytes, read once and checked (see :func:`read_receipted`)."""
        receipt = self._receipts.get(handle)
        if receipt is None:
            return None
        return read_receipted(receipt.path, receipt.sha256, label=handle)

    def text_with_note(self, handle: str) -> tuple[str | None, str | None]:
        """The receipt's text and, when its bytes changed since the receipt, the note to show.

        The text is what a quote is checked against, so the note is returned beside it, never
        mixed into it; a reader that shows the text to a model or a human shows the note too.
        """
        receipt = self._receipts.get(handle)
        if receipt is None:
            return None, None
        if receipt.kind not in TEXT_KINDS and receipt.kind not in ("data", "commit"):
            return None, None
        read = read_receipted(receipt.path, receipt.sha256, label=handle)
        if read.data is None:
            return None, read.note
        return checkable_text(read.data), read.note

    def verify(self, handle: str) -> str | None:
        """Re-hash the stored bytes; return a problem sentence, or ``None`` when they match."""
        receipt = self._receipts.get(handle)
        if receipt is None:
            return f"{handle} is not a receipt in this run."
        path = Path(receipt.path)
        if not path.is_file():
            return f"{handle} points at {path}, which is not a file on disk."
        if sha256_file(path) != receipt.sha256:
            return f"{handle}: the bytes at {path} no longer match the hash recorded when it was produced."
        if receipt.source_path is not None and receipt.source_sha256 is not None:
            original = Path(receipt.source_path)
            if not original.is_file():
                return f"{handle}: its original file {original} is no longer on disk."
            if sha256_file(original) != receipt.source_sha256:
                return f"{handle}: its original file {original} has changed since it was read."
        return None

    def ancestors(self, handle: str) -> list[Receipt]:
        """Every receipt ``handle`` was derived from, transitively (itself excluded)."""
        seen: dict[str, Receipt] = {}
        stack = list(getattr(self._receipts.get(handle), "parents", []))
        while stack:
            parent = stack.pop()
            if parent in seen or parent not in self._receipts:
                continue
            seen[parent] = self._receipts[parent]
            stack.extend(seen[parent].parents)
        return list(seen.values())

    def receipts(self, *, source: str | None = None, kind: str | None = None) -> list[Receipt]:
        return [
            r for r in self._receipts.values()
            if (source is None or r.source == source) and (kind is None or r.kind == kind)
        ]


class ScopedLookup:
    """The part of a run's store one agent may cite.

    Its own receipts (``own_source``, read live, so a receipt minted mid-loop is citable at once),
    plus whatever was handed to it (``admit``: the handles cited in the reports it received) and
    their ancestors. Anything else answers as not citable: a worker cannot ground a number in a
    peer's receipt it never saw.
    """

    def __init__(self, store: ReceiptStore, *, own_source: str, admitted: Iterable[str] = ()) -> None:
        self.store = store
        self.own_source = own_source
        self._admitted: set[str] = set()
        self.admit(admitted)

    def admit(self, handles: Iterable[str]) -> None:
        for handle in handles:
            if handle in self._admitted or self.store.get(handle) is None:
                continue
            self._admitted.add(handle)
            self._admitted.update(r.handle for r in self.store.ancestors(handle))

    def visible(self, handle: str) -> bool:
        receipt = self.store.get(handle)
        return receipt is not None and (
            receipt.source in (self.own_source, INPUT_SOURCE) or handle in self._admitted
        )

    def handles(self) -> list[str]:
        own = [r.handle for r in self.store.receipts(source=self.own_source)]
        inputs = [r.handle for r in self.store.receipts(source=INPUT_SOURCE)]
        return inputs + own + sorted(self._admitted - set(own) - set(inputs))

    def get(self, handle: str) -> Receipt | None:
        return self.store.get(handle) if self.visible(handle) else None


    def text(self, handle: str) -> str | None:
        return self.store.text(handle) if self.visible(handle) else None

    def text_with_note(self, handle: str) -> tuple[str | None, str | None]:
        return self.store.text_with_note(handle) if self.visible(handle) else (None, None)

    def read(self, handle: str) -> "ReceiptedRead | None":
        return self.store.read(handle) if self.visible(handle) else None

    def verify(self, handle: str) -> str | None:
        if not self.visible(handle):
            return f"{handle} is not a receipt you can cite."
        return self.store.verify(handle)
