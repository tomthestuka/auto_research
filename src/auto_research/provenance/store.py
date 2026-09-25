"""The run's receipt store: mints, persists and re-verifies receipts.

One store per run, shared by every role, so a handle means the same artifact everywhere in the
run and survives a resume (``receipts.jsonl`` is append-only and is replayed on construction).
Text blobs are content-addressed under ``blobs/``; files a tool wrote (figures, data products)
stay where they are and are hashed in place.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from auto_research.schemas.artifacts import KIND_LETTER, InputFile, Receipt

#: A marked synthetic ObsID (§9.5): ``…/synth<YYMMDD>-<sha>/…``.
_SYNTH_RE = re.compile(r"^synth\d{6}-[0-9a-f]+$")

#: The source of every input a unit is handed: the research request and each dispatched task.
#: Every unit may cite it, since what a role was asked is part of what it may repeat.
INPUT_SOURCE = "input"

#: Tools whose arguments are code the role typed. A number a role typed into any of them cannot be
#: grounded by their output: ``echo 3.7``, or writing 3.7 to a file and reading it back, would
#: otherwise mint a receipt for a number nothing computed.
SHELL_TOOLS = frozenset({"mcp.builtin.bash_exec"})

#: Kinds whose bytes are text a quote or a grounded number can be checked against.
TEXT_KINDS = frozenset({"result", "web", "paper", "human"})


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


class ReceiptStore:
    """Append-only receipts for one run, keyed by handle."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
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

        root = record_beside(Path(run_directory), "receipts").resolve()
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

        The research request is ``kind="human", produced_by="research_request"``; a task a role
        dispatches (a reviewer's follow-up, a lead's probe) is the default. Its numbers were grounded
        where it was written, so the unit receiving it may repeat them.
        """
        digest = sha256_bytes(text.encode("utf-8"))
        for receipt in self.receipts(source=INPUT_SOURCE):
            if receipt.sha256 == digest and receipt.kind == kind:
                return receipt
        return self.mint(source=INPUT_SOURCE, kind=kind, produced_by=produced_by, data=text)

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
            listing = sorted(
                f"{p.relative_to(target)}\t{p.stat().st_size}\t{p.stat().st_mtime_ns}"
                for p in target.rglob("*") if p.is_file()
            )
            entry = InputFile(
                path=target, sha256=sha256_bytes("\n".join(listing).encode("utf-8")), basis="listing"
            )
        else:
            digest = sha256_file(target)
            entry = InputFile(path=target, sha256=digest, receipt=self._by_sha.get(digest))
        self._input_cache[key] = entry
        return entry

    # -- lookup ------------------------------------------------------------------------------

    def get(self, handle: str) -> Receipt | None:
        return self._receipts.get(handle)

    def typed_numbers(self, source: str) -> frozenset[str]:
        """Every number ``source`` typed into a shell command this run (see :data:`SHELL_TOOLS`)."""
        typed: set[str] = set()
        for receipt in self._receipts.values():
            if receipt.source == source and receipt.produced_by in SHELL_TOOLS:
                typed.update(receipt.arg_numbers)
        return frozenset(typed)

    def text(self, handle: str) -> str | None:
        """The text a quote in this receipt is checked against, or ``None`` for binary kinds."""
        receipt = self._receipts.get(handle)
        if receipt is None:
            return None
        if receipt.kind not in TEXT_KINDS and receipt.kind != "data":
            return None
        try:
            raw = Path(receipt.path).read_bytes()
        except OSError:
            return None
        return checkable_text(raw)

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

    def typed_numbers(self, source: str) -> frozenset[str]:
        return self.store.typed_numbers(source)

    def text(self, handle: str) -> str | None:
        return self.store.text(handle) if self.visible(handle) else None

    def verify(self, handle: str) -> str | None:
        if not self.visible(handle):
            return f"{handle} is not a receipt you can cite."
        return self.store.verify(handle)
