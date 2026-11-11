"""Start and stop a local store's servers: versitygw's IAM service, its S3 gateway, and Postgres.

```
state_dir/                      socket_dir/ (short, local, no jail mounts it)
  iam/        IAM users, keys     iam.sock          IAM private endpoint (gateway -> IAM)
  versions/   older versions      .s.PGSQL.<port>   Postgres
  postgres/   the catalog cluster
  logs/       access.log, iam.log, gateway.log, postgres.log
  server.lock held by every server process for its life: one server set per state_dir
  launch.lock serialises start, stop and status
  servers.json host, pids and settings of the running set
```
All three listen on 127.0.0.1 only. The servers outlive the process that started them (their own
session), so concurrent runs share them: ``start`` is idempotent, ``stop`` is explicit. A server
process is known by what it holds (``server.lock`` among its open files), never by a pid alone. A
second start against a state_dir whose servers run with other settings, or on another host, is
refused. A remote store has nothing to start.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import contextmanager, suppress
from pathlib import Path

from auto_research.store.config import LocalStore, RemoteStore

#: Seconds a server gets to answer after it was started, or to exit after SIGTERM.
READY_TIMEOUT_S = 30.0
#: The gateway's health path (GET returns 200 when it serves).
HEALTH_PATH = "/health"

_RECORD = "servers.json"
_LOCK = "server.lock"


def _binary(name: str) -> str:
    """The executable ``name`` from PATH, else beside this interpreter (the conda environment)."""
    found = shutil.which(name) or shutil.which(name, path=str(Path(sys.executable).parent))
    if not found:
        raise RuntimeError(f"{name} is not installed; install it into this conda environment (conda-forge)")
    return found


def _settings(store: LocalStore) -> dict:
    """What a running set must share with a store to be that store's: a changed admin key id or a
    changed folder, port or database means another set."""
    return {"root": str(store.root), "socket_dir": str(store.socket_dir), "ports": store.ports.model_dump(),
            "database": store.catalog.database, "admin_user": store.catalog.admin_user,
            "admin_key_id": store.admin_s3()[0]}


def _read_record(store: LocalStore) -> dict | None:
    try:
        return json.loads((store.state_dir / _RECORD).read_text())
    except (OSError, ValueError):
        return None


def _write_record(store: LocalStore, record: dict) -> None:
    path = store.state_dir / _RECORD
    temporary = path.with_name(f".{_RECORD}.{os.getpid()}")
    temporary.write_text(json.dumps(record, indent=1))
    os.replace(temporary, path)


@contextmanager
def _locked(store: LocalStore):
    with open(store.state_dir / "launch.lock", "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _try_server_lock(store: LocalStore):
    """The server lock's open file, locked, or None when a server process holds it."""
    handle = open(store.state_dir / _LOCK, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            return None
        raise
    return handle


def holders(store: LocalStore) -> list[int]:
    """The pids of this user's processes on this host holding the store's ``server.lock`` open: its
    server processes (each inherited the lock), whatever the record says."""
    target = str((store.state_dir / _LOCK).resolve())
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            if any(os.readlink(fd) == target for fd in (entry / "fd").iterdir()):
                found.append(int(entry.name))
        except OSError:
            continue
    return sorted(found)


def _postgres_pid(store: LocalStore) -> int | None:
    try:
        return int((store.state_dir / "postgres" / "postmaster.pid").read_text().split("\n", 1)[0])
    except (OSError, ValueError):
        return None


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def down(store: LocalStore) -> list[str]:
    """What does not answer now: the IAM service, the gateway, Postgres."""
    out = []
    if not (store.iam_socket.exists() and _port_open(store.ports.iam)):
        out.append(f"IAM service (127.0.0.1:{store.ports.iam}, {store.iam_socket})")
    if not _http_ok(store.s3_endpoint + HEALTH_PATH):
        out.append(f"S3 gateway ({store.s3_endpoint}{HEALTH_PATH})")
    if not (store.catalog_socket.exists() and _port_open(store.ports.catalog)):
        out.append(f"Postgres (127.0.0.1:{store.ports.catalog}, {store.catalog_socket})")
    return out


def _tail(path: Path, lines: int = 15) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no log)"


def _refuses_user_xattrs(path: Path) -> OSError | None:
    """Set and remove a ``user.`` xattr on ``path`` (a folder's own inode, no file is made); the error
    when its file system refuses one. Listing proves nothing: NFS lists ``system.`` ones regardless."""
    try:
        os.setxattr(path, "user.ar_probe", b"1")
        os.removexattr(path, "user.ar_probe")
    except OSError as exc:
        return exc
    return None


def _mount_of(path: Path) -> tuple[str, str]:
    """``(file system type, super options)`` of the mount holding ``path`` (``/proc/self/mountinfo``)."""
    best, found = "", ("", "")
    resolved = str(path.resolve())
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        point = fields[4].replace("\\040", " ")
        tail = fields[fields.index("-") + 1:]
        if (resolved == point or resolved.startswith(point.rstrip("/") + "/")) and len(point) >= len(best):
            best, found = point, (tail[0], tail[2] if len(tail) > 2 else "")
    return found


def _check_placement(store: LocalStore) -> None:
    """``root`` is a folder and ``state_dir`` (made here) another, each on a file system that takes
    user xattrs (versitygw keeps object and version metadata in them); on NFS, ``state_dir``'s locks
    reach the server (else two hosts could each run a set); ``socket_dir`` is ours alone, outside the
    temp root."""
    if not store.root.is_dir():
        raise RuntimeError(f"store {store.name}: root {store.root} is not a folder")
    if not store.state_dir.parent.is_dir():
        raise RuntimeError(f"store {store.name}: state_dir's parent {store.state_dir.parent} is not a folder")
    store.state_dir.mkdir(mode=0o700, exist_ok=True)
    for sub in ("versions", "iam", "logs"):
        (store.state_dir / sub).mkdir(mode=0o700, exist_ok=True)
    for field in ("root", "state_dir"):
        refused = _refuses_user_xattrs(getattr(store, field))
        if refused:
            raise RuntimeError(f"store {store.name}: {field} {getattr(store, field)}: its file system refuses "
                               f"user xattrs ({refused}); versitygw keeps its metadata in them, so place it on "
                               "one that takes them")
    kind, options = _mount_of(store.state_dir)
    local_locks = [o for o in options.split(",") if o in ("nolock", "local_lock=flock", "local_lock=all")]
    if kind.startswith("nfs") and local_locks:
        raise RuntimeError(f"store {store.name}: state_dir {store.state_dir} is on NFS mounted with "
                           f"{local_locks[0]}: its server lock stays on this host, so another host could start "
                           "a second set; mount it with local_lock=none or place state_dir elsewhere")
    temp_roots = {Path("/tmp").resolve(), Path(tempfile.gettempdir()).resolve()}
    if any(store.socket_dir.resolve().is_relative_to(t) for t in temp_roots):
        raise RuntimeError(f"store {store.name}: socket_dir {store.socket_dir} lies in the temp root, which a "
                           "jail may bind; choose a short folder on local disk outside it")
    store.socket_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = store.socket_dir.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError(f"store {store.name}: socket_dir {store.socket_dir} must be yours alone (mode 0700); "
                           f"it is uid {info.st_uid}, mode {oct(info.st_mode & 0o777)}")


def _env(store: LocalStore, *, root_key: bool) -> dict[str, str]:
    """A server's environment: PATH, HOME and a C locale, plus the root key for versitygw only;
    nothing else of this process's (its API keys stay here)."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/"), "LC_ALL": "C"}
    if root_key:
        key, secret = store.admin_s3()
        env.update(ROOT_ACCESS_KEY_ID=key, ROOT_SECRET_ACCESS_KEY=secret)
    return env


def _spawn(argv: list[str], log: Path, env: dict, lock) -> subprocess.Popen:
    with open(log, "ab") as out:
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env,
                                start_new_session=True, pass_fds=(lock.fileno(),), cwd="/")


def _wait(store: LocalStore, what: str, ok, proc: subprocess.Popen | None, log: Path) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_S
    while not ok():
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"store {store.name}: the {what} exited ({proc.returncode}); {log}:\n{_tail(log)}")
        if time.monotonic() > deadline:
            raise RuntimeError(f"store {store.name}: the {what} did not answer within {READY_TIMEOUT_S:.0f} s; "
                               f"{log}:\n{_tail(log)}")
        time.sleep(0.05)


def _start_postgres(store: LocalStore, lock) -> None:
    data = store.state_dir / "postgres"
    log = store.state_dir / "logs" / "postgres.log"
    env = _env(store, root_key=False)
    if not (data / "PG_VERSION").exists():
        # The password reaches initdb on its stdin: never on a command line, never in a file.
        done = subprocess.run([_binary("initdb"), "-D", str(data), "-U", store.catalog.admin_user,
                               "--pwfile=/dev/stdin", "--auth=scram-sha-256", "-E", "UTF8", "--no-locale",
                               "--no-instructions"], env=env, capture_output=True, text=True,
                              input=store.admin_env.values()["catalog_password"] + "\n")
        if done.returncode:
            raise RuntimeError(f"store {store.name}: initdb {data} failed: {done.stderr.strip()[-800:]}")
    options = f"-p {store.ports.catalog} -h 127.0.0.1 -k {shlex.quote(str(store.socket_dir))}"
    with open(log, "ab") as out:
        done = subprocess.run([_binary("pg_ctl"), "-D", str(data), "-l", str(log), "-w", "-t",
                               str(int(READY_TIMEOUT_S)), "-o", options, "start"], env=env, stdout=out,
                              stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(lock.fileno(),))
    if done.returncode:
        raise RuntimeError(f"store {store.name}: Postgres did not start (pg_ctl {done.returncode}); "
                           f"{log}:\n{_tail(log)}")
    _create_database(store)
    _apply_schema(store)


def _create_database(store: LocalStore) -> None:
    import psycopg
    from psycopg import sql

    with psycopg.connect(store.admin_dsn(dbname="postgres"), autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (store.catalog.database,)).fetchone():
            return
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(store.catalog.database)))


def _apply_schema(store: LocalStore) -> None:
    """Install the catalog schema, or confirm it is at this code's version (``catalog.apply_schema``,
    idempotent), as the admin superuser. Run on a thread of its own: ``start`` may be called from
    inside an event loop (the preflight's)."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from auto_research.store.catalog import apply_schema

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, apply_schema(store.admin_dsn())).result()


def _stop_postgres(store: LocalStore) -> None:
    pid = _postgres_pid(store)
    if pid is None or pid not in holders(store):
        return
    done = subprocess.run([_binary("pg_ctl"), "-D", str(store.state_dir / "postgres"), "-m", "fast", "-w", "-t",
                           str(int(READY_TIMEOUT_S)), "stop"], capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(f"store {store.name}: Postgres did not stop: {done.stderr.strip()[-500:]}")


def _stop_holders(store: LocalStore) -> None:
    """Stop every server process of the store on this host: Postgres through pg_ctl, the rest by
    SIGTERM; raise when one outlives ``READY_TIMEOUT_S``."""
    _stop_postgres(store)
    pids = holders(store)
    for pid in pids:
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + READY_TIMEOUT_S
    while set(pids) & set(holders(store)):
        if time.monotonic() > deadline:
            raise RuntimeError(f"store {store.name}: server pid(s) {sorted(set(pids) & set(holders(store)))} "
                               "did not exit on SIGTERM")
        time.sleep(0.05)
    store.iam_socket.unlink(missing_ok=True)
    (store.state_dir / _RECORD).unlink(missing_ok=True)


def _running(store: LocalStore) -> dict | None:
    """The running set's record when it is this store's and every server answers (waiting up to
    ``READY_TIMEOUT_S`` for a busy one); None after stopping a set left without a record or with a
    server process gone. Raises for a set of another host or other settings, or one whose processes
    all live but do not answer."""
    record = _read_record(store)
    local = holders(store)
    if not local:
        host = (record or {}).get("host", "unknown")
        raise RuntimeError(f"store {store.name}: {store.state_dir} is in use by servers on host {host} "
                           f"(pids {(record or {}).get('pids', '?')}); stop them there before starting here")
    if record is not None and record.get("settings") != _settings(store):
        raise RuntimeError(f"store {store.name}: {store.state_dir} is in use by servers started with other "
                           f"settings ({record.get('settings')}); stop them (launch.stop) before starting with "
                           "these")
    gone = [name for name, pid in ((record or {}).get("pids") or {}).items() if pid not in local]
    if record is None or gone:
        _stop_holders(store)  # a start that crashed before its record, or a set missing a server
        return None
    deadline = time.monotonic() + READY_TIMEOUT_S
    while (unanswered := down(store)):
        if time.monotonic() > deadline:
            raise RuntimeError(f"store {store.name}: its servers run (pids {local}) but do not answer: "
                               f"{', '.join(unanswered)}; see {store.state_dir / 'logs'}")
        time.sleep(0.2)
    return record


def _confirm_own(store: LocalStore, servers: dict[str, subprocess.Popen]) -> None:
    """Raise unless the servers answering on the store's ports are the ones just started: a port free
    when chosen can be bound by another process first (seen under concurrent starts), whose server then
    answers the health check; it refuses this store's root key."""
    for what, proc in servers.items():
        if proc.poll() is not None:
            raise RuntimeError(f"store {store.name}: the {what} exited as it started ({proc.returncode}); its "
                               "port may have been taken")
    try:
        store.admin_client("s3").list_buckets()
        store.admin_client("iam").list_users()
    except Exception as exc:  # noqa: BLE001 - another server on the port, or none
        raise RuntimeError(f"store {store.name}: the servers on its ports {store.ports.s3}/{store.ports.iam} did not "
                           f"accept its root key or did not answer ({type(exc).__name__}: {exc}); another server "
                           "may hold a port: choose free ports in stores[].ports") from exc


def _start_local(store: LocalStore) -> dict:
    _check_placement(store)
    logs = store.state_dir / "logs"
    with _locked(store):
        lock = _try_server_lock(store)
        if lock is None:
            record = _running(store)
            if record is not None:
                return record
            lock = _try_server_lock(store)
            if lock is None:
                raise RuntimeError(f"store {store.name}: a process still holds {store.state_dir / _LOCK} "
                                   f"after its servers stopped (pids {holders(store)})")
        with lock:
            busy = [f"{name} {port}" for name, port in store.ports.model_dump().items() if _port_open(port)]
            if busy:
                raise RuntimeError(f"store {store.name}: 127.0.0.1 port(s) {', '.join(busy)} already answer; "
                                   "another server holds them, so choose free ports in stores[].ports")
            env = _env(store, root_key=True)
            started: list[subprocess.Popen] = []
            try:
                store.iam_socket.unlink(missing_ok=True)
                iam = _spawn([_binary("versitygw"), "--port", f"127.0.0.1:{store.ports.iam}", "iam", "--quiet",
                              "--dir", str(store.state_dir / "iam"), "--private-ports", str(store.iam_socket),
                              "--private-socket-perm", "0600"], logs / "iam.log", env, lock)
                started.append(iam)
                _wait(store, "IAM service", lambda: store.iam_socket.exists() and _port_open(store.ports.iam),
                      iam, logs / "iam.log")
                gateway = _spawn([_binary("versitygw"), "--port", f"127.0.0.1:{store.ports.s3}", "--quiet",
                                  "--health", HEALTH_PATH, "--access-log", str(store.access_log),
                                  "--iam-standalone-endpoint", str(store.iam_socket),
                                  "--iam-standalone-policy-cache-ttl", "0", "posix",
                                  "--versioning-dir", str(store.state_dir / "versions"), str(store.root)],
                                 logs / "gateway.log", env, lock)
                started.append(gateway)
                _wait(store, "S3 gateway", lambda: _http_ok(store.s3_endpoint + HEALTH_PATH), gateway,
                      logs / "gateway.log")
                _start_postgres(store, lock)
                _confirm_own(store, {"IAM service": iam, "S3 gateway": gateway})
            except BaseException:
                with suppress(Exception):
                    _stop_postgres(store)
                for proc in started:
                    proc.terminate()
                for proc in started:
                    try:
                        proc.wait(timeout=READY_TIMEOUT_S)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                raise
            record = {"host": socket.gethostname(), "settings": _settings(store), "started_at": time.time(),
                      "pids": {"iam": iam.pid, "gateway": gateway.pid, "postgres": _postgres_pid(store)}}
            _write_record(store, record)
            return record


def start(store: LocalStore | RemoteStore) -> dict | None:
    """Start a local store's servers, or return the running set's record when they already serve with
    these settings (idempotent; a busy server is waited for, never restarted). A set left by a crashed
    start, or missing a server process, is stopped and started afresh. Refuses a state_dir whose servers
    run on another host or with other settings, and ports something else answers on. A remote store:
    nothing to start (None)."""
    if isinstance(store, RemoteStore):
        return None
    return _start_local(store)


def stop(store: LocalStore | RemoteStore) -> bool:
    """Stop a local store's servers on this host; whether any was running here. Refuses a set that
    runs on another host."""
    if isinstance(store, RemoteStore) or not store.state_dir.is_dir():
        return False
    with _locked(store):
        if holders(store):
            _stop_holders(store)
            return True
        lock = _try_server_lock(store)
        if lock is None:
            record = _read_record(store) or {}
            raise RuntimeError(f"store {store.name}: its servers run on host {record.get('host', 'unknown')}; "
                               "stop them there")
        lock.close()
        (store.state_dir / _RECORD).unlink(missing_ok=True)
        return False


def status(store: LocalStore | RemoteStore) -> dict | None:
    """The running set's record while a server process holds the state_dir, else None."""
    if isinstance(store, RemoteStore) or not (store.state_dir / _LOCK).exists():
        return None
    with _locked(store):
        lock = _try_server_lock(store)
        if lock is not None:
            lock.close()
            return None
        return _read_record(store)
