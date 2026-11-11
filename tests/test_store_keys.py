"""Per-agent store keys (``store.keys``, item S4) on a throwaway local store: real versitygw (+ IAM)
and Postgres in temporary folders. The role templates are judged by the store itself, revocation by
the servers, and the keys' reach into a unit's jailed tool processes by a real jailed REPL."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import boto3
import psycopg
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from auto_research.store import keys
from tests.test_store_servers import SECRETS, _no_dotenv, make_store  # noqa: F401 - fixtures

pytestmark = pytest.mark.skipif(
    not __import__("shutil").which("versitygw"), reason="versitygw and Postgres are not installed here")

RUN = "run-s4"


@pytest.fixture
def store(make_store):
    """A running store with its two buckets (versioning Enabled), one object in each, and a bucket
    the store does not declare."""
    from auto_research.store import launch

    made = make_store()
    launch.start(made)
    s3 = made.admin_client("s3")
    for bucket in (*made.buckets, "elsewhere"):
        s3.create_bucket(Bucket=bucket)
        s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    return made


def _client(credentials: keys.Credentials):
    return boto3.client("s3", endpoint_url=credentials.s3_endpoint, region_name=credentials.region,
                        aws_access_key_id=credentials.access_key_id,
                        aws_secret_access_key=credentials.secret_access_key,
                        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1}))


def _code(call) -> str:
    try:
        call()
    except ClientError as exc:
        return exc.response["Error"]["Code"]
    return "allowed"


def _live(store) -> list[tuple]:
    with psycopg.connect(store.admin_dsn()) as conn:
        return conn.execute("SELECT kind, key_id, agent, run FROM catalog.credential WHERE revoked_at IS NULL "
                            "ORDER BY issued_at").fetchall()


def test_the_store_enforces_each_role_template(store):
    version = store.admin_client("s3").put_object(Bucket="ixpe", Key="k/a.txt", Body=b"a")["VersionId"]

    async def issue():
        return [await keys.issue(store, unit, role, RUN) for unit, role in
                (("analyst_lead", "reader"), ("analyst_worker_0", "producer"), ("reconciler", "reconciler"))]

    reader, producer, reconciler = asyncio.run(issue())
    r, p, c = _client(reader), _client(producer), _client(reconciler)

    # A reader reads pinned versions only and writes nothing.
    assert _code(lambda: r.put_object(Bucket="ixpe", Key="k/b.txt", Body=b"b", IfNoneMatch="*")) == "AccessDenied"
    assert _code(lambda: r.get_object(Bucket="ixpe", Key="k/a.txt")) == "AccessDenied"
    assert _code(lambda: r.head_object(Bucket="ixpe", Key="k/a.txt")) in ("AccessDenied", "403")
    assert r.get_object(Bucket="ixpe", Key="k/a.txt", VersionId=version)["Body"].read() == b"a"
    assert len(r.list_object_versions(Bucket="ixpe")["Versions"]) == 1
    # A producer writes anywhere in the store's buckets, never outside them.
    assert _code(lambda: p.put_object(Bucket="nicer", Key="k/s/0a1b2c3d/x", Body=b"x", IfNoneMatch="*")) == "allowed"
    assert _code(lambda: p.put_object(Bucket="elsewhere", Key="k/x", Body=b"x")) == "AccessDenied"
    assert _code(lambda: p.get_object(Bucket="nicer", Key="k/s/0a1b2c3d/x")) == "AccessDenied"
    assert _code(lambda: p.put_object_tagging(Bucket="ixpe", Key="k/a.txt", Tagging={"TagSet": [
        {"Key": "state", "Value": "verified"}]})) == "AccessDenied"  # mutable facts live in the catalog
    # The runtime's reconciler login reads as a reader.
    assert _code(lambda: c.put_object(Bucket="ixpe", Key="k/c.txt", Body=b"c")) == "AccessDenied"
    assert c.get_object(Bucket="ixpe", Key="k/a.txt", VersionId=version)["Body"].read() == b"a"
    # Nothing an agent holds deletes or reconfigures.
    for client in (r, p, c):
        assert _code(lambda: client.delete_object(Bucket="ixpe", Key="k/a.txt", VersionId=version)) == "AccessDenied"
        assert _code(lambda: client.delete_object(Bucket="ixpe", Key="k/a.txt")) == "AccessDenied"
        assert _code(lambda: client.put_bucket_versioning(
            Bucket="ixpe", VersioningConfiguration={"Status": "Suspended"})) == "AccessDenied"
    assert store.admin_client("s3").get_bucket_versioning(Bucket="ixpe")["Status"] == "Enabled"

    # The Postgres logins hold their class: a reader cannot claim, a producer can.
    recipe = json.dumps({"kind": "k", "subject": "s", "tool": "t", "algorithm_version": "1", "settings": {},
                         "inputs": []})
    with psycopg.connect(reader.dsn()) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT catalog.claim(%s::jsonb)", (recipe,))
    with psycopg.connect(producer.dsn()) as conn:
        assert conn.execute("SELECT catalog.claim(%s::jsonb)", (recipe,)).fetchone()[0]["status"] == "claimed"
    live = _live(store)
    assert {(kind, agent) for kind, _, agent, _ in live} == {
        (kind, keys.principal(RUN, unit)) for kind in ("s3", "postgres")
        for unit in ("analyst_lead", "analyst_worker_0", "reconciler")}
    async def revoke():
        for unit in ("analyst_lead", "analyst_worker_0", "reconciler"):
            await keys.revoke(store, unit, RUN)

    asyncio.run(revoke())
    assert _live(store) == []


def test_revoke_deletes_the_keys_and_ends_open_sessions(store):
    producer = asyncio.run(keys.issue(store, "analyst_worker_0", "producer", RUN))
    session = psycopg.connect(producer.dsn(), autocommit=True)
    assert session.execute("SELECT session_user").fetchone()[0] == producer.principal

    asyncio.run(keys.revoke(store, "analyst_worker_0", RUN))

    assert _code(lambda: _client(producer).put_object(Bucket="ixpe", Key="k/x", Body=b"x")) == "InvalidAccessKeyId"
    assert producer.principal not in [u["UserName"] for u in store.admin_client("iam").list_users()["Users"]]
    with pytest.raises(psycopg.OperationalError):
        session.execute("SELECT 1")  # the open session was terminated
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(producer.dsn())
    assert _live(store) == []
    asyncio.run(keys.revoke(store, "analyst_worker_0", RUN))  # a second revoke finds nothing and passes


def test_a_second_issue_replaces_the_first_and_a_failed_issue_leaves_nothing(store, monkeypatch):
    first = asyncio.run(keys.issue(store, "analyst_worker_0", "producer", RUN))
    old_session = psycopg.connect(first.dsn(), autocommit=True)
    second = asyncio.run(keys.issue(store, "analyst_worker_0", "producer", RUN))  # a resumed run
    with pytest.raises(psycopg.OperationalError):
        old_session.execute("SELECT 1")  # the first issue's open session ended with it
    assert first.access_key_id != second.access_key_id
    assert _code(lambda: _client(first).list_object_versions(Bucket="ixpe")) == "InvalidAccessKeyId"
    assert _code(lambda: _client(second).list_object_versions(Bucket="ixpe")) == "allowed"
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(first.dsn())
    assert [(k, key) for k, key, _, _ in _live(store) if k == "s3"] == [("s3", second.access_key_id)]

    from auto_research.store import catalog

    async def refused(*_, **__):
        raise catalog.CatalogError("refused for the test")

    monkeypatch.setattr(catalog, "issue_role", refused)
    held = keys.RunKeys(RUN)
    with pytest.raises(catalog.CatalogError):
        asyncio.run(held.issue(store, "analyst_worker_1", "reader"))
    name = keys.principal(RUN, "analyst_worker_1")
    assert name not in [u["UserName"] for u in store.admin_client("iam").list_users()["Users"]]
    assert all(agent != name for _, _, agent, _ in _live(store))
    assert held.held() == [] and held.pending(), "a failed issue is not revoked again at the run's end"

    # A rollback that fails too is named on the error, and the run's end revokes what it left.
    real_drop = keys._drop_user
    calls = {"n": 0}

    def drop_fails_on_rollback(iam, who):
        calls["n"] += 1
        if calls["n"] > 2:  # the issue's own two drops pass; the rollback's fails
            raise RuntimeError("IAM did not answer")
        return real_drop(iam, who)

    monkeypatch.setattr(keys, "_drop_user", drop_fails_on_rollback)
    with pytest.raises(catalog.CatalogError) as caught:
        asyncio.run(held.issue(store, "analyst_worker_2", "reader"))
    assert any("revoking the partial issue" in note and "IAM did not answer" in note
               for note in getattr(caught.value, "__notes__", []))
    left = keys.principal(RUN, "analyst_worker_2")
    assert left in [u["UserName"] for u in store.admin_client("iam").list_users()["Users"]]
    monkeypatch.setattr(keys, "_drop_user", real_drop)
    asyncio.run(held.revoke_all())
    assert left not in [u["UserName"] for u in store.admin_client("iam").list_users()["Users"]]
    assert all(agent != left for _, _, agent, _ in _live(store)) and not held.pending()
    asyncio.run(keys.revoke(store, "analyst_worker_0", RUN))


def test_a_revoke_the_catalog_missed_is_completed_by_the_next(store, monkeypatch):
    """Keys deleted from IAM while the catalog did not answer are marked revoked by a later revoke."""
    credentials = asyncio.run(keys.issue(store, "analyst_worker_0", "producer", RUN))
    real_connect = psycopg.AsyncConnection.connect

    async def down(*_, **__):
        raise psycopg.OperationalError("the catalog does not answer")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", down)
    with pytest.raises(psycopg.OperationalError):
        asyncio.run(keys.revoke(store, "analyst_worker_0", RUN))
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", real_connect)
    assert ("s3", credentials.access_key_id) in [(k, key) for k, key, _, _ in _live(store)]
    asyncio.run(keys.revoke(store, "analyst_worker_0", RUN))
    assert _live(store) == []


def test_run_keys_take_the_role_from_the_store_and_revoke_together(store):
    held = keys.RunKeys(RUN)

    async def go():
        worker = await held.issue(store, "analyst_worker_3")
        lead = await held.issue(store, "analyst_lead")
        librarian = await held.issue(store, "librarian_io")
        await held.revoke("librarian_io")  # a dead unit's alone
        assert [c.unit for c in held.held()] == ["analyst_worker_3", "analyst_lead"]
        await held.revoke_all()
        await held.revoke_all()  # idempotent
        return worker, lead, librarian

    worker, lead, librarian = asyncio.run(go())
    assert (worker.role_class, lead.role_class, librarian.role_class) == ("producer", "reader", "reader")  # the lead reads (ruling S9-5)
    assert held.held() == [] and _live(store) == []
    for gone in (worker, lead, librarian):
        assert _code(lambda: _client(gone).list_object_versions(Bucket="ixpe")) == "InvalidAccessKeyId"


def test_secrets_stay_out_of_representations_and_names_stay_legal():
    credentials = keys.Credentials(store="db", unit="u", run="r", role_class="reader", principal="ar_r_u_00",
                                   access_key_id="AKIA", secret_access_key="s3cr3t", pg_password="pw-secret",
                                   s3_endpoint="http://127.0.0.1:1", region="us-east-1", pg_port=5, pg_database="db")
    assert "s3cr3t" not in repr(credentials) and "pw-secret" not in repr(credentials)
    assert set(credentials.env()) == set(keys.ENV_NAMES) and credentials.env()["PGHOST"] == "127.0.0.1"
    import re

    for run, unit in (("a-b", "w"), ("a_b", "w"), ("X" * 80, "analyst_worker_12"), ("1run", "Ünit")):
        name = keys.principal(run, unit)
        assert re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name), name
    assert keys.principal("a-b", "w") != keys.principal("a_b", "w")


def test_a_remote_store_refuses_with_its_reason(tmp_path, monkeypatch):
    from auto_research.store.config import RemoteStore

    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    remote = RemoteStore(name="db", kind="remote", endpoint="https://s3.example.org", buckets=["b-one"],
                         catalog_dsn="host=db.example.org dbname=db user=u",
                         admin_env={"s3_access_key": "T_S3_KEY", "s3_secret_key": "T_S3_SECRET",
                                    "catalog_password": "T_PG_PASSWORD"})
    with pytest.raises(NotImplementedError, match="aws_sts key adapter"):
        asyncio.run(keys.issue(remote, "analyst_worker_0", "producer", RUN))


# ── the keys reach a unit's jailed tool processes, and nothing else ─────────────────────────────


def _activation(tmp_path: Path) -> Path:
    ini = tmp_path / "activate.sh"
    ini.write_text("export AR_TEST_ACTIVE=1\n")
    return ini


def _jail_on_this_interpreter(monkeypatch, tmp_path: Path) -> Path:
    """This interpreter stands in for the unit's environment (read-only in the jail); a toolkit with
    one package whose tools use the store from the environment they are given."""
    from auto_research.sandbox import jail

    if not jail.jail_available()[0]:
        pytest.skip("the OS jail is required and bwrap cannot run here")
    monkeypatch.setattr(jail, "_config", lambda: {"agent_fs_allow": [sys.prefix], "agent_fs_readonly": [sys.prefix]})
    toolkit = tmp_path / "tk"
    (toolkit / "storeprobe").mkdir(parents=True)
    (toolkit / "storeprobe" / "__init__.py").write_text(
        "import os\n"
        "def seen():\n"
        "    import glob\n"
        "    return {'stdout': 'ok', 'env': {k: v for k, v in os.environ.items() if k.startswith(('AWS_', 'PG')) or k == 'TOOLKIT_STORE'},\n"
        "            'stamp': {k: v for k, v in os.environ.items() if k in ('AR_TOOLKIT_COMMIT', 'AR_ENV_FINGERPRINT')},\n"
        "            'files': glob.glob('/tmp/ar-store-*')}\n"
        "def write(bucket, key):\n"
        "    import boto3, psycopg\n"
        "    s3 = boto3.client('s3', config=__import__('botocore.config').config.Config(s3={'addressing_style': 'path'}))\n"
        "    version = s3.put_object(Bucket=bucket, Key=key, Body=b'from the jail', IfNoneMatch='*')['VersionId']\n"
        "    with psycopg.connect() as conn:\n"
        "        who = conn.execute('SELECT session_user').fetchone()[0]\n"
        "    return {'stdout': 'ok', 'version': version, 'who': who}\n")
    _commit(toolkit, "the probe package")
    return toolkit


def _commit(toolkit: Path, message: str) -> str:
    """Commit the fixture toolkit's tree (a temporary repository of the test's own); its new HEAD."""
    import subprocess

    git = ["git", "-C", str(toolkit), "-c", "user.name=test", "-c", "user.email=test@example.org"]
    if not (toolkit / ".git").exists():
        subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", message], check=True)
    return subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize("owner", ["human", "pipeline"])
def test_a_units_repl_holds_its_keys_and_uses_them(store, monkeypatch, tmp_path, owner):
    from auto_research.sandbox.envs import Environment
    from auto_research.sandbox.persistent import PersistentSandbox

    toolkit = _jail_on_this_interpreter(monkeypatch, tmp_path)
    if owner == "human":
        environment = Environment(name="host", prefix=Path(sys.prefix), activate=_activation(tmp_path))
    else:
        environment = Environment(name="ar-host", prefix=Path(sys.prefix), owner="pipeline")
    credentials = asyncio.run(keys.issue(store, "analyst_worker_0", "producer", RUN))
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "the-orchestrators-own")  # never what a unit gets
    sandbox = PersistentSandbox(toolkit, tmp_path / "w", environment=environment, store_env=credentials.env())

    from auto_research.provenance.capture import toolkit_commit

    async def go():
        await sandbox.start()
        try:
            seen = await sandbox.call_tool("storeprobe", "seen", {})
            # What the runtime stamps on this call's receipt (``SandboxToolProvider`` passes both on).
            seen["call_stamp"], seen["commit"] = sandbox.call_stamp, sandbox.call_commit
            assert seen["commit"] == toolkit_commit(toolkit)
            wrote = await sandbox.call_tool("storeprobe", "write", {"bucket": "ixpe", "key": f"k/{owner}/x"})
            # A call routed to another environment runs on a pool REPL, stamped with that one's state.
            pooled = await sandbox.call_tool("storeprobe", "seen", {}, environment=other)
            pooled["call_stamp"] = sandbox.call_stamp
            # A commit, then the restart that follows it (consensus reruns the workers): the new
            # commit reaches the restarted REPL.
            (toolkit / "storeprobe" / "added.py").write_text("X = 1\n")
            newer = _commit(toolkit, "a reviewed tool")
            # Before the restart the REPL still runs the code it imported: its tools and its receipts
            # both name the boot commit, not the new HEAD.
            between = await sandbox.call_tool("storeprobe", "seen", {})
            assert between["stamp"]["AR_TOOLKIT_COMMIT"] == sandbox.call_commit == seen["commit"]
            await sandbox.restart()
            after = await sandbox.call_tool("storeprobe", "seen", {})
            assert after["stamp"]["AR_TOOLKIT_COMMIT"] == sandbox.call_commit
            return seen, wrote, after, newer, pooled
        finally:
            await sandbox.shutdown()

    other = (Environment(name="ar-host", prefix=Path(sys.prefix), owner="pipeline") if owner == "human"
             else Environment(name="host", prefix=Path(sys.prefix), activate=_activation(tmp_path)))
    try:
        seen, wrote, after, newer, pooled = asyncio.run(go())
    finally:
        asyncio.run(keys.revoke(store, "analyst_worker_0", RUN))
    assert seen["exit_code"] == 0, seen
    assert seen["env"] == credentials.env(), "the REPL did not receive exactly its unit's keys"
    assert seen["files"] == [], "the credentials file outlived the REPL's boot"
    assert seen["call_stamp"] and seen["commit"], "the runtime stamps nothing here: the check below is empty"
    assert seen["stamp"] == {"AR_TOOLKIT_COMMIT": seen["commit"], "AR_ENV_FINGERPRINT": seen["call_stamp"]}
    assert after["stamp"]["AR_TOOLKIT_COMMIT"] == newer != seen["commit"]
    assert pooled["env"] == credentials.env() and pooled["call_stamp"]
    assert pooled["stamp"]["AR_TOOLKIT_COMMIT"] == seen["commit"]
    assert pooled["stamp"]["AR_ENV_FINGERPRINT"] == pooled["call_stamp"] != seen["call_stamp"]
    assert wrote["exit_code"] == 0 and wrote["who"] == credentials.principal, wrote
    body = store.admin_client("s3").get_object(Bucket="ixpe", Key=f"k/{owner}/x", VersionId=wrote["version"])
    assert body["Body"].read() == b"from the jail"
    assert list((tmp_path / "w").rglob("ar-store-*")) == []


def test_a_one_shot_jailed_process_holds_the_keys_and_the_shell_never_does(store, monkeypatch, tmp_path):
    from auto_research.graph.runtime import _builtin_server_config
    from auto_research.sandbox import jail
    from auto_research.sandbox.envs import Environment
    from auto_research.sandbox.runner import run_in_environment

    toolkit = _jail_on_this_interpreter(monkeypatch, tmp_path)
    ini = tmp_path / "activate.sh"
    ini.write_text("export AR_TEST_ACTIVE=1\n")
    environment = Environment(name="host", prefix=Path(sys.prefix), activate=ini)
    credentials = asyncio.run(keys.issue(store, "analyst_lead", "reader", RUN))
    probe = f"{sys.executable} -P -c 'import json, storeprobe; print(json.dumps(storeprobe.seen()))'"

    def seen_by(**kwargs):
        done = run_in_environment(environment, probe, toolkit_root=toolkit, label="s4-oneshot", **kwargs)
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout.strip().splitlines()[-1])

    try:
        seen = seen_by(store_env=credentials.env())
        plain = seen_by()
    finally:
        asyncio.run(keys.revoke(store, "analyst_lead", RUN))
    assert seen["env"] == credentials.env() and seen["files"] == []
    from auto_research.env import env_lock_dir
    from auto_research.provenance.capture import toolkit_commit
    from auto_research.sandbox.envs import fingerprint_and_lock

    assert seen["stamp"] == {"AR_TOOLKIT_COMMIT": toolkit_commit(toolkit),
                             "AR_ENV_FINGERPRINT": fingerprint_and_lock(environment, env_lock_dir())}
    assert all(seen["stamp"].values())
    assert plain["env"] == {} and plain["stamp"] == {}, "a call given no keys received some"
    from auto_research.sandbox.runner import _keyed_label

    keyed = _keyed_label("s4-oneshot", credentials.env())
    assert keyed != "s4-oneshot", "a call carrying keys shares its label's folders with every other unit's"
    assert list(jail.private_tmp(keyed, jail.jail_home(keyed)).glob("ar-store-*")) == []

    # The shared shell server is built from a clean environment: no store variable reaches it, even
    # when the orchestrator's own environment holds some.
    for name, value in credentials.env().items():
        monkeypatch.setenv(name, value)
    server = _builtin_server_config(environment, toolkit, cwd=tmp_path / "run")
    assert not [k for k in server["env"] if k.startswith(("AWS_", "PG")) or k == keys.STORE_NAME_VARIABLE]
    assert credentials.secret_access_key not in json.dumps(server)


# ── the run's launch step, unit keys and teardown (graph.runtime) ─────────────────────────────


class _Model:
    provider = "openai"
    model = "stand-in"


def test_the_launch_step_starts_reconciles_and_issues_each_units_keys(make_store, monkeypatch, tmp_path):
    """``start_run_stores`` starts the store and reconciles it as the run's reconciler login;
    ``issue_unit_keys`` gives each unit the store's role for it; ``attach_worker_runtime`` hands each
    worker's REPL its keys and the worker its view of the store's records; ``revoke_all`` ends them.
    No secret reaches the ledger."""
    import yaml

    from auto_research.agents import AnalystWorkerAgent
    from auto_research.config_paths import orchestration_yaml
    from auto_research.graph import runtime
    from auto_research.monitor import RunMonitor
    from auto_research.sandbox.envs import Environment
    from auto_research.store import launch

    from tests.fixtures import toolkit as fixture

    # The reconciler judges products against the declared toolkits: this run declares the fixture's.
    root = fixture.build_toolkit(tmp_path / "toolkit", env_base="astropy")
    declared = {"name": fixture.TOOLKIT_NAME, "root": str(root), "package_glob": "*_utils",
                "template_package": fixture.PKG_NAME, "authoring_branch": fixture.BRANCH,
                "default_env": fixture.ENV_ID}
    made = make_store(extra_config={"toolkits": [declared]}, roles={"producer": ["analyst_worker_*"]})
    orch = yaml.safe_load(orchestration_yaml().read_text())
    monitor = RunMonitor(jsonl_path=tmp_path / "ledger" / "monitor.jsonl")
    held = keys.RunKeys(RUN)
    runtime.bind_run_stores(orch, RUN)
    try:
        # A store without its buckets is refused as the preflight would word it, before any key.
        with pytest.raises(RuntimeError, match=r"store db is not ready: store_buckets: bucket ixpe is missing"
                                               r".*create it"):
            asyncio.run(runtime.start_run_stores(RUN, held, monitor))
        assert launch.down(made) == [] and not held.pending()
        s3 = made.admin_client("s3")
        for bucket in made.buckets:
            s3.create_bucket(Bucket=bucket)
            s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        log_start = made.access_log.stat().st_size if made.access_log.exists() else 0
        asyncio.run(runtime.start_run_stores(RUN, held, monitor))  # the running set is shared
        # The reconciler's pass lists the buckets under its own key, never the admin's (ruling 2); the
        # readiness checks before it are the admin's (ListAllMyBuckets, HeadBucket, versioning).
        from auto_research.store.records import parse_access_line

        reconciler_key = held.held()[0].access_key_id
        with made.access_log.open() as log:
            log.seek(log_start)
            lines = [line for line in map(parse_access_line, log) if line is not None]
        listings = [line for line in lines if line.op == "s3_ListObjectVersions"]
        assert listings and {line.requester for line in listings} == {reconciler_key}, lines
        assert {line.bucket for line in listings} == set(made.buckets)
        assert [c.unit for c in held.held()] == [runtime.RECONCILER_UNIT]
        assert held.held()[0].role_class == "reconciler"

        unit_keys = asyncio.run(runtime.issue_unit_keys(held, ["analyst_worker_0", "analyst_lead"], monitor))
        assert {u: c.role_class for u, c in unit_keys.items()} == {"analyst_worker_0": "producer",
                                                                    "analyst_lead": "reader"}
        worker = AnalystWorkerAgent(model=_Model(), prompt="w")
        ini = tmp_path / "activate.sh"
        ini.write_text("export AR_TEST_ACTIVE=1\n")
        settings = runtime.SandboxSettings(environment=Environment(name="host", prefix=Path(sys.prefix), activate=ini))
        sandboxes = runtime.attach_worker_runtime([worker], run_id=RUN, registry=object(), toolkit_root=tmp_path,
                                                  sandbox_settings=settings, credentials=unit_keys)
        mine = unit_keys["analyst_worker_0"]
        assert sandboxes[0]._store_env == mine.env()
        assert worker.store_records.agent == mine.principal and worker.store_records.run == RUN
        assert {(a.store, a.bucket) for a in worker.store_records.writable} == {("db", b) for b in made.buckets}
    finally:
        asyncio.run(held.revoke_all())
        runtime.release_run_stores(RUN)
    assert _live(made) == []
    events = [json.loads(line) for line in (tmp_path / "ledger" / "monitor.jsonl").read_text().splitlines()]
    assert [e["event"] for e in events] == ["store.launch", "store.launch.error", "store.launch",
                                           "store.reconcile", "store.credentials.issue", "store.credentials.issue"]
    ledger = (tmp_path / "ledger" / "monitor.jsonl").read_text()
    for credentials in unit_keys.values():
        assert credentials.secret_access_key not in ledger and credentials.pg_password not in ledger


def test_a_run_cannot_change_the_stores_another_run_holds(make_store):
    from auto_research.graph import runtime

    first = {"stores": [make_store().model_dump(mode="json")]}
    other = {"stores": [{**first["stores"][0], "buckets": ["other-bucket"]}]}
    runtime.bind_run_stores(first, "run-a")
    try:
        runtime.bind_run_stores(first, "run-b")  # the same stores: shared
        with pytest.raises(RuntimeError, match="declares other stores"):
            runtime.bind_run_stores(other, "run-c")
        with pytest.raises(RuntimeError, match="stores are invalid"):
            runtime.bind_run_stores({"stores": []}, "run-d")
    finally:
        runtime.release_run_stores("run-a")
        runtime.release_run_stores("run-b")
    runtime.bind_run_stores(other, "run-c")  # none holds them now
    runtime.release_run_stores("run-c")
    from auto_research.store.config import stores

    assert stores()[0].buckets == ("other-bucket",)


def test_status_and_stop(store, capsys):
    from auto_research.store import launch
    from auto_research.store.__main__ import main

    held = keys.RunKeys(RUN)
    asyncio.run(held.issue(store, "analyst_worker_0"))
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "servers: running on" in out and "answering: all" in out and "live keys: 2" in out
    assert keys.principal(RUN, "analyst_worker_0") in out
    with pytest.raises(SystemExit, match="live key"):
        main(["stop"])  # a run may still be using it
    assert launch.down(store) == []
    asyncio.run(held.revoke_all())
    assert main(["stop", "--store", "db"]) == 0
    assert "store db: stopped" in capsys.readouterr().out
    assert len(launch.down(store)) == 3
    assert main(["status"]) == 0
    assert "servers: not running" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="no store named"):
        main(["status", "--store", "nope"])


@pytest.mark.parametrize("failure", ["setup", "graph"])
def test_run_research_revokes_every_key_it_issued_however_it_ends(store, monkeypatch, tmp_path, failure):
    """The real ``run_research`` on the hermetic wiring (``tests.test_setup_teardown``) with a store
    declared: the launch step reconciles it, the worker is issued its keys, and the run's teardown
    revokes them after the tools server and before the channel, when the setup fails and when the
    graph fails."""
    from auto_research.graph import runtime
    from auto_research.sandbox.persistent import PersistentSandbox
    from tests.test_react_ceiling import _hermetic_run, _ledger
    from tests.test_setup_teardown import _run, _spy_teardown

    _hermetic_run(tmp_path, monkeypatch)
    runtime.load_orchestration_config()["stores"] = [store.model_dump(mode="json")]
    order: list[str] = []
    _spy_teardown(monkeypatch, order)
    real_revoke_all = keys.RunKeys.revoke_all

    async def revoke_all(self):
        if self.pending():
            order.append("credentials")
        await real_revoke_all(self)

    async def started(self):  # the REPL is outside this test; the keys it was handed are checked
        order.append("sandbox.start")
        order.append(sorted(self._store_env))

    monkeypatch.setattr(keys.RunKeys, "revoke_all", revoke_all)
    monkeypatch.setattr(PersistentSandbox, "start", started)
    if failure == "setup":
        def broken_build(**_):
            raise RuntimeError("the graph did not build")

        monkeypatch.setattr("auto_research.graph.runtime.build_graph", broken_build)
    with pytest.raises(RuntimeError, match="did not build" if failure == "setup" else "the reviewer was reached"):
        _run(tmp_path, order)
    assert order[:2] == ["sandbox.start", sorted(keys.ENV_NAMES)]
    assert order.count("credentials") == 1, order
    at = order.index("credentials")
    assert order[at - 1:at + 2] == ["mcp", "credentials", "connector"], order
    events = _ledger(tmp_path)
    issued = [e["data"] for e in events if e["event"] == "store.credentials.issue"]
    # The lead holds a reader login of its own (S9); the worker its producer keys.
    assert sorted((d["unit"], d["role"]) for d in issued) == [("analyst_lead", "reader"),
                                                              ("analyst_worker_0", "producer")]
    assert [e["event"] for e in events if e["event"].startswith("store.")][:2] == ["store.launch", "store.reconcile"]
    assert _live(store) == [], "a key outlived its run"
    assert [u for u in store.admin_client("iam").list_users()["Users"]] == []


def test_the_launch_sweeps_the_keys_of_dead_runs_on_this_host_only(store, monkeypatch, tmp_path):
    """A run is dead when its runtime process is gone (``ledgers/<run>/runtime.json``): the next
    launch revokes its keys. A live run, a run of another host and a run with no record keep theirs;
    ``revoke --run`` serves the human for those."""
    import socket
    import subprocess

    from auto_research.graph import runtime
    from auto_research.monitor import RunMonitor
    from auto_research.sandbox.workspace import ledger_dir
    from auto_research.store.__main__ import main

    gone = subprocess.Popen(["true"])
    gone.wait()

    def record(run, **fields):
        folder = ledger_dir(run)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / runtime.RUN_PROCESS_FILE).write_text(json.dumps(fields))

    import os

    record("run-dead", host=socket.gethostname(), pid=gone.pid, create_time=12345)
    # A pid now running something else (its start time differs): the run's process is gone.
    record("run-recycled", host=socket.gethostname(), pid=os.getpid(), create_time=-1)
    record("run-elsewhere", host="another-host", pid=gone.pid, create_time=12345)
    # Records that do not say: unknown, never dead, and no launch is blocked by them.
    record("run-nostart", host=socket.gethostname(), pid=gone.pid, create_time=None)
    record("run-corrupt", host=socket.gethostname(), pid="abc", create_time=1)
    (ledger_dir("run-list") / runtime.RUN_PROCESS_FILE).parent.mkdir(parents=True, exist_ok=True)
    (ledger_dir("run-list") / runtime.RUN_PROCESS_FILE).write_text("[1, 2]")
    runtime.record_run_process("run-alive")  # this process
    kept = ("run-elsewhere", "run-alive", "run-unrecorded", "run-nostart", "run-corrupt", "run-list")
    held = {run: keys.RunKeys(run) for run in ("run-dead", "run-recycled", *kept)}
    for run, run_keys in held.items():
        asyncio.run(run_keys.issue(store, "analyst_worker_0"))
    asyncio.run(held["run-dead"].issue(store, "librarian_io"))
    dead = held["run-dead"].held()

    monitor = RunMonitor(jsonl_path=tmp_path / "ledger" / "monitor.jsonl")
    orch = {"stores": [store.model_dump(mode="json")]}
    current = keys.RunKeys("run-now")
    runtime.bind_run_stores(orch, "run-now")
    try:
        asyncio.run(runtime.start_run_stores("run-now", current, monitor))
    finally:
        asyncio.run(current.revoke_all())
        runtime.release_run_stores("run-now")
    live = {run for _, _, _, run in _live(store)}
    assert live == set(kept), live
    assert [runtime.run_process_alive(r) for r in ("run-dead", "run-recycled", "run-alive", "run-elsewhere",
                                                    "run-nostart", "run-corrupt", "run-list", "run-unrecorded")] \
        == [False, False, True, None, None, None, None, None]
    for credentials in dead:
        assert _code(lambda: _client(credentials).list_object_versions(Bucket="ixpe")) == "InvalidAccessKeyId"
    events = [json.loads(line) for line in (tmp_path / "ledger" / "monitor.jsonl").read_text().splitlines()]
    sweeps = {e["data"]["run"]: e["data"]["principals"] for e in events if e["event"] == "store.credentials.sweep"}
    assert set(sweeps) == {"run-dead", "run-recycled"}
    assert sorted(sweeps["run-dead"]) == sorted(c.principal for c in dead)

    # The human revokes what the sweep leaves: a run still running only with --force.
    with pytest.raises(SystemExit, match="still runs on this host"):
        main(["revoke", "--run", "run-alive"])
    with pytest.raises(SystemExit, match="recorded on host another-host"):
        main(["revoke", "--run", "run-elsewhere"])
    assert main(["revoke", "--run", "run-elsewhere", "--force"]) == 0
    assert main(["revoke", "--run", "run-alive", "--force"]) == 0
    for run in ("run-unrecorded", "run-nostart", "run-corrupt", "run-list"):
        assert main(["revoke", "--run", run]) == 0
    assert _live(store) == []
    assert main(["revoke", "--run", "run-unrecorded"]) == 0  # nothing left: says so


def test_two_revokes_of_one_principal_at_once_both_end_cleanly(store):
    """Two launches sweeping the same dead run: neither raises, nothing stays live."""
    credentials = asyncio.run(keys.issue(store, "analyst_worker_0", "producer", RUN))

    async def both():
        return await asyncio.gather(*(keys.revoke_principal(store, credentials.principal) for _ in range(2)),
                                    return_exceptions=True)

    assert asyncio.run(both()) == [None, None]
    assert _live(store) == []


def test_a_catalog_without_its_schema_fails_the_launch_step_before_any_key(store, tmp_path):
    """The launch step reads the catalog as the preflight's ``store_catalog`` does: a missing schema
    stops it with that item's words, before a key is issued or the store reconciled."""
    from auto_research.graph import runtime
    from auto_research.monitor import RunMonitor
    from auto_research.store import catalog, health

    with psycopg.connect(store.admin_dsn(), autocommit=True) as conn:
        conn.execute("DROP SCHEMA catalog CASCADE")
    (problem,) = health.ready_problems(store)
    assert problem.startswith("store_catalog: no catalog schema in this database"), problem
    held = keys.RunKeys(RUN)
    runtime.bind_run_stores({"stores": [store.model_dump(mode="json")]}, RUN)
    try:
        with pytest.raises(RuntimeError, match="store db is not ready: store_catalog: no catalog schema"):
            asyncio.run(runtime.start_run_stores(RUN, held, RunMonitor(jsonl_path=tmp_path / "m.jsonl")))
    finally:
        runtime.release_run_stores(RUN)
    assert not held.pending()
    asyncio.run(catalog.apply_schema(store.admin_dsn()))
    assert health.ready_problems(store) == []


def test_only_the_runtimes_stamp_reaches_a_keyed_repl(store, monkeypatch, tmp_path):
    """An activation that sets the stamp's names never supplies them: the REPL holds the runtime's
    values, and a value the runtime does not know (a toolkit with no commit) is absent."""
    import shutil

    from auto_research.sandbox.envs import Environment
    from auto_research.sandbox.persistent import PersistentSandbox

    toolkit = _jail_on_this_interpreter(monkeypatch, tmp_path)
    shutil.rmtree(toolkit / ".git")  # no commit to name
    ini = tmp_path / "guessing.sh"
    ini.write_text("export AR_TOOLKIT_COMMIT=guess AR_ENV_FINGERPRINT=guess\n")
    credentials = asyncio.run(keys.issue(store, "analyst_worker_0", "producer", RUN))
    sandbox = PersistentSandbox(toolkit, tmp_path / "w", store_env=credentials.env(),
                                environment=Environment(name="host", prefix=Path(sys.prefix), activate=ini))

    async def go():
        await sandbox.start()
        try:
            seen = await sandbox.call_tool("storeprobe", "seen", {})
            return seen, sandbox.call_stamp, sandbox.call_commit
        finally:
            await sandbox.shutdown()

    try:
        seen, stamp, commit = asyncio.run(go())
    finally:
        asyncio.run(keys.revoke(store, "analyst_worker_0", RUN))
    assert commit is None and stamp, (commit, stamp)
    assert seen["stamp"] == {"AR_ENV_FINGERPRINT": stamp}, seen["stamp"]
