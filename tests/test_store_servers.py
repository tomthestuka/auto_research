"""A local store's servers (``store.launch``) and the stores' launch preflight items (``store.health``),
on throwaway stores in temporary folders on local disk: real versitygw (+ IAM) and Postgres."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import tempfile
import time
from pathlib import Path

import pytest
import yaml

from auto_research.config_paths import CONFIG_DIR_ENV
from auto_research.preflight import Preflight, PreflightError, build_default_preflight
from auto_research.store import config as store_config
from auto_research.store import launch

pytestmark = pytest.mark.skipif(
    not all(shutil.which(b) for b in ("versitygw", "initdb", "pg_ctl")),
    reason="versitygw and Postgres (initdb, pg_ctl) are not installed in this environment")

SECRETS = {"T_S3_KEY": "testadmin", "T_S3_SECRET": "testsecret0123456789", "T_PG_PASSWORD": "pg secret"}
ADMIN = {"s3_access_key": "T_S3_KEY", "s3_secret_key": "T_S3_SECRET", "catalog_password": "T_PG_PASSWORD"}
#: Sockets need a short local folder outside the temp root (``launch`` refuses one inside it).
SOCKETS_PARENT = "/dev/shm"


@pytest.fixture(autouse=True)
def _no_dotenv(tmp_path, monkeypatch):
    """The admin variables come from the test's environment; the project's ``.env`` is never loaded."""
    monkeypatch.setattr("auto_research.env._ENV_FILE", tmp_path / "absent.env")


def _free_ports(n: int) -> list[int]:
    sockets = [socket.socket() for _ in range(n)]
    for s in sockets:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in sockets]
    for s in sockets:
        s.close()
    return ports


@pytest.fixture
def make_store(tmp_path, monkeypatch):
    """``make_store(**fields) -> LocalStore``: a store under ``tmp_path`` on free ports, declared in an
    ``orchestration.yaml`` this process reads (``AUTO_RESEARCH_CONFIG_DIR``); every store made is
    stopped afterwards."""
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    made, sockets_made = [], []
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv(CONFIG_DIR_ENV, str(config))
    store_config.reset()

    def make(extra_config: dict | None = None, **fields):
        s3, iam, catalog = _free_ports(3)
        sockets = fields.get("socket_dir")
        if not sockets:  # made here: removed at teardown, also when the store is refused or never started
            sockets = tempfile.mkdtemp(prefix="ars-", dir=SOCKETS_PARENT)
            sockets_made.append(sockets)
        (tmp_path / "root").mkdir(exist_ok=True)
        entry = {"name": "db", "kind": "local", "root": str(tmp_path / "root"), "state_dir": str(tmp_path / "state"),
                 "socket_dir": str(sockets), "ports": {"s3": s3, "iam": iam, "catalog": catalog},
                 "buckets": ["ixpe", "nicer"], "admin_env": ADMIN, **fields}
        (config / "orchestration.yaml").write_text(yaml.safe_dump(
            {"stores": [entry], "agent_fs_allow": [str(tmp_path / "work")], **(extra_config or {})}))
        store_config.reset()
        store = store_config.store()
        made.append(store)
        return store

    yield make
    try:
        for store in reversed(made):
            launch.stop(store)
    finally:
        for folder in {*sockets_made, *(str(store.socket_dir) for store in made)}:
            shutil.rmtree(folder, ignore_errors=True)
        store_config.reset()


def _check(name: str) -> str | None:
    """Run the production preflight item ``name`` as the launch does; its failure message, or None."""
    checks = Preflight()
    checks.register(name, dict(build_default_preflight()._checks)[name])
    try:
        asyncio.run(checks.run_checklist())
    except PreflightError as exc:
        return str(exc)
    return None


def _store_names() -> list[str]:
    return [n for n in build_default_preflight().names() if n.startswith("store_")]


# ── launch ────────────────────────────────────────────────────────────────────


def test_start_serves_and_a_second_start_is_the_same_servers(make_store):
    store = make_store()
    record = launch.start(store)
    assert launch.down(store) == []
    assert launch.status(store) == record
    assert store.admin_client("s3").list_buckets()["Buckets"] == []
    pg_version = store.state_dir / "postgres" / "PG_VERSION"
    stamp = pg_version.stat().st_mtime_ns

    assert launch.start(store) == record, "a second start started other servers"
    assert launch.stop(store) is True
    assert launch.status(store) is None and len(launch.down(store)) == 3
    assert launch.stop(store) is False

    again = launch.start(store)
    assert again["pids"] != record["pids"] and launch.down(store) == []
    assert pg_version.stat().st_mtime_ns == stamp, "the catalog cluster was initialised twice"
    import psycopg

    with psycopg.connect(store.admin_dsn()) as conn:
        assert conn.execute("SELECT current_database()").fetchone()[0] == "db"


def test_a_second_server_set_on_the_same_state_dir_is_refused(make_store):
    store = make_store()
    record = launch.start(store)
    other_ports = dict(zip(("s3", "iam", "catalog"), _free_ports(3)))
    other = store.model_copy(update={"ports": store.ports.model_copy(update=other_ports)})
    with pytest.raises(RuntimeError, match="in use by servers started with other settings"):
        launch.start(other)
    assert launch.down(store) == [] and launch.down(other) != []

    # A set on another host: the lock is held (over NFS) by no process of this host. This test process
    # stands in for the remote holder (``holders`` never counts the calling process).
    launch.stop(store)
    import fcntl

    path = store.state_dir / "servers.json"
    path.write_text(json.dumps({**record, "host": "elsewhere.example"}))
    with open(store.state_dir / "server.lock", "a+") as remote:
        fcntl.flock(remote, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="in use by servers on host elsewhere.example"):
            launch.start(store)
        with pytest.raises(RuntimeError, match="run on host elsewhere.example; stop them there"):
            launch.stop(store)
    assert launch.start(store)["host"] == socket.gethostname()


def test_ports_in_use_are_refused_and_nothing_is_left_running(make_store):
    store = make_store()
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", store.ports.s3))
        busy.listen()
        with pytest.raises(RuntimeError, match=f"port\\(s\\) s3 {store.ports.s3} already answer"):
            launch.start(store)
    assert launch.status(store) is None


def test_a_set_with_a_server_down_is_started_afresh(make_store):
    store = make_store()
    record = launch.start(store)
    os.kill(record["pids"]["gateway"], signal.SIGTERM)
    deadline = time.monotonic() + 10
    while record["pids"]["gateway"] in launch.holders(store) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert any("S3 gateway" in d for d in launch.down(store))
    fresh = launch.start(store)
    assert launch.down(store) == [] and fresh["pids"]["gateway"] != record["pids"]["gateway"]


def test_a_socket_dir_others_can_enter_is_refused(make_store):
    store = make_store()
    store.socket_dir.chmod(0o755)
    with pytest.raises(RuntimeError, match="must be yours alone"):
        launch.start(store)


def test_a_missing_root_is_refused(make_store, tmp_path):
    store = make_store(root=str(tmp_path / "absent"))
    with pytest.raises(RuntimeError, match="root .*absent is not a folder"):
        launch.start(store)


def test_a_folder_refusing_user_xattrs_is_refused(make_store, monkeypatch):
    """A file system without ``user.`` xattrs (NFS exports such as the runtime data dir's) cannot hold
    the gateway's metadata. The OS call is the fake: no local file system here refuses one."""
    import errno

    store = make_store()
    real = os.setxattr

    def refuse_in_state(path, *args, **kwargs):
        if Path(path).is_relative_to(store.state_dir):
            raise OSError(errno.EOPNOTSUPP, "Operation not supported", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "setxattr", refuse_in_state)
    with pytest.raises(RuntimeError, match="state_dir .* refuses user xattrs .*Operation not supported"):
        launch.start(store)
    assert launch.status(store) is None


def test_a_socket_dir_in_the_temp_root_is_refused(make_store):
    sockets = tempfile.mkdtemp(prefix="ars-", dir=tempfile.gettempdir())
    try:
        store = make_store(socket_dir=sockets)
        with pytest.raises(RuntimeError, match="lies in the temp root"):
            launch.start(store)
    finally:
        shutil.rmtree(sockets, ignore_errors=True)


def test_a_set_left_without_its_record_is_found_and_stopped(make_store):
    """A start killed between spawning its servers and writing their record leaves processes holding
    the lock: the next start finds them by the lock they hold and starts afresh; stop ends them."""
    store = make_store()
    first = launch.start(store)
    (store.state_dir / "servers.json").unlink()
    assert set(first["pids"].values()) <= set(launch.holders(store))
    fresh = launch.start(store)
    assert not set(first["pids"].values()) & set(launch.holders(store))
    assert launch.down(store) == [] and fresh["pids"] != first["pids"]
    (store.state_dir / "servers.json").unlink()
    assert launch.stop(store) is True and launch.holders(store) == [] and launch.status(store) is None


def test_a_busy_set_is_waited_for_and_never_restarted(make_store, monkeypatch):
    store = make_store()
    record = launch.start(store)
    monkeypatch.setattr(launch, "READY_TIMEOUT_S", 1.0)
    os.kill(record["pids"]["gateway"], signal.SIGSTOP)
    try:
        with pytest.raises(RuntimeError, match="its servers run .* but do not answer: S3 gateway"):
            launch.start(store)
        assert set(record["pids"].values()) <= set(launch.holders(store)), "a busy server was stopped"
    finally:
        os.kill(record["pids"]["gateway"], signal.SIGCONT)
    assert launch.start(store) == record


def test_no_secret_reaches_a_command_line_and_postgres_holds_no_s3_key(make_store):
    store = make_store()
    launch.start(store)
    for pid in launch.holders(store):
        argv = Path(f"/proc/{pid}/cmdline").read_bytes()
        environ = Path(f"/proc/{pid}/environ").read_bytes()
        for secret in SECRETS.values():
            assert secret.encode() not in argv, (pid, argv)
        if b"versitygw" not in argv:
            assert SECRETS["T_S3_SECRET"].encode() not in environ, f"the S3 root key reached {argv[:60]!r}"
        assert b"OPENAI_API_KEY" not in environ and b"T_PG_PASSWORD" not in environ
    assert not list(store.state_dir.glob(".pw*")), "a password file was left in state_dir"


# ── preflight ─────────────────────────────────────────────────────────────────


def _make_healthy(store) -> None:
    s3 = store.admin_client("s3")
    for bucket in store.buckets:
        s3.create_bucket(Bucket=bucket)
        s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})


def test_the_store_items_are_registered_in_order():
    assert _store_names() == ["store_config", "store_servers", "store_admin", "store_buckets", "store_catalog",
                              "store_integrity", "store_ci_leftovers"]


def test_the_preflight_items_pass_on_a_healthy_store(make_store):
    store = make_store()
    # The run's launch step starts a local store (ruling B); the preflight item only checks.
    assert "[store_servers] store db: not answering" in _check("store_servers")
    assert len(launch.down(store)) == 3, "store_servers started a store"
    launch.start(store)
    assert _check("store_servers") is None
    _make_healthy(store)
    checks = Preflight()
    for name, check in build_default_preflight()._checks:
        if name.startswith("store_"):
            checks.register(name, check)
    asyncio.run(checks.run_checklist())


def test_each_preflight_item_refuses_with_a_precise_message(make_store, monkeypatch, tmp_path):
    store = make_store()
    launch.start(store)
    s3 = store.admin_client("s3")
    s3.create_bucket(Bucket="ixpe")
    s3.put_bucket_versioning(Bucket="ixpe", VersioningConfiguration={"Status": "Enabled"})
    s3.create_bucket(Bucket="nicer")

    assert "[store_buckets] store db: bucket nicer versioning is never enabled; enable it" in _check("store_buckets")
    s3.put_bucket_versioning(Bucket="nicer", VersioningConfiguration={"Status": "Suspended"})
    assert "bucket nicer versioning is Suspended" in _check("store_buckets")
    s3.delete_bucket(Bucket="nicer")
    assert "bucket nicer is missing" in _check("store_buckets")

    # start installed the schema (S2's apply_schema); a catalog without it is refused.
    import psycopg

    with psycopg.connect(store.admin_dsn(), autocommit=True) as conn:
        conn.execute("DROP SCHEMA catalog CASCADE")
    assert "no catalog schema in this database" in _check("store_catalog")
    monkeypatch.setenv("T_PG_PASSWORD", "wrong")
    assert "the catalog refuses the admin role" in _check("store_catalog")
    monkeypatch.setenv("T_S3_SECRET", "wrong0123456789")
    assert "[store_admin] store db: the S3 endpoint refuses the admin key (T_S3_KEY)" in _check("store_admin")

    monkeypatch.delenv("T_S3_SECRET")
    assert "set T_S3_SECRET in .env" in _check("store_config")
    monkeypatch.setenv("T_S3_SECRET", SECRETS["T_S3_SECRET"])

    # A jail mount reaching the store's folders: the checks read the configuration file.
    mounted = make_store(extra_config={"agent_fs_readonly": [str(tmp_path)]},
                         socket_dir=str(store.socket_dir), ports=store.ports.model_dump())
    message = _check("store_config")
    assert f"root {mounted.root} meets the jail mount(s) {tmp_path}" in message
    assert "state_dir" in message and "socket_dir" not in message
    assert "not started, store_config refuses it" in _check("store_servers")

    make_store(extra_config={"agent_fs_allow": [str(store.socket_dir)]}, socket_dir=str(store.socket_dir),
               ports=store.ports.model_dump())
    assert f"socket_dir {store.socket_dir} meets the jail mount(s)" in _check("store_config")

    make_store(root=str(tmp_path / "absent"))
    assert "[store_servers] store db: not answering" in _check("store_servers")


def test_a_remote_store_that_does_not_answer_is_refused(tmp_path, monkeypatch):
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    (tmp_path / "config").mkdir()
    port = _free_ports(1)[0]
    (tmp_path / "config" / "orchestration.yaml").write_text(yaml.safe_dump({"stores": [{
        "name": "db", "kind": "remote", "endpoint": f"http://127.0.0.1:{port}", "buckets": ["ixpe"],
        "catalog_dsn": f"host=127.0.0.1 port={port} dbname=db user=u", "admin_env": ADMIN}]}))
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path / "config"))
    store_config.reset()
    try:
        assert f"the S3 endpoint http://127.0.0.1:{port} does not answer" in _check("store_servers")
        assert "the catalog does not answer" in _check("store_catalog")
    finally:
        store_config.reset()


def test_a_configuration_without_the_default_store_is_refused_never_silently_storeless(make_store, tmp_path):
    """S9 follow-up (ruling 6): units' keys and the lead's catalog query bind to the default store
    (``DEFAULT_STORE``), so a ``stores:`` block naming none is refused by ``store_config`` with the
    names it declares, and a run that bound it anyway issues no unit a key silently."""
    from auto_research.graph import runtime
    from auto_research.store.keys import RunKeys

    make_store()
    path = tmp_path / "config" / "orchestration.yaml"
    config = yaml.safe_load(path.read_text())
    config["stores"][0]["name"] = "other"
    path.write_text(yaml.safe_dump(config))
    store_config.reset()
    assert "no store is named 'db', the default store" in _check("store_config")
    assert "(declared: other)" in _check("store_config")
    runtime.bind_run_stores(config, "run-nodefault")
    try:
        with pytest.raises(RuntimeError, match="include no default store 'db'"):
            asyncio.run(runtime.issue_unit_keys(RunKeys("run-nodefault"), ["analyst_lead"], None))
    finally:
        runtime.release_run_stores("run-nodefault")



def test_a_start_whose_port_another_server_took_is_refused_and_leaves_that_server_alone(make_store, tmp_path,
                                                                                       monkeypatch):
    """S7 follow-up (main's ruling D): a port chosen free and bound by another process before this
    store's server binds it answers the health check with that process's server; `start` sees its own
    server exit on the taken port, stops what it started and raises, and the other store keeps serving."""
    first = make_store()
    launch.start(first)
    (tmp_path / "root2").mkdir()
    second = make_store(root=str(tmp_path / "root2"), state_dir=str(tmp_path / "state2"),
                        ports={"s3": first.ports.s3, **dict(zip(("iam", "catalog"), _free_ports(2)))})
    checks = {"n": 0}
    real_open = launch._port_open

    def taken_after_the_check(port):  # the S3 port, checked first, looks free; another server holds it
        checks["n"] += 1
        return False if checks["n"] == 1 else real_open(port)

    monkeypatch.setattr(launch, "_port_open", taken_after_the_check)
    with pytest.raises(RuntimeError, match="the S3 gateway exited as it started"):
        launch.start(second)
    assert launch.holders(second) == [], "the refused start left a server running"
    assert launch.down(first) == [], "the refused start harmed the store that held the port"



def test_a_store_whose_ports_answer_with_another_stores_servers_is_refused(make_store, tmp_path, monkeypatch):
    """`_confirm_own`'s second guard: servers on the store's ports that refuse its root key (another
    store's, whose own key differs) fail the start, naming the ports."""
    first = make_store()
    launch.start(first)
    monkeypatch.setenv("T_OTHER_KEY", "otheradmin")
    monkeypatch.setenv("T_OTHER_SECRET", "othersecret0123456789")
    (tmp_path / "root2").mkdir()
    other = make_store(root=str(tmp_path / "root2"), state_dir=str(tmp_path / "state2"),
                       ports=first.ports.model_dump(),
                       admin_env={**ADMIN, "s3_access_key": "T_OTHER_KEY", "s3_secret_key": "T_OTHER_SECRET"})
    with pytest.raises(RuntimeError, match="did not accept its root key"):
        launch._confirm_own(other, {})
    launch._confirm_own(first, {})  # its own servers pass
