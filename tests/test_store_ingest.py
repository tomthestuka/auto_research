"""The human's store acts of item S11 (``python -m auto_research.store start|ingest|produce``), driven
through the CLI's own entry point on a throwaway local store (real versitygw + IAM and Postgres in
temporary folders): the ingest of files on disk as products of the human's recipe, the start that
creates the declared buckets, and the producer job a jailed process runs with keys of its own."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import psycopg
import pytest

from auto_research.store import __main__ as cli
from auto_research.store import ingest
from tests.test_store_servers import SECRETS, _no_dotenv, make_store  # noqa: F401 - fixtures

pytestmark = pytest.mark.skipif(
    not __import__("shutil").which("versitygw"), reason="versitygw and Postgres are not installed here")

PASSWORD = "the human's own 1"


def _with_toolkit(make_store, tmp_path):
    """A store whose configuration declares the fixture toolkit (committed clean on its authoring
    branch: the reconciler judges products against the declared toolkits); ``(store, toolkit root)``."""
    from auto_research.toolkit_contract import reset as reset_contract

    from tests.fixtures import toolkit as fixture

    root = fixture.build_toolkit(tmp_path / "toolkit", env_base="astropy")
    declared = {"name": fixture.TOOLKIT_NAME, "root": str(root), "package_glob": "*_utils",
                "template_package": fixture.PKG_NAME, "authoring_branch": fixture.BRANCH,
                "default_env": fixture.ENV_ID}
    made = make_store(extra_config={"toolkits": [declared]})
    reset_contract()
    return made, root


@pytest.fixture
def store(make_store, tmp_path):
    """A running store with its declared buckets, versioning Enabled."""
    from auto_research.store import launch
    from auto_research.toolkit_contract import reset as reset_contract

    made, _ = _with_toolkit(make_store, tmp_path)
    launch.start(made)
    s3 = made.admin_client("s3")
    for bucket in made.buckets:
        s3.create_bucket(Bucket=bucket)
        s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    yield made
    reset_contract()


@pytest.fixture
def password_file(store, tmp_path) -> str:
    """The human's password set through the CLI (``human-password``), in a file only its owner reads."""
    path = tmp_path / "human.pw"
    path.write_text(PASSWORD + "\n")
    path.chmod(0o600)
    assert cli.main(["human-password", "--store", "db", "--password-file", str(path)]) == 0
    return str(path)


@pytest.fixture
def spectra(tmp_path) -> Path:
    """A folder of reduced files as a human leaves them: what the mapping names, and more."""
    folder = tmp_path / "reduced" / "0001"
    (folder / "spec").mkdir(parents=True)
    for name, body in (("a_src.pha", b"source"), ("a_bkg.pha", b"background"), ("a.rmf", b"response")):
        (folder / "spec" / name).write_bytes(body)
    (folder / "spec" / "notes.txt").write_text("left out")
    return folder


def _mapping(tmp_path: Path, folder: Path, files=("spec/*.pha", "spec/a.rmf"), bucket="ixpe") -> str:
    path = tmp_path / "mapping.yaml"
    path.write_text(json.dumps({"products": [{"bucket": bucket, "kind": "pha", "subject": "0001",
                                              "folder": str(folder), "files": list(files)}]}))
    return str(path)


def _lines(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]


def _versions(store) -> list[tuple[str, str]]:
    s3 = store.admin_client("s3")
    return sorted((b, v["Key"]) for b in store.buckets for v in s3.list_object_versions(Bucket=b).get("Versions", []))


def _product(store, recipe_hash: str) -> dict:
    with psycopg.connect(store.admin_dsn(), row_factory=psycopg.rows.dict_row) as conn:
        return conn.execute("SELECT * FROM catalog.product WHERE recipe_hash = %s", (recipe_hash,)).fetchone()


def test_an_ingest_stores_each_file_once_as_the_humans_product_and_the_reconciler_verifies_it(
        store, password_file, spectra, tmp_path, capsys):
    from auto_research.store import reconcile as rc

    mapping = _mapping(tmp_path, spectra)
    assert cli.main(["ingest", "--store", "db", "--mapping", mapping, "--check"]) == 0
    [planned] = _lines(capsys)
    assert sorted(planned["files"]) == ["spec/a.rmf", "spec/a_bkg.pha", "spec/a_src.pha"]
    assert _versions(store) == []  # the check writes nothing

    assert cli.main(["ingest", "--store", "db", "--mapping", mapping, "--password-file", password_file]) == 0
    [done] = _lines(capsys)
    assert done["status"] == "stored" and done["objects"] == 4
    record, recipe_hash = done["record"], done["recipe_hash"]
    folder = f"pha/0001/{recipe_hash[:8]}"
    assert record["key"] == f"{folder}/recipe.json" and record["bucket"] == "ixpe"
    assert _versions(store) == sorted(("ixpe", f"{folder}/{rel}") for rel in
                                      ("recipe.json", "spec/a.rmf", "spec/a_bkg.pha", "spec/a_src.pha"))
    s3 = store.admin_client("s3")
    head = s3.head_object(Bucket="ixpe", Key=f"{folder}/spec/a_src.pha")
    assert head["Metadata"] == {"recipe-hash": recipe_hash, "producer": "ar_human", "toolkit-commit": "human-ingest",
                                "env-fingerprint": "human-ingest",
                                "sha256": __import__("hashlib").sha256(b"source").hexdigest()}
    listed = rc.RecipeFile.model_validate_json(
        s3.get_object(Bucket="ixpe", Key=record["key"], VersionId=record["version_id"])["Body"].read())
    assert listed.recipe["tool"] == ingest.TOOL and listed.recipe["inputs"] == []
    assert listed.recipe["settings"]["folder"] == str(spectra)
    assert sorted(listed.recipe["settings"]["files"]) == sorted(o.key.removeprefix(folder + "/") for o in listed.objects)
    row = _product(store, recipe_hash)
    assert (row["owner"], row["state"], row["toolkit_commit"]) == ("ar_human", "committed", ingest.PROVENANCE)

    report = asyncio.run(rc.reconcile(store, run="t-ingest"))
    assert report.verified == [recipe_hash] and not report.flags
    # Its tool is no manifest key: the reconciler verifies it and never makes it current.
    assert report.promoted == [] and _product(store, recipe_hash)["state"] == "verified"

    # The same files again: the recipe is held, nothing is written.
    assert cli.main(["ingest", "--store", "db", "--mapping", mapping, "--password-file", password_file]) == 0
    assert [(d["status"], d["record"]) for d in _lines(capsys)] == [("reused", record)]
    assert len(_versions(store)) == 4
    # Other bytes under the same names are another product, in a folder of its own.
    (spectra / "spec" / "a_src.pha").write_bytes(b"source, reduced again")
    assert cli.main(["ingest", "--store", "db", "--mapping", mapping, "--password-file", password_file]) == 0
    [again] = _lines(capsys)
    assert again["status"] == "stored" and again["recipe_hash"] != recipe_hash and len(_versions(store)) == 8


def test_an_ingest_the_store_refuses_gives_its_claim_up_and_the_retry_writes_a_new_generation(
        store, password_file, spectra, tmp_path):
    from auto_research.store import keys
    from auto_research.store.config import store as declared

    [planned] = ingest.plan(declared("db"), ingest.load_mapping(_mapping(tmp_path, spectra)))
    # Uploads signed by a reader's key: the store refuses the first PUT, after the claim.
    reader = asyncio.run(keys.issue(store, "analyst_lead", "reader", "run-s11"))
    refusing = __import__("boto3").client(
        "s3", endpoint_url=reader.s3_endpoint, region_name=reader.region, aws_access_key_id=reader.access_key_id,
        aws_secret_access_key=reader.secret_access_key,
        config=__import__("botocore.config").config.Config(s3={"addressing_style": "path"}))

    async def run(p, s3):
        async with await psycopg.AsyncConnection.connect(cli._human_dsn(store, PASSWORD), autocommit=True) as conn:
            return await ingest.ingest_one(store, p, conn=conn, s3=s3)

    with pytest.raises(ingest.IngestError, match="the store refused the ingest"):
        asyncio.run(run(planned, refusing))
    recipe_hash = asyncio.run(_hash(store, planned.recipe))
    assert _product(store, recipe_hash)["state"] == "failed" and _versions(store) == []
    done = asyncio.run(run(planned, store.admin_client("s3")))
    assert done["status"] == "stored" and done["record"]["key"] == f"pha/0001/{recipe_hash[:8]}.2/recipe.json"
    asyncio.run(keys.revoke(store, "analyst_lead", "run-s11"))


def test_a_file_changed_after_the_plan_stops_the_ingest_before_any_claim(store, password_file, spectra, tmp_path):
    from auto_research.store.config import store as declared

    [planned] = ingest.plan(declared("db"), ingest.load_mapping(_mapping(tmp_path, spectra)))
    (spectra / "spec" / "a_src.pha").write_bytes(b"edited after the plan")

    async def run():
        async with await psycopg.AsyncConnection.connect(cli._human_dsn(store, PASSWORD), autocommit=True) as conn:
            return await ingest.ingest_one(store, planned, conn=conn, s3=store.admin_client("s3"))

    with pytest.raises(ingest.IngestError, match="spec/a_src.pha changed since the plan hashed it"):
        asyncio.run(run())
    with psycopg.connect(store.admin_dsn()) as conn:
        assert conn.execute("SELECT count(*) FROM catalog.product").fetchone() == (0,)
    assert _versions(store) == []


async def _hash(store, recipe) -> str:
    from psycopg.types.json import Jsonb

    async with await psycopg.AsyncConnection.connect(store.admin_dsn(), autocommit=True) as conn:
        return (await (await conn.execute("SELECT catalog.recipe_hash(%s)", (Jsonb(recipe),))).fetchone())[0]


@pytest.mark.parametrize("case, said", [
    ("bucket", "name a bucket of store db"),
    ("nothing", "matches no file"),
    ("marker", "recipe.json names the product's own record"),
    ("link", "lies outside"),
    ("twice", "listed twice"),
])
def test_a_plan_refuses_what_it_cannot_store_as_named(store, spectra, tmp_path, case, said):
    from auto_research.store.config import store as declared

    files, bucket = ["spec/*.pha"], "ixpe"
    if case == "bucket":
        bucket = "elsewhere"
    elif case == "nothing":
        files = ["spec/*.fits"]
    elif case == "marker":
        (spectra / "spec" / "recipe.json").write_text("{}")
        files = ["spec/*.json"]
    elif case == "twice":
        path = tmp_path / "twice.yaml"
        entry = {"kind": "pha", "subject": "0001", "folder": str(spectra), "files": files}
        path.write_text(json.dumps({"products": [{"bucket": "ixpe", **entry}, {"bucket": "nicer", **entry}]}))
        with pytest.raises(ingest.IngestError, match=said):
            ingest.plan(declared("db"), ingest.load_mapping(path))
        return
    else:
        outside = tmp_path / "outside.pha"
        outside.write_bytes(b"x")
        (spectra / "spec" / "linked.pha").symlink_to(outside)
    with pytest.raises(ingest.IngestError, match=said):
        ingest.plan(declared("db"), ingest.load_mapping(_mapping(tmp_path, spectra, files, bucket)))
    with pytest.raises(SystemExit, match=said):
        cli.main(["ingest", "--store", "db", "--mapping", _mapping(tmp_path, spectra, files, bucket), "--check"])


def test_start_creates_the_declared_buckets_with_versioning_and_says_the_store_is_ready(make_store, capsys):
    made = make_store()
    assert cli.main(["start", "--store", "db"]) == 0
    assert "not ready: store_buckets: bucket ixpe is missing" in capsys.readouterr().out
    assert cli.main(["start", "--store", "db", "--create-buckets"]) == 0
    out = capsys.readouterr().out
    assert "bucket ixpe created" in out and "bucket nicer: versioning enabled" in out and out.rstrip().endswith("ready")
    s3 = made.admin_client("s3")
    assert all(s3.get_bucket_versioning(Bucket=b)["Status"] == "Enabled" for b in made.buckets)


@pytest.fixture
def producing(make_store, tmp_path, monkeypatch):
    """A store whose configuration declares the fixture toolkit (committed clean on its authoring
    branch), this interpreter standing in for the toolkit environment the job names."""
    from auto_research import env
    from auto_research.sandbox import jail
    from auto_research.sandbox.envs import Environment
    from auto_research.store import launch
    from auto_research.toolkit_contract import reset as reset_contract

    if not jail.jail_available()[0]:
        pytest.skip("the OS jail is required and bwrap cannot run here")
    made, root = _with_toolkit(make_store, tmp_path)
    launch.start(made)
    s3 = made.admin_client("s3")
    for bucket in made.buckets:
        s3.create_bucket(Bucket=bucket)
        s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    (tmp_path / "work").mkdir()
    monkeypatch.setattr(jail, "_config", lambda: {"agent_fs_allow": [sys.prefix, str(tmp_path / "work")],
                                                  "agent_fs_readonly": [sys.prefix]})
    monkeypatch.setattr(env, "env_catalogue", lambda config=None: [Environment(name="ar-host", prefix=Path(sys.prefix))])
    source = tmp_path / "source"
    source.mkdir()
    (source / "data.txt").write_text("from the source folder")
    yield made, root, source
    reset_contract()


JOB = ("import boto3, json, os, psycopg\n"
       "from botocore.config import Config\n"
       "src = os.environ['SRC']\n"
       "body = open(src + '/data.txt', 'rb').read()\n"
       "try:\n"
       "    open(src + '/written.txt', 'w').write('x'); wrote = True\n"
       "except OSError:\n"
       "    wrote = False\n"
       "s3 = boto3.client('s3', config=Config(s3={'addressing_style': 'path'}))\n"
       "v = s3.put_object(Bucket='ixpe', Key='raw/0001/abcd1234/data.txt', Body=body, IfNoneMatch='*')['VersionId']\n"
       "with psycopg.connect() as conn:\n"
       "    who = conn.execute('SELECT session_user').fetchone()[0]\n"
       "print(json.dumps({'version': v, 'who': who, 'source_writable': wrote, 'store': os.environ.get('TOOLKIT_STORE'),\n"
       "                  'commit': os.environ.get('AR_TOOLKIT_COMMIT'), 'env': os.environ.get('AR_ENV_FINGERPRINT')}))\n")


def _job(source: Path) -> list[str]:
    return ["/usr/bin/env", f"SRC={source}", sys.executable, "-P", "-c", JOB]


def test_a_producer_job_runs_jailed_with_its_own_keys_reads_its_sources_read_only_and_leaves_no_key(
        producing, capsys):
    import subprocess

    made, root, source = producing
    code = cli.main(["produce", "--store", "db", "--unit", "migration", "--env", "host", "--readonly", str(source),
                     "--", *_job(source)])
    out = capsys.readouterr()
    assert code == 0, out.err
    seen = json.loads(out.out.strip().splitlines()[-1])
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert seen["commit"] == head and seen["store"] == "db" and seen["source_writable"] is False
    assert seen["env"] and len(seen["env"]) == 64  # the environment's fingerprint, as production stamps it
    assert seen["who"].startswith("ar_cli_produce_") and "_migration_" in seen["who"]  # its own login
    body = made.admin_client("s3").get_object(Bucket="ixpe", Key="raw/0001/abcd1234/data.txt",
                                              VersionId=seen["version"])["Body"].read()
    assert body == b"from the source folder" and not (source / "written.txt").exists()
    with psycopg.connect(made.admin_dsn()) as conn:
        live = conn.execute("SELECT agent FROM catalog.credential WHERE revoked_at IS NULL").fetchall()
        login = conn.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (seen["who"],)).fetchone()
    assert live == [] and login == (False,)  # the job's keys are revoked when it ends


def test_a_producer_job_refuses_a_toolkit_with_uncommitted_changes_before_any_key(producing, capsys):
    made, root, source = producing
    (root / "stray.py").write_text("x = 1\n")
    with pytest.raises(SystemExit, match="Commit the toolkit first"):
        cli.main(["produce", "--store", "db", "--unit", "migration", "--env", "host", "--", *_job(source)])
    with psycopg.connect(made.admin_dsn()) as conn:
        assert conn.execute("SELECT count(*) FROM catalog.credential").fetchone() == (0,)
    with pytest.raises(SystemExit):
        cli.main(["produce", "--store", "db", "--unit", "migration", "--env", "host"])
    assert "produce needs --store, --unit, --env and -- COMMAND" in capsys.readouterr().err


def _live(made) -> list:
    with psycopg.connect(made.admin_dsn()) as conn:
        return conn.execute("SELECT agent FROM catalog.credential WHERE revoked_at IS NULL").fetchall()


@pytest.mark.parametrize("job, timeout, code, said", [
    (["/bin/sh", "-c", "echo half done; exit 3"], "60", 3, "half done"),
    (["/bin/sh", "-c", "echo first line; sleep 60"], "3", 124, "first line"),
])
def test_a_producer_job_that_fails_or_runs_past_its_timeout_keeps_its_output_and_leaves_no_key(
        producing, capsys, job, timeout, code, said):
    made, _, _ = producing
    assert cli.main(["produce", "--store", "db", "--unit", "migration", "--env", "host", "--timeout", timeout,
                     "--", *job]) == code
    out = capsys.readouterr()
    assert said in out.out and "revoked" in out.err
    assert _live(made) == []


def test_a_producer_job_refuses_a_read_only_folder_inside_a_writable_jail_root(producing, tmp_path):
    made, _, _ = producing
    inside = tmp_path / "work" / "sources"
    inside.mkdir()
    with pytest.raises(SystemExit, match="meets a folder the jail binds writable"):
        cli.main(["produce", "--store", "db", "--unit", "migration", "--env", "host", "--readonly", str(inside),
                  "--", "/bin/true"])
    with psycopg.connect(made.admin_dsn()) as conn:
        assert conn.execute("SELECT count(*) FROM catalog.credential").fetchone() == (0,)


def test_a_file_above_one_put_goes_up_in_parts_with_the_humans_settings_and_the_reconciler_verifies_it(
        store, password_file, spectra, tmp_path, monkeypatch, capsys):
    """A file above the single-PUT limit (lowered here: S3's is 5 GiB) is uploaded in parts, each
    checked by the store; the product's own settings stand in its recipe beside folder and files."""
    import hashlib

    from auto_research.store import reconcile as rc

    monkeypatch.setattr(ingest, "SINGLE_PUT_MAX", 6 << 20)
    monkeypatch.setattr(ingest, "PART_SIZE", 5 << 20)  # S3's smallest part but the last
    big = spectra / "spec" / "events.fits"
    big.write_bytes(bytes(range(256)) * (11 << 12))  # 11 MiB: three parts
    path = tmp_path / "mapping.yaml"
    path.write_text(json.dumps({"products": [{
        "bucket": "ixpe", "kind": "raw", "subject": "0001", "folder": str(spectra),
        "files": ["spec/events.fits", "spec/a_src.pha"], "settings": {"software": "v1.4", "processed": "2022"}}]}))
    assert cli.main(["ingest", "--store", "db", "--mapping", str(path), "--password-file", password_file]) == 0
    [done] = _lines(capsys)
    folder = f"raw/0001/{done['recipe_hash'][:8]}"
    s3 = store.admin_client("s3")
    head = s3.head_object(Bucket="ixpe", Key=f"{folder}/spec/events.fits")
    assert head["ContentLength"] == big.stat().st_size and "-3" in head["ETag"]  # three parts
    assert head["Metadata"]["sha256"] == hashlib.sha256(big.read_bytes()).hexdigest()
    recipe = rc.RecipeFile.model_validate_json(s3.get_object(
        Bucket="ixpe", Key=done["record"]["key"], VersionId=done["record"]["version_id"])["Body"].read()).recipe
    assert recipe["settings"]["software"] == "v1.4" and recipe["settings"]["processed"] == "2022"
    assert sorted(recipe["settings"]["files"]) == ["spec/a_src.pha", "spec/events.fits"]
    report = asyncio.run(rc.reconcile(store, run="t-parts"))
    assert report.verified == [done["recipe_hash"]] and not report.flags and not report.promoted
    # The plan shows the settings; a setting named as the ingest's own, or not written as text, is refused.
    assert cli.main(["ingest", "--store", "db", "--mapping", str(path), "--check"]) == 0
    assert _lines(capsys)[0]["settings"] == {"software": "v1.4", "processed": "2022"}
    for settings, said in (({"folder": "mine"}, "which the ingest writes itself"),
                           ({"files": "mine"}, "which the ingest writes itself"),
                           ({"version": 1.0}, "valid string")):
        path.write_text(json.dumps({"products": [{"bucket": "ixpe", "kind": "raw", "subject": "0001",
                                                  "folder": str(spectra), "files": ["spec/a.rmf"],
                                                  "settings": settings}]}))
        with pytest.raises(ingest.IngestError, match=said):
            ingest.load_mapping(path)


class _NoVersionAnswer:
    """The store's client, its completion answered as a timed-out, retried completion is (status 200, no
    version id; measured on versitygw 1.11 with a 9.7 GB file on NFS)."""

    def __init__(self, s3):
        self._s3 = s3

    def __getattr__(self, name):
        return getattr(self._s3, name)

    def complete_multipart_upload(self, **kwargs):
        answer = self._s3.complete_multipart_upload(**kwargs)
        return {k: v for k, v in answer.items() if k != "VersionId"}


def test_a_large_upload_is_aborted_on_other_bytes_and_a_known_key_or_a_versionless_answer_is_read_back(
        store, tmp_path, monkeypatch):
    import hashlib

    monkeypatch.setattr(ingest, "SINGLE_PUT_MAX", 6 << 20)
    monkeypatch.setattr(ingest, "PART_SIZE", 5 << 20)
    path = tmp_path / "events.fits"
    path.write_bytes(bytes(range(256)) * (11 << 12))
    sha, size = hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size
    s3, meta = ingest.client(store), {"recipe-hash": "0" * 64}

    def open_uploads():
        return s3.list_multipart_uploads(Bucket="ixpe").get("Uploads", [])

    # Bytes that are not the planned ones: the upload is aborted, nothing stays.
    with pytest.raises(ingest.IngestError, match="changed during its upload"):
        ingest._put(s3, "ixpe", "raw/x/k1", path, "f" * 64, size, meta)
    assert open_uploads() == [] and _versions(store) == []
    # An answer without its version id: the key is read back and its version given.
    first = ingest._put(_NoVersionAnswer(s3), "ixpe", "raw/x/k1", path, sha, size, meta)
    listed = s3.list_object_versions(Bucket="ixpe", Prefix="raw/x/k1")["Versions"]
    assert [v["VersionId"] for v in listed] == [first["version_id"]] and first["sha256"] == sha
    # The key again with the same bytes (412 on completion): the version stored is reused.
    assert ingest._put(s3, "ixpe", "raw/x/k1", path, sha, size, meta) == first
    assert open_uploads() == [] and len(s3.list_object_versions(Bucket="ixpe", Prefix="raw/x/k1")["Versions"]) == 1
    # Other bytes at a known key are refused.
    other = tmp_path / "other.fits"
    other.write_bytes(bytes(range(255, -1, -1)) * (11 << 12))
    with pytest.raises(ingest.IngestError, match="already holds other bytes"):
        ingest._put(s3, "ixpe", "raw/x/k1", other, hashlib.sha256(other.read_bytes()).hexdigest(),
                    other.stat().st_size, meta)
    assert open_uploads() == []
