"""The CI pipeline's throwaway store (Phase 3 step 0b, S7): a fresh local store for one test run.

The gate's test run is jailed with no network and never reaches the real store. When the tested
toolkit declares a store tool (a manifest ``writes`` or ``reads``, or a ``record`` argument), a test run gets
a store of its own, started on this host for that run and stopped after it:

```
gate process (host)                                    the CI test run (bwrap --unshare-net)
 throwaway(mirror)                                      sees socket_dir only (bound read-only)
   launch.start: versitygw IAM + gateway + Postgres, 127.0.0.1, temporary folders
   buckets: the mirrored store's, versioning on; the catalog schema applied
   keys.issue: ci_producer (producer), ci_reader (reader)
   socket_dir/
     s3.sock          host relay ──► gateway 127.0.0.1:<s3>  ◄── relay 127.0.0.1:<s3> in the jail
                                                                  (pytest plugin ``ar_store``)
     .s.PGSQL.<port>  Postgres                               ◄── PGHOST=<socket_dir>
     calls.sock       StoreRecords.bracket per watched call  ◄── the return recorder (begin, end)
     seed.sock        one data set file uploaded as a product ◄── TOOLKIT_STORE_SEED (a toolkit test)
```
The test process reaches the store with the producer's keys in the standard variables (``AWS_*``,
``PG*``), the reader's under :data:`READER_PREFIX`, and the store's name in :data:`ENV_STORE`. The
servers' own state (IAM, versions, the access log, the catalog cluster, the admin key) stays on the
host, outside the jail. What each watched call wrote is read from the store's records by the
runtime's own :class:`~auto_research.store.records.StoreRecords` and handed to the return check.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import socket
import re
import signal
import sys
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import PrivateAttr

from auto_research.store.config import AdminEnv

#: Where every throwaway store's folders are made, in ``ar-ci-<uid>/`` (one per user): ``<id>/`` its work
#: folder (root, state and its life lock), ``<id>-s`` its own socket folder, ``<id>-j`` the one the jail
#: binds. A fixed local place outside the temp root (``launch`` refuses a socket folder there), the same
#: for every process of the user whatever its ``TMPDIR``, outside every folder a jail binds otherwise.
PARENT_ROOT = Path("/var/tmp")
#: A store's work folder holds this file locked for the store's life; the kernel lets go when its
#: process ends, however it ends, so a free lock marks a leftover (:func:`sweep_leftovers`).
LIFE_LOCK = "in-use"
#: Seconds a socket folder with no work folder, or a half-made ``.new-…`` folder, is left alone (a store
#: being made or being removed).
SWEEP_GRACE_S = 120
_ID = re.compile(r"^[0-9a-f]{12}$")
#: The units the run's two keys are issued to.
PRODUCER, READER = "ci_producer", "ci_reader"
#: The variable naming the store a CI test run has (unset: none): the name records give.
ENV_STORE = "TOOLKIT_STORE"
#: The unix socket a toolkit test asks for a data set's file through (see :meth:`CIStore._seed`).
ENV_SEED = "TOOLKIT_STORE_SEED"
#: The prefix of the reader's variables (``AWS_ACCESS_KEY_ID``, ``AWS_SECRET_ACCESS_KEY``, ``PGUSER``,
#: ``PGPASSWORD``); endpoint, region, socket folder, port and database are the producer's.
READER_PREFIX = "TOOLKIT_STORE_READER_"
#: The gate's own variables: the bracket socket the return recorder calls, the S3 relay socket.
ENV_CALLS, ENV_S3_SOCKET = "AR_STORE_CALLS", "AR_STORE_S3_SOCKET"
#: Every store tool of the tree (``{key: [module, fn]}``): the recorder brackets each call, watched or
#: not, so the final sweep can tell a tool's write from one made outside every call.
ENV_TOOLS = "AR_STORE_TOOLS"
#: The brief's validation records as this run's store holds them (a JSON list of ``{store, bucket, key,
#: version_id}``, in the brief's order): the same keys, the versions the copy made.
ENV_VALIDATION = "TOOLKIT_STORE_VALIDATION"
#: The pytest plugin that relays the store's S3 endpoint into the jail's own loopback.
PLUGIN_NAME = "ar_store"
#: A seeded file's product: its kind, the tool named in its recipe.
SEED_KIND, SEED_TOOL = "test_data", "auto_research.ci.seed"
#: The most bytes one test run may seed (a single PUT each; a data set is read file by file).
SEED_MAX_BYTES = 4 << 30
#: The longest seed request line (synthetic bytes travel base64-encoded in it).
SEED_LINE_MAX = 64 << 20
#: Ports are taken free and released before the servers bind them: tries before giving up.
_START_ATTEMPTS = 3
_CHUNK = 1 << 20


class StoreTools(NamedTuple):
    """The store tools of a toolkit tree (:func:`store_tools`): the packages declaring one, every
    tool's ``writes``, and each store tool's ``(module, fn)``, which the recorder brackets."""

    packages: frozenset[str]
    writes: dict[str, list[str]]
    entries: dict[str, tuple[str, str]]


def store_tools(root: Path) -> StoreTools:
    """The store tools of the toolkit tree at ``root``: a tool whose manifest entry declares
    ``writes`` or ``reads``, or an argument of kind ``record``. Manifests are read as data
    (``registry.manifest_literal``)."""
    from auto_research.provenance.inputs import declared_kinds
    from auto_research.sandbox.registry import manifest_literal
    from auto_research.toolkit_contract import toolkit

    packages, writes, entries = set(), {}, {}
    for package in toolkit().packages(root):
        path = Path(root) / package / "skills" / "__init__.py"
        try:
            manifest = manifest_literal(path.read_text(encoding="utf-8")) if path.is_file() else None
        except (OSError, UnicodeDecodeError):
            manifest = None
        for key, spec in (manifest or {}).items():
            if not isinstance(spec, dict):
                continue
            declared = spec.get("writes")
            writes[str(key)] = [str(w) for w in declared] if isinstance(declared, list) else []
            schema = spec.get("args_schema")
            kinds = declared_kinds(schema) if isinstance(schema, dict) else {}
            if writes[str(key)] or spec.get("reads") or "record" in kinds.values():
                packages.add(package)
                if isinstance(spec.get("module"), str) and isinstance(spec.get("fn"), str):
                    entries[str(key)] = (spec["module"], spec["fn"])
    return StoreTools(frozenset(packages), writes, entries)


def undeclared_store_names(root: Path, packages, declared) -> list[str]:
    """One line per manifest ``writes``/``reads`` entry of ``packages`` (under ``root``) naming a store
    ``declared`` (store names) does not hold: what the structural job and the launch check refuse."""
    from auto_research.sandbox.registry import manifest_literal

    known, out = set(declared), []
    for package in packages:
        path = Path(root) / package / "skills" / "__init__.py"
        try:
            manifest = manifest_literal(path.read_text(encoding="utf-8")) if path.is_file() else None
        except (OSError, UnicodeDecodeError):
            manifest = None
        for key, spec in (manifest or {}).items():
            for field in ("writes", "reads"):
                names = spec.get(field) if isinstance(spec, dict) else None
                for name in names if isinstance(names, list) else []:
                    if isinstance(name, str) and name not in known:
                        out.append(f"{key}: {field} names the store {name!r}, which the configuration does not "
                                   f"declare; name one of {', '.join(sorted(known)) or '(none: declare a store)'}")
    return out


def mirror_of(config: Mapping[str, Any]):
    """The store a CI test run mirrors: the configured default store (``db``), else the first one
    declared; ``None`` when ``config`` declares no store."""
    from auto_research.store import config as store_config

    try:
        declared = store_config.stores(dict(config))
    except ValueError:
        return None
    return next((s for s in declared if s.name == store_config.DEFAULT_STORE), declared[0])


def ci_parent() -> Path:
    """This user's folder of throwaway stores: ``PARENT_ROOT/ar-ci-<uid>``, mode 0700, refused unless it
    is a plain folder of this user's alone."""
    from auto_research.sandbox.jail import _owned_private_dir

    return _owned_private_dir(PARENT_ROOT / f"ar-ci-{os.getuid()}")


def _start_time(pid: int) -> int | None:
    """``pid``'s start time in clock ticks (``/proc/<pid>/stat`` field 22), ``None`` when it is gone,
    exited (a zombie, collected here when it is this process's child) or another user's: with the pid,
    it names one process, never a later one given the same pid."""
    try:
        if os.stat(f"/proc/{pid}").st_uid != os.getuid():
            return None
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rindex(")") + 2:].split()
        if fields[0] in ("Z", "X"):
            with contextlib.suppress(ChildProcessError, OSError):
                os.waitpid(pid, os.WNOHANG)
            return None
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _end(processes: Mapping[int, int | None], wait_s: float = 15.0) -> list[int]:
    """End each ``{pid: start time}`` that is still that process: SIGTERM, then SIGKILL after
    ``wait_s``; the pids it signalled."""
    alive = [pid for pid, started in processes.items() if started is not None and _start_time(pid) == started]
    for pid in alive:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + wait_s
    while any(_start_time(pid) == processes[pid] for pid in alive) and time.monotonic() < deadline:
        time.sleep(0.1)
    for pid in alive:
        if _start_time(pid) == processes[pid]:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
    return alive


def _lock_free(folder: Path) -> bool:
    """Whether ``folder``'s store has ended: its life lock is free. A store's work folder gets its name
    only once its lock is held (:meth:`CIStore._declare`), so a named folder without a held lock is a
    leftover."""
    import fcntl

    try:
        fd = os.open(folder / LIFE_LOCK, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


def _server_holders(parent: Path) -> dict[int, tuple[str, bool, int]]:
    """``{pid: (store id, folder removed, start time)}`` of this user's processes holding a store's
    ``server.lock`` under ``parent`` (every server ``launch`` starts inherits it), also one whose folder
    was removed: the servers, never a process that only names a store's path, nor one holding a store's
    life lock (a gate process starting its store holds both). The start time is read before and after,
    so a pid reused meanwhile is left out."""
    root = re.escape(str(parent.resolve()))  # /proc shows resolved paths
    held = re.compile(rf"^{root}/([0-9a-f]{{12}})/state/server\.lock(?: \(deleted\))?$")
    life = re.compile(rf"^{root}/(?:[0-9a-f]{{12}}|\.new-[^/]+)/{re.escape(LIFE_LOCK)}(?: \(deleted\))?$")
    found = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        pid = int(entry.name)
        started = _start_time(pid)
        if started is None:
            continue
        links = []
        try:
            fds = list((entry / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            with contextlib.suppress(OSError):  # a file closed during the scan
                links.append(os.readlink(fd))
        match = next((m for link in links if (m := held.match(link))), None)
        if match is None or any(life.match(link) for link in links):
            continue
        if _start_time(pid) == started:
            found[pid] = (match.group(1), match.group(0).endswith(" (deleted)"), started)
    return found


def leftover_folders() -> list[Path]:
    """The folders this user's throwaway stores left behind, read only: an ended store's work and socket
    folders (its life lock free), socket folders with no work folder and a half-made ``.new-…`` folder
    whose lock is free, both past :data:`SWEEP_GRACE_S`. What ``ci-sweep`` removes."""
    try:
        parent = ci_parent()
        entries = {e.name for e in parent.iterdir()}
    except (OSError, RuntimeError):
        return []
    ids = {name for name in entries if _ID.match(name) and (parent / name).is_dir()}
    found: list[Path] = []
    for name in sorted(n for n in entries if n.startswith(".new-")):  # a store that died while being made
        making = parent / name
        try:
            if making.is_dir() and not making.is_symlink() and _lock_free(making) \
                    and time.time() - making.lstat().st_mtime > SWEEP_GRACE_S:
                found.append(making)
        except FileNotFoundError:  # named or removed meanwhile
            continue
    ended = {i for i in ids if _lock_free(parent / i)}
    orphans = {name[:12] for name in entries if _ID.match(name[:12]) and name[12:] in ("-s", "-j")} - ids
    for ident in sorted(ended | orphans):
        targets = [parent / ident, parent / f"{ident}-s", parent / f"{ident}-j"]
        try:
            if ident not in ended and any(time.time() - t.lstat().st_mtime <= SWEEP_GRACE_S
                                          for t in targets if os.path.lexists(t)):
                continue  # a store being made (its socket folders come after its work folder) or removed
        except FileNotFoundError:
            continue
        found += [t for t in targets if os.path.lexists(t)]
    return found


def sweep_leftovers(remove_folders: bool = True) -> list[str]:
    """Clean up what this user's throwaway stores left behind (a gate process killed outright, a folder
    removed under a running store): every server of a store whose life lock is free or whose work folder
    is gone is ended, then those stores' folders are removed (:func:`leftover_folders`). A store in use
    (its lock held) is never touched, nor any process but a store's servers. What it did, one line each.
    Every throwaway start runs it with ``remove_folders=False`` (ending leftover servers only: a removal
    can be gigabytes); ``python -m auto_research.store ci-sweep`` runs it whole, the one way to clean the
    folders, and the launch's preflight item ``store_ci_leftovers`` says when there are some."""
    parent, done = ci_parent(), []
    # Each server judged now, by its own store's lock: a store named meanwhile (another process's)
    # holds its lock, so its servers are left alone.
    servers = _server_holders(parent)
    done += [f"ended leftover server pid {pid}" for pid in _end(
        {pid: started for pid, (ident, removed, started) in servers.items()
         if removed or not (parent / ident).is_dir() or _lock_free(parent / ident)})]
    if not remove_folders:
        return done
    for target in leftover_folders():
        try:
            if target.is_symlink():
                target.unlink()
            elif target.exists():
                shutil.rmtree(target, ignore_errors=True)
            else:
                continue
        except FileNotFoundError:  # removed meanwhile (another sweep)
            continue
        done.append(f"removed {target}")
    return done


def check_ci_leftovers() -> None:
    """Preflight item ``store_ci_leftovers``: a notice (``PreflightWarning``, never a failure) naming how many
    folders the CI pipeline's throwaway stores left behind and their size, and the command that removes
    them."""
    from auto_research.preflight import PreflightWarning

    found = leftover_folders()
    if not found:
        return
    size = 0
    for folder in found:
        for root, _dirs, files in os.walk(folder):
            for name in files:
                with contextlib.suppress(OSError):
                    size += os.lstat(os.path.join(root, name)).st_size
    raise PreflightWarning(f"{len(found)} folder(s) left by the CI pipeline's throwaway stores in {ci_parent()} "
                           f"({size / 2**20:.1f} MiB); run `python -m auto_research.store ci-sweep` to remove them")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _free_ports(n: int) -> list[int]:
    sockets = [socket.socket() for _ in range(n)]
    try:
        for s in sockets:
            s.bind(("127.0.0.1", 0))
        return [s.getsockname()[1] for s in sockets]
    finally:
        for s in sockets:
            s.close()


def _members(client, record: Mapping[str, str]) -> list[tuple[str, str]]:
    """The object versions a validation record brings: itself, and for a recipe file every object it
    lists (same bucket). Raises ``ValueError`` for a recipe file that does not read as one."""
    from auto_research.store.reconcile import RECIPE_FILE, RECIPE_FILE_MAX, RecipeFile

    members = [(record["key"], record["version_id"])]
    if record["key"].rsplit("/", 1)[-1] == RECIPE_FILE:
        body = client.get_object(Bucket=record["bucket"], Key=record["key"],
                                 VersionId=record["version_id"])["Body"].read(RECIPE_FILE_MAX + 1)
        listed = RecipeFile.model_validate_json(body)
        members += [(o.key, o.version_id) for o in listed.objects]
    return members


def validation_problems(mirror, records) -> tuple[list[str], list[str]]:
    """Why the brief's validation ``records`` cannot reach the CI run, one line each, as ``(missing,
    unreachable)``: missing — a record of a store other than the mirrored one, a bucket it lacks, or an
    object version (or one its recipe file lists) the real store does not hold, the brief's fault;
    unreachable — the real store did not answer, a fault outside the change. Read with the real
    store's admin key, outside every jail."""
    problems, unreachable, client = [], [], None
    for record in records:
        r = dict(record)
        where = f"{r['store']} {r['bucket']}/{r['key']} version {r['version_id']}"
        if mirror is None or r["store"] != mirror.name:
            problems.append(f"{where}: the CI pipeline mirrors the store "
                            f"{mirror.name if mirror is not None else '(none is configured)'} only")
            continue
        if r["bucket"] not in mirror.buckets:
            problems.append(f"{where}: store {mirror.name} has no bucket {r['bucket']}")
            continue
        try:
            client = client or mirror.admin_client("s3")
            for key, version in _members(client, r):
                client.head_object(Bucket=r["bucket"], Key=key, VersionId=version)
        except ValueError as exc:
            problems.append(f"{where}: does not read as a recipe file ({str(exc).splitlines()[0]})")
        except Exception as exc:  # noqa: BLE001 — named to the lead and the coder, with the store's answer
            status = (getattr(exc, "response", None) or {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status in (400, 404, 405):
                problems.append(f"{where}: the store holds no such object version")
            else:
                unreachable.append(f"{where}: the store did not answer ({type(exc).__name__}: {exc})")
    return problems, unreachable


def _check_names(store: str, kind: Any, subject: Any, files: Mapping[str, Any]) -> None:
    """Refuse, before anything is claimed, a seed whose kind, subject or file names the store would not
    keep as written: each a plain path of parts (no empty, ``.`` or ``..`` part, no surrounding space or
    control character), no file named ``recipe.json``, no file under another one, keys within 1024 bytes."""
    import unicodedata

    def plain(part: str) -> bool:
        return (bool(part) and part not in (".", "..") and part == part.strip()
                and not any(unicodedata.category(c).startswith("C") for c in part))

    if not (isinstance(kind, str) and isinstance(subject, str) and plain(kind) and plain(subject)
            and "/" not in kind and "/" not in subject):
        raise SeedRefused("name the product's kind and subject, each one plain word with no '/'")
    for name in files:
        parts = name.split("/") if isinstance(name, str) else [""]
        if not all(plain(p) for p in parts) or parts[-1] == "recipe.json":
            raise SeedRefused(f"name each file as plain path parts joined by '/', and none recipe.json ({name!r} "
                              "is not)")
        if any(other != name and other.startswith(name + "/") for other in files):
            raise SeedRefused(f"name files that are no folder of another ({name!r} holds a file below it)")
        if len(f"{kind}/{subject}/xxxxxxxx/{name}".encode()) > 1024:
            raise SeedRefused(f"name each file in under 1000 bytes ({name[:40]!r}… is longer)")


class _HeldSecrets(AdminEnv):
    """A throwaway store's admin secrets, held by the object itself: never in this process's
    environment, which an author harness or a jail started meanwhile could inherit."""

    _held: dict[str, str] = PrivateAttr(default_factory=dict)

    def values(self) -> dict[str, str]:
        return dict(self._held)


class ValidationRefused(RuntimeError):
    """The brief's validation data cannot be copied for a reason of the brief's own (too large, a
    recipe the run's catalog refuses): the lead's to fix, never a fault outside the change."""


class SeedRefused(ValueError):
    """A seed request this run cannot serve (its message is the test's)."""


class CIStore:
    """One throwaway store for one CI test run (:func:`throwaway`). ``env``: the test process's
    variables; ``socket_dir``: the folder the jail binds read-only; ``calls``: what the store recorded
    of each bracketed call (``{call, tool, flags, notes, writes}`` or ``{call, tool, error}``)."""

    def __init__(self, mirror, *, data_sets: Mapping[str, Path] | None = None, validation=(),
                 writes: Mapping[str, list[str]] | None = None, cache: Path | None = None,
                 tools: Mapping[str, tuple[str, str]] | None = None) -> None:
        self.mirror = mirror
        self.data_sets = {name: Path(folder) for name, folder in (data_sets or {}).items()}
        self.writes = dict(writes or {})
        self.validation = [dict(r) for r in validation]
        self.cache = Path(cache) if cache is not None else None
        self.tools = dict(tools or {})
        self._made: set[tuple[str, str, str]] = set()  # (bucket, key, version) seeded or copied in
        self.run = f"ci-{secrets.token_hex(4)}"
        self.calls: list[dict[str, Any]] = []
        self.env: dict[str, str] = {}
        self.store = None
        self._dirs: list[Path] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._servers: list[asyncio.AbstractServer] = []
        self._seed_lock = threading.Lock()
        self._seeded_bytes = 0
        self.records = None
        self._jail_dir: Path | None = None
        self._life = None  # the open, locked LIFE_LOCK file
        self._pids: dict[int, int] = {}  # the store's server processes (pid: start time)

    @property
    def socket_dir(self) -> Path:
        """The folder the jail binds: the relay, bracket and seed sockets and a link to Postgres's
        socket, apart from the store's own socket folder (its IAM socket stays out of the jail)."""
        return self._jail_dir

    # -- start and stop ----------------------------------------------------------------------------

    def start(self) -> CIStore:
        from auto_research.store import launch

        with contextlib.suppress(Exception):  # best effort: a leftover never keeps a store from starting
            sweep_leftovers(remove_folders=False)  # its servers only; `ci-sweep` removes folders
        for attempt in range(_START_ATTEMPTS):
            self.store, self._pids = self._declare(), {}
            try:
                record = launch.start(self.store)
                self._pids = {pid: _start_time(pid) for pid in (record.get("pids") or {}).values() if pid}
                break
            except BaseException as exc:
                # A port taken between choosing it and binding it shows as a server that exits or
                # does not answer: tried again on new ports, the last failure raised.
                with contextlib.suppress(Exception):
                    launch.stop(self.store)
                _end(self._pids)
                self._remove_folders()
                if attempt == _START_ATTEMPTS - 1 or not isinstance(exc, RuntimeError):
                    raise
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name=f"ci-store-{self.run}", daemon=True)
        self._thread.start()
        self._call(self._serve())
        if self.validation:
            self.env[ENV_VALIDATION] = json.dumps(self._copy_validation())
        return self

    def _declare(self):
        """A local store in fresh temporary folders on free ports, named and bucketed like the mirror;
        its admin secrets are fresh and held by the store object alone (:class:`_HeldSecrets`)."""
        from auto_research.store.config import LocalStore

        import fcntl

        ident, parent = secrets.token_hex(6), ci_parent()
        # Made under a name no sweep reads, locked, then named: a named folder's lock is always held.
        making = Path(tempfile.mkdtemp(prefix=".new-", dir=parent))
        self._dirs = [making]
        self._life = open(making / LIFE_LOCK, "w")
        fcntl.flock(self._life, fcntl.LOCK_EX)
        os.rename(making, work := parent / ident)
        self._dirs = [work]
        for suffix in ("-s", "-j"):
            (folder := parent / f"{ident}{suffix}").mkdir(mode=0o700)
            self._dirs.append(folder)
        sockets, self._jail_dir = self._dirs[1], self._dirs[2]
        (work / "root").mkdir()
        admin = _HeldSecrets(s3_access_key="AR_CI_S3_KEY", s3_secret_key="AR_CI_S3_SECRET",
                             catalog_password="AR_CI_PG_PASSWORD")
        admin._held = {"s3_access_key": f"ci{secrets.token_hex(8)}", "s3_secret_key": secrets.token_urlsafe(24),
                       "catalog_password": secrets.token_urlsafe(24)}
        s3, iam, catalog = _free_ports(3)
        return LocalStore(name=self.mirror.name, kind="local", root=work / "root", state_dir=work / "state",
                          socket_dir=sockets, ports={"s3": s3, "iam": iam, "catalog": catalog},
                          buckets=self.mirror.buckets, synthetic_buckets=self.mirror.synthetic_buckets,
                          admin_env=admin)

    async def _serve(self) -> None:
        from auto_research.store import keys
        from auto_research.store.records import StoreRecords, writable_area

        client = self.store.admin_client("s3")
        for bucket in self.store.buckets:
            await asyncio.to_thread(client.create_bucket, Bucket=bucket)
            await asyncio.to_thread(client.put_bucket_versioning, Bucket=bucket,
                                    VersioningConfiguration={"Status": "Enabled"})
        producer = await keys.issue(self.store, PRODUCER, "producer", self.run)
        reader = await keys.issue(self.store, READER, "reader", self.run)
        self.records = StoreRecords(agent=producer.principal, run=self.run, stores=[self.store],
                                    writable=writable_area([self.store.name], stores=[self.store]))
        where = self._jail_dir
        # A hard link reaches the same socket from the jail's folder (connect resolves the inode).
        os.link(self.store.catalog_socket, where / self.store.catalog_socket.name)
        self._servers = [
            await asyncio.start_unix_server(self._relay, path=str(where / "s3.sock")),
            await asyncio.start_unix_server(self._on_call, path=str(where / "calls.sock")),
            await asyncio.start_unix_server(self._on_seed, path=str(where / "seed.sock"), limit=SEED_LINE_MAX),
        ]
        reader_env = reader.env()
        self.env = {
            **producer.env(), "PGHOST": str(where), ENV_STORE: self.store.name, ENV_SEED: str(where / "seed.sock"),
            **{READER_PREFIX + name: reader_env[name]
               for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "PGUSER", "PGPASSWORD")},
            ENV_CALLS: str(where / "calls.sock"), ENV_S3_SOCKET: str(where / "s3.sock"),
            ENV_TOOLS: json.dumps({key: list(entry) for key, entry in self.tools.items()}),
        }

    def _call(self, coro, timeout: float | None = None):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def stop(self) -> None:
        """Stop the servers and remove every folder the store used; idempotent."""
        from auto_research.store import launch

        try:
            if self._loop is not None:
                with contextlib.suppress(Exception):
                    self._call(self._close(), timeout=30)
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join(timeout=30)
                if not self._loop.is_running():  # a stalled loop thread dies with the process (daemon)
                    self._loop.close()
                self._loop = None
        finally:
            try:
                if self.store is not None:
                    with contextlib.suppress(Exception):  # a folder removed under the store: by pid below
                        launch.stop(self.store)
                    _end(self._pids)  # what is still running of its servers (their folder gone, say)
                    self._pids = {}
            finally:
                self._remove_folders()

    async def _close(self) -> None:
        for server in self._servers:
            server.close()
        # A connection still open (a client that died mid-call) ends with the loop, not after it.
        for task in [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]:
            task.cancel()
        for server in self._servers:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 5)

    def _remove_folders(self) -> None:
        for folder in reversed(self._dirs):  # the work folder (and its life lock) last
            shutil.rmtree(folder, ignore_errors=True)
        self._dirs = []
        if self._life is not None:
            self._life.close()
            self._life = None

    def _copy_validation(self) -> list[dict[str, str]]:
        """Copy each validation record's object version (and, for a recipe file, every object it lists)
        from the real store into this one, at the same bucket and key with the same metadata; a
        recipe file becomes a committed, verified product here, its listing naming the new versions.
        Returns the records as this store holds them, in the brief's order. Bytes come from
        :attr:`cache` when an earlier run of the same gate fetched them. Counted against
        :data:`SEED_MAX_BYTES`."""
        import psycopg
        from psycopg.types.json import Jsonb

        from auto_research.store import reconcile as rc

        real, mine = self.mirror.admin_client("s3"), self.records.client(self.store.name)
        copies: dict[tuple[str, str, str], dict[str, Any]] = {}  # (bucket, key, real version) -> this store's

        def fetch(bucket: str, key: str, version: str) -> tuple[Path, dict]:
            head = real.head_object(Bucket=bucket, Key=key, VersionId=version)
            if self._seeded_bytes + int(head["ContentLength"]) > SEED_MAX_BYTES:
                raise ValidationRefused(f"the validation records pass {SEED_MAX_BYTES >> 30} GiB; name fewer")
            self._seeded_bytes += int(head["ContentLength"])
            folder = self.cache or Path(self._dirs[0])
            path = folder / hashlib.sha256(f"{bucket}\0{key}\0{version}".encode()).hexdigest()
            if not path.is_file():
                partial = path.with_suffix(".part")
                with open(partial, "wb") as handle:
                    real.download_fileobj(bucket, key, handle, ExtraArgs={"VersionId": version})
                os.replace(partial, path)
            return path, head

        def put(bucket: str, key: str, body, meta: dict) -> dict[str, Any]:
            done = mine.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch="*", Metadata=meta)
            self._made.add((bucket, key, done["VersionId"]))
            return {"store": self.store.name, "bucket": bucket, "key": key, "version_id": done["VersionId"]}

        for record in self.validation:
            bucket, where = record["bucket"], (record["bucket"], record["key"], record["version_id"])
            if where in copies:
                continue
            members = _members(real, record)
            listed_copies = []
            for key, version in members[1:]:  # a recipe file's objects
                if (bucket, key, version) not in copies:
                    path, head = fetch(bucket, key, version)
                    with open(path, "rb") as handle:
                        copies[(bucket, key, version)] = {
                            **put(bucket, key, handle, head.get("Metadata") or {}),
                            "sha256": _sha256(path), "size": path.stat().st_size}
                listed_copies.append(copies[(bucket, key, version)])
            path, head = fetch(*where)
            meta = dict(head.get("Metadata") or {})
            if len(members) == 1:
                with open(path, "rb") as handle:
                    copies[where] = put(bucket, record["key"], handle, meta)
                continue
            listed = rc.RecipeFile.model_validate_json(path.read_bytes())
            listed.objects = [rc.RecipeObject(key=o.key, version_id=c["version_id"], sha256=o.sha256, size=o.size)
                              for o, c in zip(listed.objects, listed_copies)]
            rewritten = listed.model_dump_json().encode()
            meta[rc.META_SHA256] = hashlib.sha256(rewritten).hexdigest()
            marker = {**put(bucket, record["key"], rewritten, meta), "sha256": meta[rc.META_SHA256],
                      "size": len(rewritten)}
            with psycopg.connect(self.store.admin_dsn(), autocommit=True) as conn:
                answer = conn.execute("SELECT catalog.claim(%s)", (Jsonb(listed.recipe),)).fetchone()[0]
                if answer["status"] != "claimed":
                    raise ValidationRefused(f"validation record {bucket}/{record['key']}: its recipe could not be "
                                            f"recorded in the CI store ({answer['status']}); name one record per recipe")
                conn.execute("SELECT catalog.commit(%s, %s, %s, %s)",
                             (answer["recipe_hash"], Jsonb([*listed_copies, marker]),
                              meta.get(rc.META_TOOLKIT_COMMIT) or "ci-validation",
                              meta.get(rc.META_ENV_FINGERPRINT) or "ci-validation"))
                conn.execute("SELECT catalog.verify(%s)", (answer["recipe_hash"],))
            copies[where] = marker
        return [{k: copies[(r["bucket"], r["key"], r["version_id"])][k] for k in ("store", "bucket", "key", "version_id")}
                for r in self.validation]

    # -- the S3 relay ------------------------------------------------------------------------------

    async def _relay(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One connection from the jail, carried byte for byte to the gateway on 127.0.0.1."""
        try:
            up_reader, up_writer = await asyncio.open_connection("127.0.0.1", self.store.ports.s3)
        except OSError:
            writer.close()
            return

        async def pump(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
            try:
                while data := await source.read(_CHUNK):
                    sink.write(data)
                    await sink.drain()
                if sink.can_write_eof():
                    sink.write_eof()
            except (ConnectionError, OSError):
                pass

        try:
            await asyncio.gather(pump(reader, up_writer), pump(up_reader, writer))
        finally:
            for w in (up_writer, writer):
                w.close()

    # -- the bracket around each watched call ------------------------------------------------------

    async def _on_call(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One watched call: ``{"call", "tool"}`` opens the bracket (answered ``{"ok": true}`` once the
        store is listed); the next line, or the connection's end, closes it. What the store recorded
        is kept in :attr:`calls`; the second answer follows."""
        from auto_research.store.records import StoreRecordError

        try:
            begin = json.loads(await reader.readline() or b"{}")
            call, tool = str(begin.get("call", "")), str(begin.get("tool", ""))
            try:
                async with self.records.bracket(declared=self.writes.get(tool, ())) as io:
                    writer.write(b'{"ok": true}\n')
                    await writer.drain()
                    await reader.readline()
            except StoreRecordError as exc:
                self.calls.append({"call": call, "tool": tool, "error": str(exc)})
                writer.write((json.dumps({"ok": False, "error": str(exc)}) + "\n").encode())
            else:
                self.calls.append(await asyncio.to_thread(self._facts, call, tool, io))
                writer.write(b'{"ok": true}\n')
            await writer.drain()
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            writer.close()

    def _facts(self, call: str, tool: str, io) -> dict[str, Any]:
        """What the return check judges of one call's writes: each version as recorded, its metadata,
        and for a recipe file its text and the catalog's hash of the recipe it holds."""
        import psycopg
        from psycopg.types.json import Jsonb

        from auto_research.store.reconcile import RECIPE_FILE, RECIPE_FILE_MAX

        client = self.records.client(self.store.name)
        writes = []
        for written in io.writes:
            entry = written.model_dump(mode="json")
            if not written.delete_marker:
                where = dict(Bucket=written.bucket, Key=written.key, VersionId=written.version_id)
                try:
                    entry["metadata"] = client.head_object(**where).get("Metadata") or {}
                    body = (client.get_object(**where)["Body"].read(RECIPE_FILE_MAX + 1)
                            if written.key.rsplit("/", 1)[-1] == RECIPE_FILE else None)
                except Exception as exc:  # noqa: BLE001 — named to the coder as a write that could not be read
                    entry["read_error"] = f"{type(exc).__name__}: {exc}"
                    body = None
                if body is not None:
                    entry["recipe_file"] = body.decode("utf-8", "replace") if len(body) <= RECIPE_FILE_MAX else None
                    try:
                        parsed = json.loads(body) if entry["recipe_file"] else None
                    except ValueError:
                        parsed = None  # judged as no recipe file by the return check
                    recipe = parsed.get("recipe") if isinstance(parsed, dict) else None
                    if isinstance(recipe, dict):
                        try:
                            with psycopg.connect(self.store.admin_dsn(), autocommit=True) as conn:
                                entry["recipe_hash_computed"] = conn.execute(
                                    "SELECT catalog.recipe_hash(%s)", (Jsonb(recipe),)).fetchone()[0]
                        except psycopg.Error as exc:
                            entry["read_error"] = f"the catalog did not hash its recipe ({type(exc).__name__}: {exc})"
            writes.append(entry)
        facts = {"call": call, "tool": tool, "flags": sorted(io.flags), "carried": sorted(io.carried),
                 "notes": list(io.notes), "writes": writes, "producer": self.records.agent}
        # Ruling A: each product's catalog row (state, owner, recipe inputs), and the product each pinned
        # read and each recipe input belongs to (an object row), so the check can tell whether the
        # recipes name what the call read.
        reads = [r.object for r in io.reads if r.object is not None]
        hashes = sorted({(w.get("metadata") or {}).get("recipe-hash") for w in writes} - {None})
        try:
            with psycopg.connect(self.store.admin_dsn(), autocommit=True) as conn:
                def product_of(o: Mapping[str, Any]) -> str | None:
                    row = conn.execute("SELECT recipe_hash FROM catalog.object WHERE store = %s AND bucket = %s "
                                       "AND key = %s AND version_id = %s",
                                       (o.get("store"), o.get("bucket"), o.get("key"), o.get("version_id"))).fetchone()
                    return row[0] if row else None

                facts["reads"] = [{**(o := r.model_dump(include={"store", "bucket", "key", "version_id"})),
                                   "product": product_of(o)} for r in reads]
                products = {}
                for recipe_hash in hashes:
                    row = conn.execute("SELECT state, owner, recipe FROM catalog.product WHERE recipe_hash = %s",
                                       (recipe_hash,)).fetchone()
                    products[recipe_hash] = None if row is None else {
                        "state": row[0], "owner": row[1],
                        "inputs": [{**i, "product": product_of(i)} for i in (row[2] or {}).get("inputs", [])
                                   if isinstance(i, dict)]}
                facts["products"] = products
        except psycopg.Error as exc:
            facts["catalog_error"] = f"{type(exc).__name__}: {exc}"
        return facts

    def sweep(self) -> list[dict[str, str]]:
        """Ruling E, after the run: every version in the store that no bracketed call wrote and the
        pipeline did not seed or copy in (a test's own write, a tool's leftover thread or process
        writing after its call returned)."""
        client = self.records.client(self.store.name)
        attributed = set(self._made) | {(w["bucket"], w["key"], w["version_id"])
                                        for c in self.calls for w in c.get("writes", [])}
        outside = []
        for bucket in self.store.buckets:
            for page in client.get_paginator("list_object_versions").paginate(Bucket=bucket):
                for v in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]:
                    if (bucket, v["Key"], v["VersionId"]) not in attributed:
                        outside.append({"bucket": bucket, "key": v["Key"], "version_id": v["VersionId"]})
        return outside

    # -- seeding a data set's file -----------------------------------------------------------------

    async def _on_seed(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                request = json.loads(await reader.readline() or b"{}")
                answer = await asyncio.to_thread(self._seed, request)
            except SeedRefused as exc:
                answer = {"error": str(exc)}
            except ValueError:  # past the line limit, or no JSON
                answer = {"error": f"send one JSON line of at most {SEED_LINE_MAX >> 20} MiB"}
            except Exception as exc:  # noqa: BLE001 — the test reads why its seed was not stored
                answer = {"error": f"the pipeline could not store the seed ({type(exc).__name__}: {exc})"}
            writer.write((json.dumps(answer) + "\n").encode())
            await writer.drain()
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            writer.close()

    def _seed(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """One seed request, answered ``{"record": ...}`` (or ``{"record", "objects"}``):

        * ``{"data": <data set>, "path": <file in its folder>, "bucket": <bucket>}`` — the file, as a
          product of kind :data:`SEED_KIND`, subject the data set; the record is the file's own;
        * ``{"data", "paths": [<file>, ...], "bucket"}`` — those files as one such product; the record
          is its ``recipe.json`` (a product's record), ``objects`` each file's;
        * ``{"kind", "subject", "bucket", "files": {<name>: <base64 bytes>}}`` — synthetic bytes a test
          stages (main's ruling 2), as a product of that kind and subject; answered as ``paths``.

        Each is stored with the admin key as a committed, verified product at ``<kind>/<subject>/<short
        hash>/...`` beside its ``recipe.json``, create-only with the five metadata entries, recorded as
        seeded (outside every call window; the sweep passes it). The same request again answers the same."""
        forms = ('send {"data": "<data set>", "path": "<file in its folder>", "bucket": "<bucket>"} (or "paths": '
                 '[<file>, ...]), or {"kind", "subject", "bucket", "files": {"<name>": "<base64>"}}')
        if not isinstance(request, Mapping):
            raise SeedRefused(forms)
        bucket = request.get("bucket")
        if not isinstance(bucket, str) or bucket not in self.store.buckets:
            raise SeedRefused(f"name a bucket of store {self.store.name}: {', '.join(self.store.buckets)}")
        if "files" in request:
            return self._seed_synthetic(request, bucket)
        name = request.get("data")
        rels = [request.get("path")] if "path" in request else request.get("paths")
        if not (isinstance(name, str) and name.strip() and isinstance(rels, list) and rels
                and all(isinstance(r, str) and r.strip() for r in rels)):
            raise SeedRefused(forms)
        if name not in self.data_sets:
            raise SeedRefused(f"name a data set this CI pipeline provides: {', '.join(sorted(self.data_sets)) or 'none'}")
        folder = self.data_sets[name].resolve()
        files: dict[str, Path] = {}
        for rel in rels:
            path = (folder / rel).resolve()
            if not path.is_relative_to(folder) or not path.is_file():
                raise SeedRefused(f"name a file inside data set {name!r} ({rel!r} is none)")
            files[path.relative_to(folder).as_posix()] = path
        _check_names(self.store.name, SEED_KIND, name, files)
        recipe = {"kind": SEED_KIND, "subject": name, "tool": SEED_TOOL, "algorithm_version": "1",
                  "settings": {"bucket": bucket, **({"path": next(iter(files))} if "path" in request
                                                     else {"paths": sorted(files)})}, "inputs": []}
        objects, marker = self._store_seeded(bucket, recipe, files)
        return {"record": objects[0]} if "path" in request else {"record": marker, "objects": objects}

    def _seed_synthetic(self, request: Mapping[str, Any], bucket: str) -> dict[str, Any]:
        import base64
        import binascii

        kind, subject, files = request.get("kind"), request.get("subject"), request.get("files")
        if not isinstance(files, dict) or not files or not all(isinstance(b, str) for b in files.values()):
            raise SeedRefused('send "files": {"<name inside the product>": "<base64 bytes>"}, at least one')
        _check_names(self.store.name, kind, subject, files)
        try:
            data = {name: base64.b64decode(body, validate=True) for name, body in files.items()}
        except (binascii.Error, ValueError):
            raise SeedRefused("encode each file's bytes with standard base64 (base64.b64encode)") from None
        recipe = {"kind": kind, "subject": subject, "tool": SEED_TOOL, "algorithm_version": "1",
                  "settings": {"bucket": bucket, "files": {n: hashlib.sha256(b).hexdigest() for n, b in data.items()}},
                  "inputs": []}
        # Synthetic bytes (main's ruling A): every read of them carries `synthetic`, in any bucket.
        objects, marker = self._store_seeded(bucket, recipe, data, flags=("synthetic",))
        return {"record": marker, "objects": objects}

    def _store_seeded(self, bucket: str, recipe: dict, files: Mapping[str, Path | bytes],
                      flags: tuple[str, ...] = ()) -> tuple[list[dict], dict]:
        """Store ``files`` (a path or bytes each) as the committed, verified product of ``recipe``, its
        catalog row stamped with ``flags`` (``catalog.stamp_flags``); ``(each file's record, the recipe
        file's record)``. Counted against :data:`SEED_MAX_BYTES`."""
        import io

        import psycopg
        from psycopg.types.json import Jsonb

        from auto_research.store import reconcile as rc

        def keep(o: Mapping[str, Any]) -> dict[str, str]:
            return {k: o[k] for k in ("store", "bucket", "key", "version_id")}

        with self._seed_lock, psycopg.connect(self.store.admin_dsn(), autocommit=True) as conn:
            answer = conn.execute("SELECT catalog.claim(%s)", (Jsonb(recipe),)).fetchone()[0]
            if answer["status"] == "reuse":
                prefix = f"{recipe['kind']}/{recipe['subject']}/{answer['recipe_hash'][:8]}"
                by_key = {o["key"]: o for o in answer["objects"]}
                return ([keep(by_key[f"{prefix}/{name}"]) for name in files],
                        keep(by_key[f"{prefix}/{rc.RECIPE_FILE}"]))
            if answer["status"] != "claimed":
                raise SeedRefused(f"the seed could not be stored ({answer['status']}: {answer.get('reason', '')})")
            size = sum(f.stat().st_size if isinstance(f, Path) else len(f) for f in files.values())
            if self._seeded_bytes + size > SEED_MAX_BYTES:
                conn.execute("SELECT catalog.fail(%s, %s)", (answer["recipe_hash"], "past the seed limit"))
                raise SeedRefused(f"the seed would take this run past {SEED_MAX_BYTES >> 30} GiB of seeded "
                                  "data; seed the few files a test reads")
            self._seeded_bytes += size
            recipe_hash = answer["recipe_hash"]
            prefix = f"{recipe['kind']}/{recipe['subject']}/{recipe_hash[:8]}"
            client = self.records.client(self.store.name)
            meta = {rc.META_RECIPE_HASH: recipe_hash, rc.META_PRODUCER: self.store.catalog.admin_user,
                    rc.META_TOOLKIT_COMMIT: "ci-seed", rc.META_ENV_FINGERPRINT: "ci-seed"}

            def put(key: str, body, sha: str, length: int) -> dict[str, Any]:
                done = client.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch="*",
                                         ChecksumAlgorithm="SHA256", Metadata={**meta, rc.META_SHA256: sha})
                self._made.add((bucket, key, done["VersionId"]))
                return {"store": self.store.name, "bucket": bucket, "key": key, "version_id": done["VersionId"],
                        "sha256": sha, "size": length}

            objects = []
            try:
                for name, source in files.items():
                    if isinstance(source, Path):
                        with open(source, "rb") as handle:
                            objects.append(put(f"{prefix}/{name}", handle, _sha256(source), source.stat().st_size))
                    else:
                        objects.append(put(f"{prefix}/{name}", io.BytesIO(source),
                                           hashlib.sha256(source).hexdigest(), len(source)))
                listed = rc.RecipeFile(recipe_hash=recipe_hash, generation=answer["generation"], recipe=recipe,
                                       objects=[{k: o[k] for k in ("key", "version_id", "sha256", "size")}
                                                for o in objects])
                body = listed.model_dump_json().encode()
                marker = put(f"{prefix}/{rc.RECIPE_FILE}", body, hashlib.sha256(body).hexdigest(), len(body))
                conn.execute("SELECT catalog.commit(%s, %s, %s, %s)",
                             (recipe_hash, Jsonb([*objects, marker]), "ci-seed", "ci-seed"))
                conn.execute("SELECT catalog.verify(%s)", (recipe_hash,))
                if flags:
                    conn.execute("SELECT catalog.stamp_flags(%s, %s, %s)", (recipe_hash, list(flags), "ci-seed"))
            except Exception as exc:  # noqa: BLE001 — the claim fails, so the same request may be tried again
                with contextlib.suppress(Exception):
                    conn.execute("SELECT catalog.fail(%s, %s)", (recipe_hash, f"seed not stored: {exc}"[:500]))
                raise SeedRefused(f"the store refused the seed ({type(exc).__name__}: {exc}); send it again, and "
                                  "when it repeats, name the files more plainly") from None
        return [keep(o) for o in objects], keep(marker)


@contextlib.contextmanager
def throwaway(mirror, *, data_sets: Mapping[str, Path] | None = None,
              writes: Mapping[str, list[str]] | None = None, validation=(),
              cache: Path | None = None, tools: Mapping[str, tuple[str, str]] | None = None) -> Iterator[CIStore]:
    """A started :class:`CIStore` mirroring ``mirror`` (its name, buckets and synthetic buckets),
    stopped and removed on exit. ``data_sets``: the folders a test may seed from (``{name: folder}``);
    ``writes``: each manifest key's declared stores, for the bracket; ``validation``: the brief's
    validation records, copied in from the real store before the run (:meth:`CIStore._copy_validation`);
    ``cache``: a folder (outside every jail) keeping the fetched bytes for the gate's later runs;
    ``tools``: every store tool of the tree (``StoreTools.entries``), each call bracketed."""
    store = CIStore(mirror, data_sets=data_sets, writes=writes, validation=validation, cache=cache, tools=tools)
    try:
        yield store.start()
    finally:
        store.stop()


#: The pytest plugin the gate loads into a test run that has a store: it listens on the store's S3
#: port on the jail's own loopback and carries each connection to the relay socket bound in.
PLUGIN_SOURCE = '''\
import os
import socket
import threading


def _pump(source, sink):
    try:
        while True:
            data = source.recv(1 << 16)
            if not data:
                break
            sink.sendall(data)
    except OSError:
        pass
    finally:
        try:
            sink.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _pair(client, path):
    try:
        up = socket.socket(socket.AF_UNIX)
        up.connect(path)
    except OSError:
        client.close()
        return
    back = threading.Thread(target=_pump, args=(up, client), daemon=True)
    back.start()
    _pump(client, up)
    back.join()
    client.close()
    up.close()


def _serve(listener, path):
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            return
        threading.Thread(target=_pair, args=(client, path), daemon=True).start()


def pytest_configure(config):
    path = os.environ.get("%(socket)s")
    endpoint = os.environ.get("AWS_ENDPOINT_URL_S3", "")
    if not path or ":" not in endpoint:
        return
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", int(endpoint.rsplit(":", 1)[1])))
    listener.listen(64)
    threading.Thread(target=_serve, args=(listener, path), daemon=True).start()
''' % {"socket": ENV_S3_SOCKET}


__all__ = ["CIStore", "LIFE_LOCK", "check_ci_leftovers", "leftover_folders", "PARENT_ROOT", "ValidationRefused", "ci_parent", "sweep_leftovers", "ENV_CALLS", "ENV_S3_SOCKET", "ENV_SEED", "ENV_STORE", "ENV_TOOLS", "ENV_VALIDATION",
           "StoreTools",
           "validation_problems", "PLUGIN_NAME", "PLUGIN_SOURCE",
           "PRODUCER", "READER", "READER_PREFIX", "SEED_KIND", "SEED_MAX_BYTES", "SEED_TOOL",
           "SeedRefused", "mirror_of", "store_tools", "throwaway"]
