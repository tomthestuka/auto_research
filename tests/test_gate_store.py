"""The CI pipeline's throwaway store (`store.ci`) and the return check's store rule
(`layer_b.returns.store`), Phase 3 step 0b S7.

The gate cases build a throwaway toolkit change as `tests/test_gate_returns.py` does and run the REAL
`run_gate` with its default Layer-B runner, under the real jail with no network. The configuration
names a store, so every pytest run of a toolkit with a store tool gets a fresh throwaway store (real
versitygw + IAM + Postgres, started by `store.launch`, keys issued by `store.keys`); the tool reaches
it from inside the jail with the run's producer key, as the toolkit's store helper does (claim,
create-only puts with the helper's metadata, `recipe.json` last, commit). What a watched call wrote is
judged from the store's own records (`store.records.StoreRecords.bracket`).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

from auto_research.sandbox import gate
from auto_research.store import ci, launch
from tests.test_gate_returns import BREACHES, _failed, _gate, _repo, analysis_env  # noqa: F401 — fixture
from tests.test_store_servers import _free_ports, _no_dotenv, make_store  # noqa: F401 — fixtures

pytestmark = pytest.mark.skipif(
    not all(shutil.which(b) for b in ("versitygw", "initdb", "pg_ctl")),
    reason="versitygw and Postgres (initdb, pg_ctl) are not installed in this environment")

#: The store the configuration declares (never contacted: the CI run mirrors its name and buckets).
MIRROR = {"name": "db", "kind": "remote", "endpoint": "http://127.0.0.1:9", "buckets": ["alpha", "beta"],
          "catalog_dsn": "host=nowhere dbname=db user=nobody",
          "admin_env": {"s3_access_key": "UNSET_A", "s3_secret_key": "UNSET_B", "catalog_password": "UNSET_C"}}

_WRITES = '"writes": ["db"], "algorithm_version": "1", '
_MANIFEST = (
    'TOOL_MANIFEST = {\n  "research_toolkit.demo_utils.my_tool": {\n'
    '    "module": "demo_utils.lib.demo_help", "fn": "my_tool",\n'
    '    "description": "d", %s"args_schema": {"properties": {\n'
    '        "src": {"kind": "record"}, "subject": {"kind": "setting"}}}}}\n'
)

#: The store helper's write path, as the toolkit's own helper takes it (S8): claim the recipe, store
#: each file create-only with the five metadata entries, write recipe.json last, commit.
_HELPER = '''\
"""Store reads and product writes over S3 and the catalog."""

import hashlib
import json
import os

import boto3
import psycopg
from botocore.config import Config
from psycopg.types.json import Jsonb


def s3():
    return boto3.client("s3", config=Config(s3={"addressing_style": "path"}))


def read(record):
    return s3().get_object(Bucket=record["bucket"], Key=record["key"], VersionId=record["version_id"])["Body"].read()


def store_product(bucket, kind, subject, tool, files, inputs=(), recipe_file=True, metadata=True, listed=None,
                  commit=True):
    client = s3()
    recipe = {"kind": kind, "subject": subject, "tool": tool, "algorithm_version": "1",
              "settings": {}, "inputs": [dict(i) for i in inputs]}
    with psycopg.connect(autocommit=True) as conn:
        answer = conn.execute("SELECT catalog.claim(%s)", (Jsonb(recipe),)).fetchone()[0]
        recipe_hash, generation = answer["recipe_hash"], answer["generation"]
        folder = "%s/%s/%s" % (kind, subject, recipe_hash[:8])
        meta = {"recipe-hash": recipe_hash, "producer": os.environ["PGUSER"],
                "toolkit-commit": "0" * 40, "env-fingerprint": "demo"}

        def put(name, body):
            sha = hashlib.sha256(body).hexdigest()
            done = client.put_object(Bucket=bucket, Key=folder + "/" + name, Body=body, IfNoneMatch="*",
                                     ChecksumAlgorithm="SHA256", Metadata=dict(meta, sha256=sha) if metadata else {})
            return {"store": os.environ["TOOLKIT_STORE"], "bucket": bucket, "key": folder + "/" + name,
                    "version_id": done["VersionId"], "sha256": sha, "size": len(body)}

        objects = [put(name, body) for name, body in files.items()]
        if recipe_file:
            shown = objects if listed is None else objects[:listed]
            body = json.dumps({"recipe_hash": recipe_hash, "generation": generation, "recipe": recipe,
                               "objects": [{k: o[k] for k in ("key", "version_id", "sha256", "size")} for o in shown]})
            objects.append(put("recipe.json", body.encode()))
        if commit:
            conn.execute("SELECT catalog.commit(%s, %s, %s, %s)", (recipe_hash, Jsonb(objects), "0" * 40, "demo"))
    return objects
'''

_TOOL = (
    "def my_tool(src, subject):\n"
    "    from demo_utils.lib.store_help import read, store_product\n"
    "    rows = read(src).decode().splitlines()\n"
    "    objects = store_product('alpha', 'rowcount', subject, 'research_toolkit.demo_utils.my_tool',\n"
    "                            %s, inputs=[src]%s)\n"
    "%s"
    "    return {'stdout': 'rows=%%d' %% len(rows)}\n"
)


def _tool(files="{'count.txt': ('%d\\n' % len(rows)).encode()}", options="", after=""):
    return _TOOL % (files, options, after)


#: The test's input: the brief's validation record, copied into the run's store by the pipeline.
_INPUT = (
    "    import json, os\n"
    "    src = json.loads(os.environ['TOOLKIT_STORE_VALIDATION'])[0]\n"
)
_CALL = _INPUT + "    res = my_tool(src, 's1')\n    assert res['stdout'] == 'rows=2'\n"

# (tool body, test body, writes declaration)
STORE_CLEAN = {
    "a_product_stored_through_the_helper": (_tool(), _CALL, _WRITES),
    "a_product_of_two_files": (_tool(files="{'count.txt': b'3\\n', 'rows.txt': b'a\\n'}"), _CALL, _WRITES),
    "a_raised_call_after_storing_part_of_a_product": (  # a failed production ends without its recipe file
        _tool(options=", recipe_file=False", after="    raise ValueError('no rows')\n"),
        _INPUT + "    import pytest\n    with pytest.raises(ValueError):\n        my_tool(src, 's1')\n    res = 1\n",
        _WRITES),
    "a_call_reading_back_what_it_stored": (  # its own write read back is no input (review of ruling A)
        _tool(after="    from demo_utils.lib.store_help import read\n"
                    "    assert read(objects[0]) == ('%d\\n' % len(rows)).encode()\n"), _CALL, _WRITES),
    "a_call_that_fans_out_to_a_thread": (  # the thread's call joins the open bracket, never waits on it
        "def my_tool(src, subject, inner=False):\n"
        "    import threading\n"
        "    from demo_utils.lib.store_help import read, store_product\n"
        "    rows = read(src).decode().splitlines()\n"
        "    if not inner:\n"
        "        side = threading.Thread(target=my_tool, args=(src, subject + 'b', True))\n"
        "        side.start()\n"
        "        side.join()\n"
        "    store_product('alpha', 'rowcount', subject, 'research_toolkit.demo_utils.my_tool',\n"
        "                  {'count.txt': ('%d\\n' % len(rows)).encode()}, inputs=[src])\n"
        "    return {'stdout': 'rows=%d' % len(rows)}\n",
        _CALL, _WRITES),
}

#: A test staging a synthetic product through the seed channel (main's ruling 2): its recipe file's
#: record and its objects' records.
_SYNTHETIC = (
    "    import base64, socket\n"
    "    link = socket.socket(socket.AF_UNIX)\n"
    "    link.connect(os.environ['TOOLKIT_STORE_SEED'])\n"
    "    files = {'rows.txt': base64.b64encode(b'p\\nq\\nr\\n').decode()}\n"
    "    link.sendall(json.dumps({'kind': 'rows', 'subject': 's9', 'bucket': 'beta', 'files': files}).encode() + b'\\n')\n"
    "    staged = json.loads(link.makefile().readline())\n"
    "    assert staged['record']['key'].endswith('/recipe.json'), staged\n"
)

# Main's ruling 1: input coverage is per call (the union of its products' inputs, its own products chained).
STORE_CLEAN.update({
    "two_products_from_two_inputs": (
        "def my_tool(src, subject, other=None):\n"
        "    from demo_utils.lib.store_help import read, store_product\n"
        "    rows = read(src).decode().splitlines()\n"
        "    more = read(other['objects'][0]).decode().splitlines()\n"
        "    store_product('alpha', 'rowcount', subject, 'research_toolkit.demo_utils.my_tool',\n"
        "                  {'count.txt': ('%d\\n' % len(rows)).encode()}, inputs=[src])\n"
        "    store_product('alpha', 'rowcount', subject + 'b', 'research_toolkit.demo_utils.my_tool',\n"
        "                  {'count.txt': ('%d\\n' % len(more)).encode()}, inputs=[other['record']])\n"
        "    return {'stdout': 'rows=%d more=%d' % (len(rows), len(more))}\n",
        _INPUT + _SYNTHETIC + "    res = my_tool(src, 's1', staged)\n    assert res['stdout'] == 'rows=2 more=3'\n",
        _WRITES),
    "a_product_chained_from_one_made_earlier_in_the_call": (
        _tool(after="    from demo_utils.lib.store_help import read\n"
                    "    first = [o for o in objects if o['key'].endswith('/recipe.json')][0]\n"
                    "    read(first)\n"
                    "    store_product('alpha', 'total', subject, 'research_toolkit.demo_utils.my_tool',\n"
                    "                  {'total.txt': b'2\\n'}, inputs=[{k: first[k] for k in ('store', 'bucket', 'key', "
                    "'version_id')}])\n"),
        _CALL, _WRITES),
})

STORE_BREACHES = {
    "a_product_written_over_an_earlier_version": (
        _tool(after="    from demo_utils.lib.store_help import s3\n"
                    "    s3().put_object(Bucket='alpha', Key=objects[0]['key'], Body=b'again')\n"),
        _CALL, _WRITES, "over the key's earlier version"),
    "a_write_in_a_store_the_manifest_does_not_name": (_tool(), _CALL, "", "names no db under \"writes\""),
    "a_product_with_no_recipe_file": (_tool(options=", recipe_file=False"), _CALL, _WRITES, "and 0 recipe.json"),
    "an_object_without_the_helpers_metadata": (
        _tool(options=", metadata=False"), _CALL, _WRITES, "without the metadata x-amz-meta-recipe-hash"),
    "a_recipe_file_that_leaves_an_object_out": (
        _tool(files="{'count.txt': b'3\\n', 'rows.txt': b'a\\n'}", options=", listed=1"), _CALL, _WRITES,
        "leaves out db alpha/rowcount/s1/"),
    "a_recipe_naming_another_tool": (
        _tool().replace("'research_toolkit.demo_utils.my_tool'", "'my_tool'"), _CALL, _WRITES,
        "names the tool 'my_tool'"),
    "an_object_whose_sha256_metadata_is_not_its_bytes": (
        "def my_tool(src, subject):\n"
        "    from demo_utils.lib.store_help import read, s3\n"
        "    rows = read(src).decode().splitlines()\n"
        "    meta = {'recipe-hash': '0' * 64, 'sha256': '1' * 64, 'producer': 'p', 'toolkit-commit': 'c',\n"
        "            'env-fingerprint': 'e'}\n"
        "    s3().put_object(Bucket='alpha', Key='rowcount/s1/0000aaaa/count.txt', Body=b'3\\n', IfNoneMatch='*',\n"
        "                    Metadata=meta)\n"
        "    return {'stdout': 'rows=%d' % len(rows)}\n",
        _CALL, _WRITES, "which is not the sha256 of its bytes"),
    "a_raised_call_that_overwrote": (
        _tool(options=", recipe_file=False",
              after="    from demo_utils.lib.store_help import s3\n"
                    "    s3().put_object(Bucket='alpha', Key=objects[0]['key'], Body=b'again')\n"
                    "    raise ValueError('no rows')\n"),
        _INPUT + "    import pytest\n    with pytest.raises(ValueError):\n        my_tool(src, 's1')\n    res = 1\n",
        _WRITES, "over the key's earlier version"),
    # Ruling A: the catalog's account of the product.
    "a_product_claimed_and_never_committed": (
        _tool(options=", commit=False"), _CALL, _WRITES, "is claimed in the catalog when the call returns"),
    "a_recipe_whose_inputs_leave_out_a_read": (
        _tool().replace("inputs=[src]", "inputs=[]"), _CALL, _WRITES, "no product the call stored names it"),
}


@contextlib.contextmanager
def serving_real_store(folder: Path, buckets: list[str]):
    """A real local store on this host in ``folder``, standing for the one the configuration declares
    (a CI run never reaches it): ``(entry, store, put)``, ``entry`` its ``stores:`` entry, ``put(bucket,
    key, body)`` storing an object with the admin key and returning its record. Stopped on exit."""
    from auto_research.store.config import LocalStore

    (folder / "root").mkdir(parents=True)
    s3, iam, catalog = _free_ports(3)
    entry = {"name": "db", "kind": "local", "root": str(folder / "root"), "state_dir": str(folder / "state"),
             "socket_dir": tempfile.mkdtemp(prefix="ars-", dir="/dev/shm"),
             "ports": {"s3": s3, "iam": iam, "catalog": catalog}, "buckets": buckets,
             "admin_env": {"s3_access_key": "REAL_S3_KEY", "s3_secret_key": "REAL_S3_SECRET",
                           "catalog_password": "REAL_PG_PASSWORD"}}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("auto_research.env._ENV_FILE", folder / "absent.env")
        for name, value in {"REAL_S3_KEY": "realadmin", "REAL_S3_SECRET": "realsecret0123456789",
                            "REAL_PG_PASSWORD": "real pg secret"}.items():
            mp.setenv(name, value)
        try:
            real = LocalStore(**entry)
            launch.start(real)
            client = real.admin_client("s3")
            for bucket in real.buckets:
                client.create_bucket(Bucket=bucket)
                client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})

            def put(bucket, key, body):
                return {"store": "db", "bucket": bucket, "key": key,
                        "version_id": client.put_object(Bucket=bucket, Key=key, Body=body)["VersionId"]}

            yield entry, real, put
        finally:
            with contextlib.suppress(Exception):
                launch.stop(LocalStore(**entry))
            shutil.rmtree(entry["socket_dir"], ignore_errors=True)


@pytest.fixture(scope="module")
def real_store(tmp_path_factory):
    """The store the configuration declares, holding the brief's validation records: a plain object,
    and a product whose ``recipe.json`` lists two objects. ``{"entry", "store", "plain", "product"}``."""
    with serving_real_store(tmp_path_factory.mktemp("real"), ["alpha", "beta", "data"]) as (entry, real, put):
        plain = put("data", "k/s/a.dat", b"x\ny\n")
        members = [{**put("data", f"k/s/abcd1234/{n}", b), "sha256": hashlib.sha256(b).hexdigest(), "size": len(b)}
                   for n, b in (("one.txt", b"one\n"), ("two.txt", b"two\n"))]
        recipe = {"kind": "k", "subject": "s", "tool": "research_toolkit.demo_utils.split", "algorithm_version": "1",
                  "settings": {}, "inputs": [plain]}
        listing = {"recipe_hash": hashlib.sha256(json.dumps(recipe).encode()).hexdigest(), "generation": 1,
                   "recipe": recipe, "objects": [{k: m[k] for k in ("key", "version_id", "sha256", "size")}
                                                 for m in members]}
        product = put("data", "k/s/abcd1234/recipe.json", json.dumps(listing).encode())
        yield {"entry": entry, "store": real, "plain": plain, "product": product}


@pytest.fixture
def store_config(analysis_env, real_store, monkeypatch):
    """``set(**extra)``: the host's configuration as the gate reads it, declaring the real store."""

    def configure(**extra):
        store = extra.pop("store", real_store["entry"])  # read once: the gate reads its configuration often
        monkeypatch.setattr(gate, "_orchestration_config", lambda: {
            "env_externals": {"heasoft": {"root": "/opt/heasoft", "private": {"PFILES": ";"}}},
            "stores": [store], **extra})

    configure()
    return configure


def _store_repo(tmp_path, tool, test, writes, **kwargs):
    extra = {"demo_utils/skills/__init__.py": _MANIFEST % writes, "demo_utils/lib/store_help.py": _HELPER,
             **kwargs.pop("extra_files", {})}
    return _repo(tmp_path, tool, test, extra_files=extra, **kwargs)


def _run(repo, records):
    """``run_gate`` on ``repo`` for a brief naming ``records`` as its validation data."""
    import asyncio

    from auto_research.schemas import ValidationDataset
    from tests.test_gate import _spec

    return asyncio.run(gate.run_gate(repo, _spec(validation_data=[ValidationDataset(**r) for r in records])))


# ── the gate ──────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(STORE_CLEAN))
def test_a_product_stored_through_the_helper_passes_the_ci_pipeline(tmp_path, store_config, real_store, name):
    tool, test, writes = STORE_CLEAN[name]
    report = _run(_store_repo(tmp_path, tool, test, writes), [real_store["plain"]])
    assert not _failed(report), [c.detail for c in _failed(report)]
    assert report.passed, report.reason
    assert any(c.name == "layer_b.returns" and c.passed for c in report.layer_b), report.layer_b


@pytest.mark.parametrize("name", sorted(STORE_BREACHES))
def test_a_store_write_breach_fails_the_minting_job_by_name(tmp_path, store_config, real_store, name):
    tool, test, writes, needle = STORE_BREACHES[name]
    report = _run(_store_repo(tmp_path, tool, test, writes), [real_store["plain"]])
    bad = [c for c in _failed(report) if c.name == "layer_b.returns.store"]
    assert bad and not report.passed, [(c.name, c.detail) for c in _failed(report)]
    assert any(needle in c.detail and "research_toolkit.demo_utils.my_tool" in c.detail for c in bad), \
        [c.detail for c in bad]
    assert {j.name: j.status for j in report.jobs}["minting"] == "fail"


def test_every_check_has_a_seeded_breach():
    names = {c for _t, _s, c in BREACHES.values()} | {"layer_b.returns.store"}
    assert names == {f"layer_b.returns.{k}" for k in ("transmit", "figure", "receipted", "table", "number",
                                                       "placed", "store", "unclassified")}


def test_the_briefs_validation_records_reach_the_tests(tmp_path, store_config, real_store):
    """The brief's records are copied in from the real store: a plain object version, and a product's
    recipe file with every object it lists, the product committed and verified in the run's catalog."""
    test = ("    import json, os\n"
            "    import psycopg\n"
            "    from demo_utils.lib.store_help import read\n"
            "    records = json.loads(os.environ['TOOLKIT_STORE_VALIDATION'])\n"
            "    assert [r['key'] for r in records] == ['k/s/a.dat', 'k/s/abcd1234/recipe.json'], records\n"
            "    assert read(records[0]) == b'x\\ny\\n'\n"
            "    listing = json.loads(read(records[1]))\n"
            "    assert [read(dict(records[1], key=o['key'], version_id=o['version_id'])) for o in listing['objects']]"
            " == [b'one\\n', b'two\\n']\n"
            "    with psycopg.connect(autocommit=True) as conn:\n"
            "        states = conn.execute(\"SELECT state FROM catalog.product WHERE kind = 'k'\").fetchall()\n"
            "    assert states == [('verified',)], states\n"
            "    res = my_tool(records[0], 's1')\n")
    report = _run(_store_repo(tmp_path, _tool(), test, _WRITES), [real_store["plain"], real_store["product"]])
    assert report.passed, [(c.name, c.detail) for c in (*report.layer_a, *report.layer_b) if not c.passed]
    assert next(c for c in report.layer_b if c.name == "layer_b.validation_data").passed


def test_a_validation_record_the_real_store_lacks_fails_the_tests_job(tmp_path, store_config, real_store):
    report = _run(_store_repo(tmp_path, _tool(), _CALL, _WRITES), [dict(real_store["plain"], version_id="nope")])
    check = next(c for c in report.layer_b if c.name == "layer_b.validation_data")
    assert not check.passed and "holds no such object version" in check.detail and "k/s/a.dat" in check.detail
    assert not report.passed and {j.name: j.status for j in report.jobs}["tests"] == "fail"


def test_a_record_seeded_from_a_data_set_reaches_the_tool(tmp_path, store_config, real_store):
    """A data test asks the run's store for a data set's file and hands the record to the tool."""
    from tests.fixtures.toolkit import ROOT_CONFTEST

    rows = tmp_path / "rows"
    rows.mkdir()
    (rows / "rows.csv").write_text("a,b\n1,2\n")
    store_config(test_data={"demo_rows": str(rows)})
    test = ("import json\nimport os\nimport socket\n\nimport pytest\n\n"
            "from demo_utils.lib.demo_help import my_tool\n\n\n"
            '@pytest.mark.data("demo_rows")\n'
            "def test_my_tool():\n"
            "    link = socket.socket(socket.AF_UNIX)\n"
            "    link.connect(os.environ['TOOLKIT_STORE_SEED'])\n"
            "    link.sendall(json.dumps({'data': 'demo_rows', 'path': 'rows.csv', 'bucket': 'beta'}).encode() + b'\\n')\n"
            "    answer = json.loads(link.makefile().readline())\n"
            "    assert answer['record']['bucket'] == 'beta', answer\n"
            "    assert my_tool(answer['record'], 's1')['stdout'] == 'rows=2'\n")
    (tmp_path / "t").mkdir()
    repo = _store_repo(tmp_path / "t", _tool(), "    res = 1\n", _WRITES, base_files={"conftest.py": ROOT_CONFTEST},
                       extra_files={"demo_utils/tests/test_my_tool.py": test})
    report = _run(repo, [real_store["plain"]])
    assert report.passed, [(c.name, c.detail) for c in (*report.layer_a, *report.layer_b) if not c.passed]
    assert next(c for c in report.layer_b if c.name == "layer_b.data").detail == "1 data tests ran"


def test_a_toolkit_with_no_store_tool_runs_without_a_store(tmp_path, store_config):
    tool = "def my_tool(src, subject):\n    return {'stdout': 'done'}\n"
    test = ("    import os\n    assert 'TOOLKIT_STORE' not in os.environ and 'AWS_ENDPOINT_URL_S3' not in os.environ\n"
            "    res = my_tool({}, 's1')\n")
    manifest = _MANIFEST.replace('"kind": "record"', '"kind": "setting"') % ""
    report = _gate(_repo(tmp_path, tool, test, extra_files={"demo_utils/skills/__init__.py": manifest}))
    assert report.passed, report.reason


def test_the_ci_run_reaches_no_real_store(tmp_path, store_config, real_store, monkeypatch):
    """The real store runs on this host while the gate runs (and the CI run copies the brief's record
    from it): its endpoint, ports, sockets and folders are absent from the CI test run, which reaches
    its own throwaway store all the same. Its folders are even on the jail's writable allow-list here,
    which the gate's hermetic run leaves out."""
    from auto_research.sandbox import jail

    real = real_store["store"]
    monkeypatch.setattr(jail, "_config", lambda: {"agent_fs_allow": [str(real.root), str(real.state_dir)],
                                                  "agent_fs_readonly": []})
    paths = [str(real.root), str(real.state_dir), str(real.socket_dir)]
    ports = [real.ports.s3, real.ports.iam, real.ports.catalog]
    probe = (f"    for path in {paths!r}:\n"
             "        assert not os.path.exists(path), path\n"
             "    import socket\n"
             f"    for port in {ports!r}:\n"
             "        try:\n"
             "            socket.create_connection(('127.0.0.1', port), timeout=2).close()\n"
             "        except OSError:\n"
             "            continue\n"
             "        raise AssertionError(f'the CI run reached port {port} of the real store')\n"
             f"    assert os.environ['AWS_ENDPOINT_URL_S3'] != {real.s3_endpoint!r}\n")
    report = _run(_store_repo(tmp_path, _tool(), _CALL + probe, _WRITES), [real_store["plain"]])
    assert report.passed, [(c.name, c.detail) for c in (*report.layer_a, *report.layer_b) if not c.passed]
    assert launch.down(real) == [], "the real store stopped answering"


@pytest.mark.parametrize("body", [
    "    from demo_utils.lib.store_help import s3\n"
    "    s3().put_object(Bucket='alpha', Key='fixture/x.txt', Body=b'x')\n",  # the test's own write
    None,  # a write by the tool's thread after its call returned
], ids=["by_the_test", "after_the_call_returned"])
def test_a_version_written_outside_every_call_fails_the_run(tmp_path, store_config, real_store, body):
    """Ruling E: the store is swept after each run; a version no bracketed call wrote fails it."""
    tool = _tool(after="    import threading, time\n"
                       "    from demo_utils.lib.store_help import s3\n"
                       "    def late():\n"
                       "        time.sleep(1)\n"
                       "        s3().put_object(Bucket='alpha', Key='late/x.txt', Body=b'x')\n"
                       "    threading.Thread(target=late).start()\n") if body is None else _tool()
    test = _CALL + (body or "    import time\n    time.sleep(3)\n")
    report = _run(_store_repo(tmp_path, tool, test, _WRITES), [real_store["plain"]])
    bad = [c for c in _failed(report) if c.name == "layer_b.returns.store"]
    where = "late/x.txt" if body is None else "fixture/x.txt"
    assert any("written outside a call" in c.detail and where in c.detail for c in bad), \
        [(c.name, c.detail) for c in _failed(report)]
    assert not report.passed


def test_a_package_without_store_tools_runs_its_tests_without_a_store(tmp_path, store_config, real_store):
    """Ruling C: the earlier tests of a package declaring no store tool run with no store."""
    other = {"other_utils/__init__.py": "",
             "other_utils/tests/test_plain.py": "import os\n\n\ndef test_plain():\n"
                                                "    assert 'TOOLKIT_STORE' not in os.environ\n"}
    report = _run(_store_repo(tmp_path, _tool(), _CALL, _WRITES, base_files=other), [real_store["plain"]])
    assert report.passed, [(c.name, c.detail) for c in (*report.layer_a, *report.layer_b) if not c.passed]
    assert next(c for c in report.layer_b if c.name == "layer_b.regression").passed


def test_a_store_that_does_not_start_is_infrastructure_not_the_change(tmp_path, store_config, real_store,
                                                                       monkeypatch):
    """Ruling B: the run's throwaway store not starting is a fault outside the change (``passing``)."""
    (tmp_path / "a_file").write_text("")
    monkeypatch.setattr(ci, "PARENT_ROOT", tmp_path / "a_file")  # no folder can be made there
    report = _run(_store_repo(tmp_path, _tool(), _CALL, _WRITES), [real_store["plain"]])
    assert not report.passed and report.infrastructure == "passing", report.reason
    check = next(c for c in report.layer_b if c.name == gate.INFRA_STORE_START)
    assert "the throwaway store did not start" in check.detail and "lies outside it" in check.detail


def test_a_real_store_that_does_not_answer_is_infrastructure_likely_to_recur(tmp_path, store_config, monkeypatch):
    """Ruling B: the validation precheck reaching no real store is outside the change (``recurring``)."""
    for name in ("UNSET_A", "UNSET_B", "UNSET_C"):
        monkeypatch.setenv(name, "secret-of-a-store-nobody-serves")
    store_config(store={**MIRROR, "endpoint": f"http://127.0.0.1:{_free_ports(1)[0]}"})  # nothing answers there
    record = {"store": "db", "bucket": "alpha", "key": "k/s/a.dat", "version_id": "v1"}
    report = _run(_store_repo(tmp_path, _tool(), _CALL, _WRITES), [record])
    assert not report.passed and report.infrastructure == "recurring", report.reason
    check = next(c for c in report.layer_b if c.name == gate.INFRA_REAL_STORE)
    assert "did not answer" in check.detail


def test_the_gate_jail_binds_no_writable_allow_list_root(tmp_path, monkeypatch):
    """Hermetic: of the allow-list the gate's test run sees only the read-only roots."""
    from auto_research.sandbox import jail

    writable, readonly = tmp_path / "runs", tmp_path / "software"
    writable.mkdir()
    readonly.mkdir()
    monkeypatch.setattr(jail, "_config", lambda: {"agent_fs_allow": [str(writable), str(readonly)],
                                                  "agent_fs_readonly": [str(readonly)]})
    monkeypatch.setattr(gate, "_orchestration_config", lambda: {})
    seen = []
    monkeypatch.setattr(jail, "wrap", lambda argv, policy: seen.append(policy) or argv)
    gate._jailed_pytest(["python"], junit_path=str(tmp_path / "j" / "junit.xml"), worktree=tmp_path / "wt")
    assert str(readonly) in seen[0].ro
    assert str(writable) not in (*seen[0].ro, *seen[0].rw)


# ── the throwaway store itself ────────────────────────────────────────────────────────────────────


def _mirror():
    from auto_research.store.config import RemoteStore

    return RemoteStore(**MIRROR)


def _seed(store, **request):
    link = socket.socket(socket.AF_UNIX)
    link.connect(store.env[ci.ENV_SEED])
    link.sendall(json.dumps(request).encode() + b"\n")
    return json.loads(link.makefile().readline())


def test_the_throwaway_store_serves_seeds_and_two_keys_then_leaves_nothing(tmp_path):
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError

    data = tmp_path / "rows"
    (data / "sub").mkdir(parents=True)
    (data / "sub" / "rows.csv").write_text("a,b\n1,2\n")
    (tmp_path / "outside.csv").write_text("x\n")
    with ci.throwaway(_mirror(), data_sets={"rows": data}) as store:
        env = store.env
        assert env[ci.ENV_STORE] == "db" and env["PGHOST"] == str(store.socket_dir)
        assert store.store.buckets == ("alpha", "beta") and store.socket_dir.parent == ci.ci_parent()
        # The jail's folder: the relay, bracket, seed and Postgres sockets, never the IAM one.
        assert sorted(p.name for p in store.socket_dir.iterdir()) == sorted(
            ["s3.sock", "calls.sock", "seed.sock", store.store.catalog_socket.name])
        import psycopg

        with psycopg.connect(host=env["PGHOST"], port=env["PGPORT"], user=env["PGUSER"], password=env["PGPASSWORD"],
                             dbname=env["PGDATABASE"]) as conn:
            assert conn.execute("SELECT session_user").fetchone()[0] == env["PGUSER"]
        record = _seed(store, data="rows", path="sub/rows.csv", bucket="alpha")["record"]
        assert record["key"].startswith(f"{ci.SEED_KIND}/rows/") and record["key"].endswith("/sub/rows.csv")
        assert _seed(store, data="rows", path="sub/rows.csv", bucket="alpha")["record"] == record  # once per run
        def client(prefix=""):
            return boto3.client("s3", endpoint_url=env["AWS_ENDPOINT_URL_S3"], region_name=env["AWS_REGION"],
                                aws_access_key_id=env[prefix + "AWS_ACCESS_KEY_ID"],
                                aws_secret_access_key=env[prefix + "AWS_SECRET_ACCESS_KEY"],
                                config=Config(s3={"addressing_style": "path"}))

        import base64

        synthetic = _seed(store, kind="rows", subject="s1", bucket="beta",
                          files={"a/b.txt": base64.b64encode(b"one\n").decode()})
        assert synthetic["record"]["key"].startswith("rows/s1/") and synthetic["record"]["key"].endswith("/recipe.json")
        assert [o["key"].rsplit("/", 2)[-2:] for o in synthetic["objects"]] == [["a", "b.txt"]]
        assert _seed(store, kind="rows", subject="s1", bucket="beta",
                     files={"a/b.txt": base64.b64encode(b"one\n").decode()}) == synthetic  # once per run
        assert "standard base64" in _seed(store, kind="rows", subject="s1", bucket="beta", files={"x": "@@"})["error"]
        assert "kind and subject" in _seed(store, kind="a/b", subject="s", bucket="beta", files={"x": ""})["error"]
        b64 = base64.b64encode(b"x").decode()
        for files, needle in (({"a//b": b64}, "plain path parts"), ({"recipe.json": b64}, "none recipe.json"),
                              ({"a": b64, "a/b": b64}, "no folder of another"), ({" a": b64}, "plain path parts")):
            assert needle in _seed(store, kind="rows", subject="s1", bucket="beta", files=files)["error"], files
        both = _seed(store, data="rows", paths=["sub/rows.csv"], bucket="alpha")  # the toolkit fixture's form
        assert both["record"]["key"].endswith("/recipe.json") and both["objects"][0]["key"].endswith("/sub/rows.csv")
        link = socket.socket(socket.AF_UNIX)
        link.connect(store.env[ci.ENV_SEED])
        link.sendall(b"[1, 2]\n")
        assert 'send {"data"' in json.loads(link.makefile().readline())["error"]
        # Ruling A: a synthetic seed's product carries `synthetic`; a call reading it (bracketed by the
        # runtime's own StoreRecords) carries it too, whatever its bucket.
        bracket = socket.socket(socket.AF_UNIX)
        bracket.connect(env[ci.ENV_CALLS])
        answers = bracket.makefile("rb")
        bracket.sendall(b'{"call": "synthetic-read", "tool": "t"}\n')
        assert json.loads(answers.readline())["ok"]
        read = synthetic["objects"][0]
        client().get_object(Bucket=read["bucket"], Key=read["key"], VersionId=read["version_id"])["Body"].read()
        bracket.sendall(b'{"end": true}\n')
        assert json.loads(answers.readline())["ok"]
        seen = next(c for c in store.calls if c["call"] == "synthetic-read")
        assert "synthetic" in seen["carried"], seen
        assert store.sweep() == [], "seeded products are no write outside a call"
        for request, needle in (({"data": "other", "path": "x", "bucket": "alpha"}, "name a data set this CI pipeline provides"),
                                ({"data": "rows", "path": "../outside.csv", "bucket": "alpha"}, "name a file inside data set"),
                                ({"data": "rows", "path": "sub/rows.csv", "bucket": "gamma"}, "name a bucket of store db"),
                                ({"data": "rows", "bucket": "alpha"}, 'send {"data"')):
            assert needle in _seed(store, **request)["error"], request

        reader = client(ci.READER_PREFIX)
        body = reader.get_object(Bucket="alpha", Key=record["key"], VersionId=record["version_id"])["Body"].read()
        assert body == b"a,b\n1,2\n"
        with pytest.raises(ClientError):
            reader.put_object(Bucket="alpha", Key="k/s/x.txt", Body=b"x")
        client().put_object(Bucket="alpha", Key="k/s/x.txt", Body=b"x")
        held = [store.socket_dir, store.store.state_dir, store.store.root]
        # The admin secrets are the store object's own, never this process's environment.
        assert store.store.admin_s3()[1] not in os.environ.values()
        assert not [k for k in os.environ if k.startswith("AR_CI_")]
    assert not any(p.exists() for p in held), held
    assert launch.down(store.store)  # nothing answers any more


def test_a_tool_reading_a_store_without_a_record_argument_declares_it_under_reads(tmp_path):
    """Main's ruling 2: ``reads: [<store>]`` makes a package a store package; validated like writes."""
    from auto_research.provenance.inputs import spec_problems

    pkg = tmp_path / "demo_utils" / "skills"
    pkg.mkdir(parents=True)
    manifest = (_MANIFEST % '"reads": ["db"], ').replace('"kind": "record"', '"kind": "setting"')
    (pkg / "__init__.py").write_text(manifest)
    assert ci.store_tools(tmp_path).packages == {"demo_utils"}
    assert spec_problems("t", {"reads": ["db"], "args_schema": {"properties": {}}}) == []
    assert any("reads is" in p for p in spec_problems("t", {"reads": ["db", "db"], "args_schema": {"properties": {}}}))


def test_store_tools_reads_writes_and_record_arguments_from_the_manifests(tmp_path):
    pkg = tmp_path / "demo_utils" / "skills"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(_MANIFEST % _WRITES)
    key, entry = "research_toolkit.demo_utils.my_tool", ("demo_utils.lib.demo_help", "my_tool")
    assert ci.store_tools(tmp_path) == ({"demo_utils"}, {key: ["db"]}, {key: entry})
    (pkg / "__init__.py").write_text(_MANIFEST % "")
    assert ci.store_tools(tmp_path).packages == {"demo_utils"}  # a record argument alone
    (pkg / "__init__.py").write_text((_MANIFEST % "").replace('"kind": "record"', '"kind": "setting"'))
    assert ci.store_tools(tmp_path) == (frozenset(), {key: []}, {})


def test_the_mirror_is_the_default_store_else_the_first_and_none_without_a_block():
    other = {**MIRROR, "name": "aux"}
    assert ci.mirror_of({"stores": [other, MIRROR]}).name == "db"
    assert ci.mirror_of({"stores": [other]}).name == "aux"
    assert ci.mirror_of({}) is None


# ── ruling B on the lead's side: an infrastructure failure does not spend a submission ────────────


@pytest.mark.parametrize("kind, counted", [("passing", 1), ("recurring", 2)])
def test_an_infrastructure_failure_of_the_ci_pipeline_is_not_the_coders(tmp_path, monkeypatch, kind, counted):
    """Through the lead's real cycle (``tests/test_chain_advance.py``'s wiring): the first submission's
    CI pipeline reports a fault outside the change; a passing one is refunded (the lead submits again
    at no cost), one likely to recur counts. The report stands for the gate's (its own tests above
    produce both kinds); the cycle, the session and the submit tool are production's."""
    import tests.test_chain_advance as chain
    from auto_research.schemas import GATE_JOBS, CheckResult, GateJob, GateReport
    from tests.test_colead_failure import ONE

    real = chain._pass_gate
    reports = [GateReport(
        passed=False, jobs=tuple(GateJob(name=j, status="fail" if j == "tests" else "pass") for j in GATE_JOBS),
        layer_b=[CheckResult(name=gate.INFRA_STORE_START if kind == "passing" else gate.INFRA_REAL_STORE,
                             layer="B", passed=False, detail="the throwaway store did not start (boom)")],
        infrastructure=kind)]

    async def first_fails(worktree, spec):
        return reports.pop(0) if reports else await real(worktree, spec)

    monkeypatch.setattr(chain, "_pass_gate", first_fails)
    outcome, model, built, *_ = chain._cycle(tmp_path, monkeypatch, [[ONE], [chain.SUBMIT], [chain.SUBMIT],
                                                                     *chain.FINISH], verdicts=[True])
    (said,) = model.seen[2][1]
    assert "The CI pipeline could not judge the change: the throwaway store did not start (boom)" in said
    assert ("not counted against your budget" in said) == (kind == "passing"), said
    assert [r.status for r in outcome.authored] == ["committed"]
    assert built[0][1].submits_used == counted


# ── main's ruling B: a manifest names only configured stores ──────────────────────────────────────


def test_the_structural_job_refuses_a_store_name_the_configuration_lacks(tmp_path, store_config):
    manifest = _MANIFEST % '"writes": ["aux"], "algorithm_version": "1", "reads": ["db", "elsewhere"], '
    report = _gate(_store_repo(tmp_path, _tool(), _CALL, "", extra_files={"demo_utils/skills/__init__.py": manifest}))
    check = next(c for c in report.layer_a if c.name == "structural.argument_kinds")
    assert not check.passed and not report.passed
    assert "writes names the store 'aux'" in check.detail and "reads names the store 'elsewhere'" in check.detail
    assert "name one of db" in check.detail and "store 'db'" not in check.detail


def test_the_launch_refuses_a_toolkit_naming_a_store_the_configuration_lacks(make_store, tmp_path):
    """The preflight item `store_config` reads every declared toolkit's manifests as data."""
    from tests.test_store_servers import _check

    root = tmp_path / "kit"
    (root / "demo_utils" / "skills").mkdir(parents=True)
    (root / "demo_utils" / "skills" / "__init__.py").write_text(_MANIFEST % '"reads": ["aux"], ')
    kit = {"name": "research_toolkit", "root": str(root), "package_glob": "*_utils", "template_package": "demo_utils",
           "authoring_branch": "ai", "default_env": "demo"}
    make_store(extra_config={"toolkits": [kit]})
    message = _check("store_config")
    assert message and "reads names the store 'aux'" in message and "name one of db" in message, message
    (root / "demo_utils" / "skills" / "__init__.py").write_text(_MANIFEST % '"reads": ["db"], ')
    assert _check("store_config") is None


# ── leftovers: the life lock, the sweep (`ci-sweep`), stopping a store whose folder was removed ──────

_KILLED_GATE = """
import json, os, sys
from pathlib import Path
import auto_research.env as env
env._ENV_FILE = Path("/nonexistent.env")
from auto_research.store import ci
from auto_research.store.config import RemoteStore
ci.PARENT_ROOT = Path(sys.argv[2])
store = ci.CIStore(RemoteStore(**json.loads(sys.argv[1]))).start()
print(json.dumps([str(store.store.state_dir.parent), str(store.socket_dir), str(store.store.socket_dir)]), flush=True)
os._exit(0)  # as a gate process killed outright: no stop runs
"""


def _servers_naming(folder) -> list[int]:
    """This user's processes whose command line names ``folder``."""
    found = []
    for entry in Path("/proc").iterdir():
        try:
            if entry.name.isdigit() and str(folder) in (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode():
                found.append(int(entry.name))
        except OSError:
            continue
    return found


def _killed_gate() -> list[Path]:
    """A throwaway store started in a process that then dies without stopping it: its folders."""
    import subprocess
    import sys

    done = subprocess.run([sys.executable, "-c", _KILLED_GATE, json.dumps(MIRROR), str(ci.PARENT_ROOT)],
                          capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr
    left = [Path(p) for p in json.loads(done.stdout.strip().splitlines()[-1])]
    assert all(p.is_dir() for p in left) and _servers_naming(left[0]), "the killed gate left nothing to sweep"
    return left


@pytest.fixture
def own_parent(monkeypatch):
    """This test's own folder of throwaway stores (under /var/tmp, like the real one), so a sweep here
    meets only this test's stores; removed afterwards, every server in it ended."""
    root = Path(tempfile.mkdtemp(prefix="ar-ci-test-", dir="/var/tmp"))
    monkeypatch.setattr(ci, "PARENT_ROOT", root)
    yield root
    try:
        assert ci.sweep_leftovers() == [], "a store of this test left servers or folders behind"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_ci_sweep_cleans_a_killed_gates_leftovers_and_leaves_a_store_in_use_alone(own_parent, capsys):
    from auto_research.store.__main__ import main

    with ci.throwaway(_mirror()) as live:
        left = _killed_gate()
        main(["ci-sweep"])
        said = capsys.readouterr().out
        assert "ended leftover server pid" in said and f"removed {left[0]}" in said, said
        assert not any(p.exists() for p in left) and not _servers_naming(left[0])
        assert live.store.state_dir.is_dir() and launch.down(live.store) == [], "the sweep touched a store in use"
        assert (live.store.state_dir.parent / ci.LIFE_LOCK).is_file()
    main(["ci-sweep"])
    assert capsys.readouterr().out.strip() == "nothing left behind by the CI pipeline's stores"


def test_the_sweep_ends_a_stores_servers_and_nothing_else_naming_its_folders(own_parent):
    """Review of the sweep: a reader of a leftover store's log (`tail -f`) is no server; it survives."""
    import subprocess

    left = _killed_gate()
    reader = subprocess.Popen(["tail", "-f", str(left[0] / "state" / "logs" / "gateway.log")],
                              stdout=subprocess.DEVNULL)
    try:
        ci.sweep_leftovers()
        assert reader.poll() is None, "the sweep ended a process that only reads a store's log"
        assert not any(p.exists() for p in left)
    finally:
        reader.kill()
        reader.wait()


def test_every_throwaway_start_ends_leftover_servers_and_leaves_their_folders_to_ci_sweep(own_parent):
    """Main's ruling A: a start ends leftover servers only; their folders go with `ci-sweep`."""
    left = _killed_gate()
    with ci.throwaway(_mirror()):
        assert not _servers_naming(left[0]) and all(p.exists() for p in left)
    assert all(f"removed {p}" in ci.sweep_leftovers() for p in left[:1])
    assert not any(p.exists() for p in left)


def test_a_store_whose_folder_was_removed_under_it_still_stops_its_servers(own_parent):
    with ci.throwaway(_mirror()) as store:
        work = store.store.state_dir.parent
        assert _servers_naming(work)
        shutil.rmtree(work)  # removed from outside while the store runs
    deadline = __import__("time").monotonic() + 30
    while _servers_naming(work) and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.2)
    assert not _servers_naming(work), "the store's servers outlived it"
    assert not store.socket_dir.exists() and not store.store.socket_dir.exists()


def test_a_sweep_leaves_alone_a_store_named_after_it_listed_the_folder(own_parent, monkeypatch):
    """Review of the sweep (found live, two suites sharing a user's folder): another process's store,
    named between the sweep's folder listing and its server scan, keeps its servers."""
    real = ci._server_holders
    late: dict = {}

    def scan_after_a_store_started(parent):
        if "store" not in late:  # once: the store's own start sweeps too
            late["store"] = ci.throwaway(_mirror())  # named after the outer sweep listed `parent`
            late["live"] = late["store"].__enter__()
        return real(parent)

    monkeypatch.setattr(ci, "_server_holders", scan_after_a_store_started)
    try:
        ci.sweep_leftovers()
        monkeypatch.setattr(ci, "_server_holders", real)
        assert launch.down(late["live"].store) == [], "the sweep ended a store in use"
        assert late["live"].store.state_dir.is_dir()
    finally:
        monkeypatch.setattr(ci, "_server_holders", real)
        late["store"].__exit__(None, None, None)


def test_a_start_whose_port_another_store_took_starts_again_on_fresh_ports(own_parent, monkeypatch):
    """Review of the start (found in a concurrent stress run): a port chosen free and bound by another
    process before this store's server binds it answers the health check with that process's server;
    `launch.start` refuses it (its own server exits on the taken port) and the throwaway starts again on
    fresh ports."""
    import time

    with ci.throwaway(_mirror()) as first:
        taken = first.store.ports
        real_ports, real_open = ci._free_ports, launch._port_open
        calls = {"ports": 0, "open": 0}

        def ports_once_taken(n):
            calls["ports"] += 1
            return [taken.s3, *real_ports(2)] if calls["ports"] == 1 else real_ports(n)  # its S3 port taken

        def busy_check_passes_once(port):  # as if the other server bound the port just after the check
            calls["open"] += 1
            return False if calls["open"] == 1 else real_open(port)  # the S3 port, checked first

        monkeypatch.setattr(ci, "_free_ports", ports_once_taken)
        monkeypatch.setattr(launch, "_port_open", busy_check_passes_once)
        started = time.monotonic()
        with ci.throwaway(_mirror()) as second:
            assert calls["ports"] >= 2, "the start never tried again"
            assert second.store.ports.s3 != taken.s3
            second.store.admin_client("s3").list_buckets()  # its own gateway, its own key
        assert launch.down(first.store) == [], "the second start harmed the first store"
        assert time.monotonic() - started < 120


def test_a_half_made_store_folder_is_left_alone_until_its_grace_ends(own_parent):
    import fcntl
    import os

    parent = ci.ci_parent()
    fresh, old, held = (parent / ".new-fresh", parent / ".new-old", parent / ".new-held")
    for folder in (fresh, old, held):
        folder.mkdir()
        (folder / ci.LIFE_LOCK).write_text("")
    long_ago = __import__("time").time() - ci.SWEEP_GRACE_S - 10
    os.utime(old, (long_ago, long_ago))
    os.utime(held, (long_ago, long_ago))
    with open(held / ci.LIFE_LOCK) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # a store still being made, its lock held
        done = ci.sweep_leftovers()
        assert done == [f"removed {old}"], done
        assert fresh.is_dir() and held.is_dir()
    shutil.rmtree(fresh)
    shutil.rmtree(held)


def test_the_launch_notes_leftover_ci_folders_and_names_ci_sweep(own_parent):
    """Main's ruling 1: a notice (never a failure) with the count and size, naming the command."""
    from auto_research.preflight import PreflightWarning

    ci.check_ci_leftovers()  # nothing left: no notice
    left = _killed_gate()
    ci.sweep_leftovers(remove_folders=False)  # as a start does: its servers end, its folders stay
    with pytest.raises(PreflightWarning, match=r"3 folder\(s\) left by the CI pipeline's throwaway stores .* "
                                               r"MiB\); run `python -m auto_research.store ci-sweep`"):
        ci.check_ci_leftovers()
    assert sorted(ci.leftover_folders()) == sorted(left)
    ci.sweep_leftovers()
    ci.check_ci_leftovers()
