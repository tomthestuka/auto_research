"""Receipts from the stores' own records: what one unit's call read and wrote in a store (step 0b S5).

A toolkit tool reaches a store over S3 with its unit's own key (item S4); nothing it reports about
its reads and writes is trusted. The runtime brackets each call instead:

```
 bracket(declared) ── before: ListObjectVersions under the unit's writable area; access-log offset
      │  the call runs (one call of a unit at a time: the bracket holds the unit's lock)
      ▼
 after:  ListObjectVersions again ── new versions, delete markers, removed versions
         access log since the offset ── lines by the unit's keys (catalog `credential` table):
             GetObject with a versionId  → a pinned read (an input)
             PutObject/CopyObject/CompleteMultipartUpload/DeleteObject(s) → which listed changes are
                                           this call's (a shared area holds other units' writes too)
         each new version and each read hashed by the runtime (GET by version id, SHA-256),
         cached by (store, bucket, key, version_id, size, last_modified)
      ▼
 CallIO(reads, writes, flags, notes) ──► provenance.capture.receipt_call
```

A store whose read record is ``none`` (a remote S3 without an access log) attributes every listed
change to the call, and its reads are the call's pinned ``record`` arguments. The one-call-at-a-time
assumption is the runtime's today (a unit's loop runs its calls in turn); the bracket's lock makes it
explicit, so two overlapping calls of one unit queue instead of sharing a window. A process that
outlives its call (a killed tool's child) writes into the next call's window: the reconciler's sweep
(item S6) is the check for that.

Server-specific code is the access-log reader (``versitygw`` writes AWS's server-access-log format);
everything else uses the S3 contract only.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import re
import threading
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote

from auto_research.schemas.artifacts import InputFile, StoreObject

#: Operations that create a version or a delete marker (the access log's names).
WRITE_OPS = frozenset({"s3_PutObject", "s3_CopyObject", "s3_CompleteMultipartUpload", "s3_DeleteObject",
                       "s3_DeleteObjects"})
#: Operations that read an object's bytes.
READ_OPS = frozenset({"s3_GetObject"})
#: The most object versions the summary line names (the receipt names them all).
_SUMMARY_MAX = 12
#: Chunk size for hashing a version's bytes.
_CHUNK = 1 << 20

_PREFIX = re.compile(r"^(\S+) (\S+) \[[^\]]*\] (\S+) (\S+) (\S+) (\S+) (.*)$")
_URI = re.compile(r" (/\S*) (\d{3}) ")
#: The gateway's first line in a log file (``log starts <time>``): the one line that is no request.
_HEADER = re.compile(r"^log starts \d{4}-\d\d-\d\d ")


class StoreRecordError(RuntimeError):
    """The store's records could not be read before a call (its message is the model's)."""


@dataclass(frozen=True)
class Area:
    """A part of a store a unit may write: ``bucket`` under ``prefix`` (``""``: the whole bucket)."""

    store: str
    bucket: str
    prefix: str = ""


@dataclass(frozen=True)
class LogLine:
    """One access-log request: who, what, on which object and version, with what outcome.
    ``key`` is ``None`` for a bucket-level request, and for a line whose object could not be read
    from it (``parsed`` False)."""

    requester: str
    bucket: str
    op: str
    key: str | None
    version_id: str | None
    status: int
    parsed: bool = True


def parse_access_line(line: str) -> LogLine | None:
    """One line of the gateway's access log (AWS server-access-log format), or ``None`` for a line
    that is not a request (the header). The key field is written unescaped (it may hold spaces) and
    the request URI escaped, so the object is read from the URI and accepted only where the key field
    before it says the same; a line where no reading agrees is returned with ``parsed`` False."""
    match = _PREFIX.match(line.rstrip("\n"))
    if match is None:
        return None
    _owner, bucket, _ip, requester, _request_id, op, rest = match.groups()
    for uri in _URI.finditer(rest):
        key_field = rest[:uri.start()]
        path, _, query = uri.group(1).partition("?")
        if path != f"/{bucket}" and not path.startswith(f"/{bucket}/"):
            continue
        key = unquote(path[len(bucket) + 2:]) or None
        if (key is None and key_field != "-") or (key is not None and key != key_field):
            continue
        version = parse_qs(query, keep_blank_values=True).get("versionId", [None])[0] or None
        return LogLine(requester, bucket, op, key, version, int(uri.group(2)))
    status = re.search(r" (\d{3}) ", rest)
    return LogLine(requester, bucket, op, None, None, int(status.group(1)) if status else 0, parsed=False)


def _maybe_succeeded(line: LogLine) -> bool:
    """A request that succeeded, or whose status the line does not say (counted, so nothing escapes)."""
    return line.status == 0 or 200 <= line.status < 300


# ------------------------------------------------------------------ hashing (process-wide cache)

_HASHES: dict[tuple[str, str, str, str, int, str], str] = {}
_HASHES_LOCK = threading.Lock()


def _hash_version(client: Any, store: str, bucket: str, key: str, version_id: str, size: int,
                  last_modified: str) -> str:
    """SHA-256 of one version's bytes, computed by the runtime (GET by version id, the store's own
    checksum validated on the way by the client), cached by (store, bucket, key, version, size,
    last modified): an edit in the store's folder keeps the version id but changes size or time, so
    it is hashed again. A stored SHA-256 that disagrees raises ``ValueError``."""
    cache_key = (store, bucket, key, version_id, size, last_modified)
    with _HASHES_LOCK:
        cached = _HASHES.get(cache_key)
    if cached is not None:
        return cached
    response = client.get_object(Bucket=bucket, Key=key, VersionId=version_id, ChecksumMode="ENABLED")
    digest = hashlib.sha256()
    body = response["Body"]
    try:
        for chunk in iter(lambda: body.read(_CHUNK), b""):
            digest.update(chunk)
    finally:
        body.close()
    sha = digest.hexdigest()
    stored = response.get("ChecksumSHA256")
    if stored and response.get("ChecksumType", "FULL_OBJECT") == "FULL_OBJECT":
        if base64.b64decode(stored).hex() != sha:
            raise ValueError(f"the store's own SHA-256 ({base64.b64decode(stored).hex()[:12]}) differs from "
                             f"the bytes it serves ({sha[:12]})")
    with _HASHES_LOCK:
        _HASHES[cache_key] = sha
    return sha


# ------------------------------------------------------------------ listings


@dataclass(frozen=True)
class _Version:
    size: int
    last_modified: str
    latest: bool
    marker: bool


def _listing(client: Any, area: Area) -> dict[tuple[str, str], _Version]:
    """Every version and delete marker under the area: ``{(key, version_id): _Version}``."""
    found: dict[tuple[str, str], _Version] = {}
    pages = client.get_paginator("list_object_versions").paginate(Bucket=area.bucket, Prefix=area.prefix)
    for page in pages:
        for v in page.get("Versions", []):
            found[(v["Key"], v["VersionId"])] = _Version(int(v.get("Size", 0)), str(v["LastModified"]),
                                                         bool(v.get("IsLatest")), False)
        for m in page.get("DeleteMarkers", []):
            found[(m["Key"], m["VersionId"])] = _Version(0, str(m["LastModified"]), bool(m.get("IsLatest")), True)
    return found


def _status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None) or {}
    return response.get("ResponseMetadata", {}).get("HTTPStatusCode")


# ------------------------------------------------------------------ one call's record


@dataclass
class CallIO:
    """What one call read and wrote in the stores, as the stores record it."""

    reads: list[InputFile] = field(default_factory=list)
    writes: list[StoreObject] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)
    #: flags the call's reads carry from the products they descend from (``_lineage_flags``): shown on
    #: its receipt, never stamped on its own products (ruling 11-10: derived through ancestry instead)
    carried: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    #: when the bracket opened and closed (UTC): the call's window in the catalog's event log
    window: tuple[datetime, datetime] | None = None

    def unverified(self, note: str) -> None:
        self.flags.add("unverified")
        self.notes.append(note)

    def summary(self) -> str:
        """The line the model reads after the result: each object version the call wrote and read."""
        def name(o: StoreObject) -> str:
            return f"{o.store} {o.bucket}/{o.key} version {o.version_id}"

        parts = []
        for w in self.writes:
            what = "deleted (marker)" if w.delete_marker else "wrote"
            parts.append(f"{what} {name(w)}" + (f", over version {w.overwrote}" if w.overwrote else "")
                         + ("" if w.declared else ", in a store this tool does not declare"))
        parts += [f"read {name(r.object)}" for r in self.reads if r.object is not None]
        shown = parts[:_SUMMARY_MAX] + ([f"and {len(parts) - _SUMMARY_MAX} more"] if len(parts) > _SUMMARY_MAX else [])
        return ("[store] " + " · ".join(shown)) if parts else ""


@dataclass
class _Window:
    """One store's access-log lines in a call's window, and this unit's keys there."""

    requests: list[LogLine]
    keys: set[str]


@dataclass
class _Ticket:
    listings: dict[Area, dict[tuple[str, str], _Version]]
    offsets: dict[str, int]


class StoreRecords:
    """One unit's view of the stores' records: pins records, checks named versions, and brackets
    each call (:meth:`bracket`). ``agent`` and ``run`` name the unit's keys in each store's catalog
    (``credential`` rows of kind ``s3``); ``writable``: the areas the unit's keys may write
    (:func:`writable_area`); ``stores``: the declared stores (default: ``store.config.stores()``);
    ``reconciler``: the runtime's own reconciler login (``store.keys.Credentials``, its ``store`` and
    ``dsn()``), through which this view reads that store's catalog and stamps flags; a store without
    one is read with the admin role (a test, the CLI)."""

    def __init__(self, *, agent: str, run: str, writable: Iterable[Area] = (),
                 stores: Sequence[Any] | None = None, reconciler: Any | None = None) -> None:
        from auto_research.store import config

        self.agent, self.run = agent, run
        self.stores = {s.name: s for s in (stores if stores is not None else config.stores())}
        self.writable = tuple(dict.fromkeys(writable))
        for area in self.writable:
            known = self.stores.get(area.store)
            if known is None or area.bucket not in known.buckets:
                raise ValueError(f"writable area {area.store} {area.bucket} is not a declared store bucket")
        self._clients: dict[str, Any] = {}
        self._clients_lock = threading.Lock()
        self._lock: asyncio.Lock | None = None
        self._keys: dict[str, set[str]] = {}  # the unit's keys per store, as last read from the catalog
        self.reconciler = reconciler
        #: stamps that failed, retried with the next one: (store, objects, flags, receipt, window)
        self._unstamped: list[tuple] = []

    # -- clients -------------------------------------------------------------------------------

    def client(self, store: str) -> Any:
        """The runtime's admin S3 client for ``store`` (made once; boto3 clients are thread-safe)."""
        with self._clients_lock:
            if store not in self._clients:
                self._clients[store] = self.stores[store].admin_client("s3")
            return self._clients[store]

    def _bucket_known(self, store: str, bucket: str) -> bool:
        return store in self.stores and bucket in self.stores[store].buckets

    # -- before a call: pins and named versions -------------------------------------------------

    def current_version(self, store: str, bucket: str, key: str) -> str:
        """The version a record without one is pinned to (``provenance.inputs.CurrentVersion``): the
        key's latest version; ``NoSuchRecord`` when the store, bucket or object is absent (a latest
        delete marker included)."""
        from auto_research.provenance.inputs import NoSuchRecord

        if not self._bucket_known(store, bucket):
            raise NoSuchRecord(f"{store} {bucket}/{key}")
        try:
            head = self.client(store).head_object(Bucket=bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 — a 404 is an absent object; anything else a store fault
            if _status(exc) == 404:
                raise NoSuchRecord(f"{store} {bucket}/{key}") from None
            raise
        return str(head["VersionId"])

    def version_problem(self, argument: str, record: Mapping[str, str]) -> str | None:
        """Why a record naming its ``version_id`` cannot be read (coordinator ruling d), in words the
        model acts on; ``None`` when the store holds that version."""
        store, bucket, key, version = (record["store"], record["bucket"], record["key"], record["version_id"])
        if not self._bucket_known(store, bucket):
            return (f"argument {argument}: no store {store!r} with bucket {bucket!r} is declared; name a "
                    "store and bucket the run declares")
        try:
            self.client(store).head_object(Bucket=bucket, Key=key, VersionId=version)
        except Exception as exc:  # noqa: BLE001
            if _status(exc) in (400, 404, 405):  # unknown, malformed, or a delete marker
                return (f"argument {argument}: store {store!r} holds no readable version {version!r} of "
                        f"key {key!r} in bucket {bucket!r}; give a version_id the store holds, or leave "
                        "version_id out to read the current version")
            raise
        return None

    # -- the bracket ----------------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def bracket(self, declared: Iterable[str] = (),
                      given: Sequence[Mapping[str, str]] = ()) -> AsyncIterator[CallIO]:
        """Record what the call run inside this block read and wrote in the stores. ``declared``: the
        stores the tool's manifest names in ``writes``; ``given``: the call's pinned ``record``
        arguments (its reads in a store that keeps no read record). Entering raises
        :class:`StoreRecordError` when the store cannot be listed (the call must not run); leaving
        never raises: what cannot be established marks the record ``unverified``."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:  # one call of a unit at a time: the window is that call's alone
            ticket = await asyncio.to_thread(self._begin)
            io = CallIO()
            opened = datetime.now(timezone.utc)
            try:
                yield io
            finally:
                io.window = (opened, datetime.now(timezone.utc))
                await asyncio.to_thread(self._end, ticket, io, frozenset(declared), list(given))

    def _log_stores(self) -> list[Any]:
        return [s for s in self.stores.values() if s.adapters.get("reads") == "access_log"]

    def _begin(self) -> _Ticket:
        listings: dict[Area, dict[tuple[str, str], _Version]] = {}
        for area in self.writable:
            try:
                listings[area] = _listing(self.client(area.store), area)
            except Exception as exc:  # noqa: BLE001
                raise StoreRecordError(
                    f"the store {area.store} did not list bucket {area.bucket} before the call "
                    f"({type(exc).__name__}), so what the call writes could not be recorded; state the "
                    "store fault in your report") from exc
        offsets = {}
        for s in self._log_stores():
            try:
                offsets[s.name] = os.stat(s.access_log).st_size
            except FileNotFoundError:
                offsets[s.name] = 0
            except OSError as exc:
                raise StoreRecordError(f"the store {s.name}'s access log could not be read before the call "
                                       f"({exc.strerror or exc}); state the store fault in your report") from exc
        return _Ticket(listings, offsets)

    def _end(self, ticket: _Ticket, io: CallIO, declared: frozenset[str], given: list[Mapping[str, str]]) -> None:
        try:
            windows = self._log_window(ticket, io)
            self._record_writes(ticket, io, declared, windows)
            self._record_reads(io, windows, given)
        except Exception as exc:  # noqa: BLE001 — recording never ends the call; it marks what it lost
            io.unverified(f"the store records after the call could not be read ({type(exc).__name__}: {exc})")

    def _dsn(self, store: str) -> str:
        """The catalog connection for ``store``: the runtime's reconciler login when it holds one there."""
        if self.reconciler is not None and self.reconciler.store == store:
            return self.reconciler.dsn()
        return self.stores[store].admin_dsn()

    # -- flags across runs (the catalog's product_flag) ----------------------------------------------

    def stamp_flags(self, writes: Sequence[StoreObject], own: Iterable[str], receipt: str,
                    window: tuple[datetime, datetime] | None = None, carried: Iterable[str] = ()) -> list[str]:
        """Stamp a call's flags on every product it made (``catalog.stamp_flags``, the receipt named
        ``<run>/<handle>``), so a later run reading it carries them and the reconciler never promotes
        it: the products recording its ``writes`` (catalog ``object`` rows), and every product this
        unit's catalog login claimed, committed or failed during the call (``window``, the bracket's;
        ``catalog.event``), which catches a product stored but never committed (adopted later) and
        writes the bracket could not attribute.

        ``own``: the flags the call earned itself, always stamped. ``carried``: the flags its receipt
        carries from its inputs and parents; each product gets those its recipe inputs do not already
        yield through ancestry (``_lineage_flags``: a read that is no recipe input, a parent outside the
        catalog), so a flag derivable upstream stays derived and a human clear there reaches the
        product (ruling 11-10). A stamp that fails is retried with the next one; returns a note per
        store whose catalog could not be stamped."""
        own_marks, carried_marks = sorted(set(own)), sorted(set(carried) - set(own))
        notes: list[str] = []
        pending, self._unstamped = self._unstamped, []
        if own_marks or carried_marks:
            stores = {w.store for w in writes} | (set(self.stores) if window is not None else set())
            pending += [(store, [w for w in writes if w.store == store and not w.delete_marker], own_marks,
                         carried_marks, f"{self.run}/{receipt}", window) for store in sorted(stores)]
        for job in pending:
            store, _objects, job_own, job_carried, named, _window = job
            try:
                self._stamp(*job)
            except Exception as exc:  # noqa: BLE001 — kept, retried with the next stamp
                self._unstamped.append(job)
                notes.append(f"the flags {', '.join(sorted({*job_own, *job_carried}))} of {named} could not be "
                             f"stamped on its products in the store {store}'s catalog yet ({type(exc).__name__}: "
                             f"{exc}); the runtime tries again after the next call")
        return notes

    def _stamp(self, store: str, objects: list[StoreObject], own: list[str], carried: list[str], named: str,
               window: tuple[datetime, datetime] | None) -> None:
        import psycopg

        with psycopg.connect(self._dsn(store), autocommit=True, connect_timeout=10) as conn:
            hashes = {row[0] for o in objects for row in conn.execute(
                "SELECT recipe_hash FROM catalog.object WHERE store = %s AND bucket = %s AND key = %s "
                "AND version_id = %s", (o.store, o.bucket, o.key, o.version_id)).fetchall()}
            if window is not None:
                hashes |= {row[0] for row in conn.execute(
                    "SELECT DISTINCT recipe_hash FROM catalog.event WHERE actor = %s AND recipe_hash IS NOT NULL "
                    "AND action IN ('claim', 'commit', 'fail') AND at BETWEEN %s AND %s",
                    (self.agent, *window)).fetchall()}
            recipes = {h: (conn.execute("SELECT recipe FROM catalog.product WHERE recipe_hash = %s",
                                        (h,)).fetchone() or [{}])[0] for h in hashes}
        for recipe_hash in sorted(hashes):
            marks = set(own) | (set(carried) - (self._derivable(recipes[recipe_hash]) if carried else set()))
            if not marks:
                continue
            with psycopg.connect(self._dsn(store), autocommit=True, connect_timeout=10) as conn:
                conn.execute("SELECT catalog.stamp_flags(%s, %s, %s)", (recipe_hash, sorted(marks), named))

    def _derivable(self, recipe: Mapping[str, Any]) -> set[str]:
        """The flags a product's recipe inputs yield through ancestry, as a reader of it derives them."""
        found: set[str] = set()
        for item in (recipe or {}).get("inputs", []):
            name = item.get("store") if isinstance(item, Mapping) else None
            if name not in self.stores:
                continue
            if item.get("bucket") in self.stores[name].synthetic_buckets:
                found.add("synthetic")
            found |= self._lineage_flags(name, (item["bucket"], item["key"], item["version_id"]))[0]
        return found

    def _lineage_flags(self, store: str, obj: tuple[str, str, str]) -> tuple[set[str], str | None]:
        """The flags a version read carries from the catalog: those live (stamped, not cleared by the
        human, ``catalog.live_flag``) on the product that
        recorded it and on every product its recipe's inputs came from, transitively; ``synthetic``
        when any of them wrote or read in a synthetic bucket; ``unverified`` when one of them failed
        the reconciler's check (state ``failed`` or ``nondeterministic``). ``(flags, the product
        named, or None)``."""
        import psycopg

        bucket, key, version = obj
        with psycopg.connect(self._dsn(store), autocommit=True, connect_timeout=10) as conn:
            rows = conn.execute(
                "WITH RECURSIVE lineage(recipe_hash) AS ("
                " SELECT o.recipe_hash FROM catalog.object o"
                "  WHERE o.store = %s AND o.bucket = %s AND o.key = %s AND o.version_id = %s"
                " UNION"
                " SELECT o.recipe_hash FROM lineage l JOIN catalog.product p ON p.recipe_hash = l.recipe_hash"
                "  CROSS JOIN LATERAL jsonb_array_elements(coalesce(p.recipe->'inputs', '[]'::jsonb)) i"
                "  JOIN catalog.object o ON o.store = i->>'store' AND o.bucket = i->>'bucket'"
                "   AND o.key = i->>'key' AND o.version_id = i->>'version_id')"
                " SELECT p.recipe_hash, p.state, p.recipe,"
                "  (SELECT coalesce(array_agg(DISTINCT f.flag), '{}') FROM catalog.live_flag f"
                "    WHERE f.recipe_hash = p.recipe_hash),"
                "  (SELECT coalesce(array_agg(DISTINCT o.store || '/' || o.bucket), '{}') FROM catalog.object o"
                "    WHERE o.recipe_hash = p.recipe_hash)"
                " FROM lineage l JOIN catalog.product p ON p.recipe_hash = l.recipe_hash",
                (store, bucket, key, version)).fetchall()
        synthetic = {f"{name}/{b}" for name, s in self.stores.items() for b in s.synthetic_buckets}
        flags: set[str] = set()
        for _hash, state, recipe, stamped, places in rows:
            flags.update(stamped)
            if state in ("failed", "nondeterministic"):
                flags.add("unverified")
            inputs = [f"{i.get('store')}/{i.get('bucket')}" for i in (recipe or {}).get("inputs", [])
                      if isinstance(i, dict)]
            if synthetic & {*places, *inputs}:
                flags.add("synthetic")
        direct = next((h for h, *_ in rows if h == self._recorded_by(store, obj)), None) if rows else None
        return flags, direct

    def _recorded_by(self, store: str, obj: tuple[str, str, str]) -> str | None:
        """The product whose ``object`` row records this version (the one a read names)."""
        import psycopg

        with psycopg.connect(self._dsn(store), autocommit=True, connect_timeout=10) as conn:
            row = conn.execute("SELECT recipe_hash FROM catalog.object WHERE store = %s AND bucket = %s AND key = %s "
                               "AND version_id = %s", (store, *obj)).fetchone()
        return row[0] if row else None

    def _unit_keys(self, store: Any) -> set[str]:
        """The S3 access keys the catalog names for this unit in this run (revoked ones included:
        a key revoked mid-call still wrote as this unit). A query that fails falls back to the keys
        an earlier query found (keys are issued at the run's start); with none known it raises."""
        import psycopg

        try:
            with psycopg.connect(self._dsn(store.name), autocommit=True, connect_timeout=10) as conn:
                rows = conn.execute("SELECT key_id FROM catalog.credential WHERE kind = 's3' AND agent = %s "
                                    "AND run = %s", (self.agent, self.run)).fetchall()
        except Exception:
            if self._keys.get(store.name):
                return self._keys[store.name]
            raise
        self._keys[store.name] = {row[0] for row in rows}
        return self._keys[store.name]

    def _log_window(self, ticket: _Ticket, io: CallIO) -> dict[str, _Window]:
        """Every request the store's log holds in the call's window, and this unit's keys, per store
        whose window was read."""
        windows: dict[str, _Window] = {}
        for s in self._log_stores():
            offset = ticket.offsets.get(s.name, 0)
            try:
                with open(s.access_log, "rb") as handle:
                    size = os.fstat(handle.fileno()).st_size
                    if size < offset:
                        io.unverified(f"the store {s.name}'s access log shrank during the call (rotated?); "
                                      "what the call read and wrote there is not known")
                        continue
                    starts_line = True
                    if offset:
                        handle.seek(offset - 1)
                        starts_line = handle.read(1) == b"\n"
                    raw = handle.read(size - offset)
            except OSError as exc:
                io.unverified(f"the store {s.name}'s access log could not be read ({exc.strerror or exc})")
                continue
            text = raw.decode("utf-8", "surrogateescape")
            if not starts_line:
                text = text.partition("\n")[2]  # the window began inside another request's line
            complete = text[:text.rfind("\n") + 1]  # a line still being written is another request's
            requests: list[LogLine] = []
            for line in complete.splitlines():
                parsed = parse_access_line(line)
                if parsed is not None:
                    requests.append(parsed)
                elif not _HEADER.match(line):
                    # A line that is no request and no header: a request this reader cannot read
                    # (a key holding a newline splits its line), so whose it was is not known.
                    io.unverified(f"the store {s.name}'s access log holds a line this runtime cannot read; "
                                  "what the call read and wrote there is not fully known")
            keys: set[str] = set()
            if requests:
                try:
                    keys = self._unit_keys(s)
                except Exception as exc:  # noqa: BLE001
                    io.unverified(f"the store {s.name}'s catalog did not name this unit's keys "
                                  f"({type(exc).__name__}); what the call read and wrote there is not known")
                    continue
                if not keys:
                    io.unverified(f"the store {s.name}'s catalog names no key for this unit; what the call "
                                  "read and wrote there is not known")
                    continue
            windows[s.name] = _Window(requests, keys)
        return windows

    def _record_writes(self, ticket: _Ticket, io: CallIO, declared: frozenset[str],
                       windows: dict[str, _Window]) -> None:
        matched: set[tuple[str, str, str]] = set()  # (store, bucket, key) of attributed changes
        for area, before in ticket.listings.items():
            client = self.client(area.store)
            try:
                after = _listing(client, area)
            except Exception as exc:  # noqa: BLE001
                io.unverified(f"the store {area.store} did not list bucket {area.bucket} after the call "
                              f"({type(exc).__name__}); what the call wrote there is not known")
                continue
            has_log = self.stores[area.store].adapters.get("reads") == "access_log"
            window = windows.get(area.store)
            if has_log and window is None:
                io.unverified(f"the store {area.store}'s log was not read; what the call wrote in bucket "
                              f"{area.bucket} is not attributed")
                continue
            writes = [p for p in (window.requests if window else []) if p.bucket == area.bucket
                      and p.op in WRITE_OPS and _maybe_succeeded(p)]
            own = [p for p in writes if p.requester in window.keys] if window else []
            # How many versions each key's successful PUT-like requests of this unit made, and whether
            # anyone else wrote that key in the window (then a version cannot be told apart).
            puts = {}
            for p in own:
                if p.key and p.op != "s3_DeleteObject" and p.op != "s3_DeleteObjects":
                    puts[p.key] = puts.get(p.key, 0) + 1
            deletes = {p.key for p in own if p.op == "s3_DeleteObject" and p.key}
            bulk = any(p.op == "s3_DeleteObjects" or p.key is None for p in own)
            others = {p.key for p in writes if window and p.requester not in window.keys}

            def mine(key: str, removal: bool) -> bool:
                """A change is this call's: any change without a log; with one, a new version where
                this unit put that key, a delete marker or removal where it deleted it (a bulk
                delete names no key: its bucket's markers and removals)."""
                if not has_log:
                    return True
                if removal:
                    return key in deletes or bulk
                return key in puts

            new: dict[str, list[tuple[str, _Version]]] = {}
            for (key, version), info in after.items():  # newest first for a key (the listing's order)
                if (key, version) not in before:
                    new.setdefault(key, []).append((version, info))
            latest_before = {k: v for (k, v), info in before.items() if info.latest and not info.marker}
            for key, versions in new.items():
                markers = [(v, i) for v, i in versions if i.marker]
                data = [(v, i) for v, i in versions if not i.marker]
                take = [(v, i) for v, i in markers if mine(key, True)]
                if data and mine(key, False):
                    if has_log and (key in others or len(data) > puts.get(key, 0)):
                        matched.add((area.store, area.bucket, key))
                        io.unverified(f"{area.store} {area.bucket}/{key} gained {len(data)} versions in the call's "
                                      "window, written also by another key: which one this call wrote is "
                                      "not known")
                    else:
                        take += data
                if not take:
                    continue
                matched.add((area.store, area.bucket, key))
                previous = latest_before.get(key)
                for version, info in reversed([vi for vi in versions if vi in take]):  # oldest first
                    obj = dict(store=area.store, bucket=area.bucket, key=key, version_id=version, size=info.size,
                               delete_marker=info.marker, declared=area.store in declared, overwrote=previous)
                    if not info.marker:
                        try:
                            obj["sha256"] = _hash_version(client, area.store, area.bucket, key, version,
                                                          info.size, info.last_modified)
                        except Exception as exc:  # noqa: BLE001
                            io.unverified(f"{area.store} {area.bucket}/{key} version {version} could not be "
                                          f"hashed ({type(exc).__name__}: {exc})")
                    written = StoreObject(**obj)
                    io.writes.append(written)
                    previous = None if info.marker else version
                    if not written.declared:
                        io.flags.add("undeclared_write")
                        io.notes.append(f"wrote {area.store} {area.bucket}/{key}, a store this tool's manifest "
                                        "does not name in writes")
            for (key, version), info in before.items():
                if (key, version) not in after and mine(key, True):
                    matched.add((area.store, area.bucket, key))
                    io.flags.add("undeclared_write")
                    io.notes.append(f"removed {area.store} {area.bucket}/{key} version {version} from the "
                                    "store's history")
        listed = {(a.store, a.bucket) for a in ticket.listings}
        for store, window in windows.items():
            for p in window.requests:
                if p.requester not in window.keys or p.op not in WRITE_OPS or not _maybe_succeeded(p):
                    continue
                if not p.parsed:
                    io.flags.add("undeclared_write")
                    io.notes.append(f"the store {store}'s log shows a {p.op} by this unit whose object could "
                                    "not be read from the log")
                elif p.key is None:
                    if (store, p.bucket) not in listed:
                        io.flags.add("undeclared_write")
                        io.notes.append(f"the store {store}'s log shows a {p.op} in bucket {p.bucket}, "
                                        "outside the area this unit writes")
                elif (store, p.bucket, p.key) not in matched and (store, p.bucket) not in listed:
                    io.flags.add("undeclared_write")
                    io.notes.append(f"the store {store}'s log shows a {p.op} of {p.bucket}/{p.key} by this "
                                    "unit, outside the area this unit writes")
                elif (store, p.bucket, p.key) not in matched and p.op in ("s3_PutObject", "s3_CopyObject",
                                                                          "s3_CompleteMultipartUpload"):
                    io.unverified(f"the store {store}'s log shows a {p.op} of {p.bucket}/{p.key} by this unit "
                                  "that left no new version in the listing")

    def _record_reads(self, io: CallIO, windows: dict[str, _Window], given: list[Mapping[str, str]]) -> None:
        pinned: list[tuple[str, str, str, str]] = []
        for store, window in windows.items():
            for p in window.requests:
                if p.requester not in window.keys or p.op not in READ_OPS or not _maybe_succeeded(p):
                    continue
                if not p.parsed or p.key is None or p.version_id is None:
                    what = f"{p.bucket}/{p.key}" if p.key else f"an object in bucket {p.bucket}"
                    io.unverified(f"read {store} {what} without a version the log names: the version it read "
                                  "is not known")
                    if p.bucket in self.stores[store].synthetic_buckets:
                        io.flags.add("synthetic")
                    continue
                pinned.append((store, p.bucket, p.key, p.version_id))
        for record in given:  # a store with no read record: the call's pinned records are its reads
            name = record.get("store")
            if name in self.stores and self.stores[name].adapters.get("reads") != "access_log":
                pinned.append((name, record["bucket"], record["key"], record["version_id"]))
        for store, bucket, key, version in dict.fromkeys(pinned):
            if bucket in self.stores[store].synthetic_buckets:
                io.flags.add("synthetic")
            client = self.client(store)
            try:
                head = client.head_object(Bucket=bucket, Key=key, VersionId=version)
                size, modified = int(head.get("ContentLength", 0)), str(head["LastModified"])
                sha = _hash_version(client, store, bucket, key, version, size, modified)
            except Exception as exc:  # noqa: BLE001
                io.unverified(f"{store} {bucket}/{key} version {version}, which the call read, could not be "
                              f"hashed ({type(exc).__name__}: {exc})")
                continue
            obj = StoreObject(store=store, bucket=bucket, key=key, version_id=version, size=size, sha256=sha)
            io.reads.append(InputFile(object=obj, sha256=sha, basis="object"))
            try:
                carried, product = self._lineage_flags(store, (bucket, key, version))
            except Exception as exc:  # noqa: BLE001 — what the read carries is not known: fail closed
                io.unverified(f"the store {store}'s catalog did not say what {bucket}/{key} version {version} "
                              f"descends from ({type(exc).__name__})")
                continue
            if carried:
                io.carried.update(carried)
                io.notes.append(f"read {store} {bucket}/{key} version {version} of product {(product or '?')[:12]}, "
                                f"which carries {', '.join(sorted(carried))} from the run that made it or "
                                "from what it was made from")


def writable_area(store_names: Iterable[str], stores: Sequence[Any] | None = None) -> list[Area]:
    """Every bucket of each named store, whole: the area a unit whose tools declare those stores in
    ``writes`` may write (until a key policy narrows it to prefixes, item S4)."""
    from auto_research.store import config

    declared = {s.name: s for s in (stores if stores is not None else config.stores())}
    area = []
    for name in dict.fromkeys(store_names):
        if name not in declared:
            raise ValueError(f"no store named {name!r} is declared in `stores:`")
        area += [Area(name, bucket) for bucket in declared[name].buckets]
    return area


def store_location_problem(arguments: Mapping[str, Any], names: Iterable[str], cwd: Path | None,
                           stores: Sequence[Any] | None = None) -> str | None:
    """Why a call is refused for a ``path`` argument (``names``) resolving inside a store's folders
    (a local store's root, state or socket folder; coordinator ruling a), in words the model acts
    on; ``None`` otherwise. A store is reached through ``record`` arguments, never by its folder.
    With no ``stores:`` block configured there is no store folder."""
    if stores is None:
        from auto_research.store import config

        try:
            stores = config.stores()
        except ValueError:
            return None
    folders = [(s.name, Path(os.path.realpath(p))) for s in stores
               for p in (getattr(s, "root", None), getattr(s, "state_dir", None), getattr(s, "socket_dir", None))
               if p is not None]
    if not folders:
        return None
    for name in names:
        value = arguments.get(name)
        for item in (value if isinstance(value, list) else [value]):
            if not isinstance(item, (str, os.PathLike)) or not str(item).strip():
                continue
            path = Path(item)
            if not path.is_absolute() and cwd is not None:
                path = Path(cwd) / path
            try:
                resolved = Path(os.path.realpath(path))
            except (OSError, ValueError):
                continue
            for store, folder in folders:
                if resolved == folder or resolved.is_relative_to(folder):
                    return (f"This call was not run: argument {name} names {item}, inside the folders of the "
                            f"store {store}. A store's objects reach a tool as records "
                            '({"store", "bucket", "key"}); use a tool that takes a record argument, or ask '
                            "in your report's tool_request for one")
    return None


__all__ = ["Area", "CallIO", "LogLine", "READ_OPS", "StoreRecordError", "StoreRecords", "WRITE_OPS",
           "parse_access_line", "store_location_problem", "writable_area"]
