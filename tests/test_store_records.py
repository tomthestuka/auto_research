"""Receipts from the store's own records (step 0b S5): a call's writes and pinned reads (no LLM).

A real throwaway store (versitygw + its IAM + Postgres, ``store.launch.start`` in temporary folders),
a real toolkit registry over a temporary toolkit, the real ``SandboxToolProvider`` and
``ReceiptStore``; only the tool process is a fake, and it reaches the store over S3 with the unit's
own key, as a toolkit tool does.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os

import boto3
import pytest
from botocore.config import Config

from auto_research import toolkit_contract
from auto_research.agents.react import SandboxToolProvider
from auto_research.provenance import ReceiptStore
from auto_research.sandbox.registry import ToolkitRegistry
from auto_research.store import catalog
from auto_research.store import config as store_config
from auto_research.store import launch
from auto_research.store.config import RemoteStore
from auto_research.store.records import StoreRecords, parse_access_line, writable_area
from tests.test_sandbox import _FakeSandbox
from tests.test_store_servers import _no_dotenv, make_store  # noqa: F401 — fixtures

pytestmark = pytest.mark.skipif(
    not all(__import__("shutil").which(b) for b in ("versitygw", "initdb", "pg_ctl")),
    reason="versitygw and Postgres (initdb, pg_ctl) are not installed in this environment")

UNIT, RUN = "analyst_worker_0", "run-1"
MAKE, READ, SCRIBBLE, OPEN, ERASE, PRODUCE = (f"research_toolkit.demo_utils.{t}" for t in (
    "make", "read", "scribble", "open_path", "erase", "produce"))
RECORD = {"kind": "record"}
MANIFEST = {
    MAKE: {"module": "demo_utils.lib", "fn": "make", "description": "Make a product from a record.",
           "writes": ["db"], "algorithm_version": "1",
           "args_schema": {"type": "object", "required": ["src", "key"],
                           "properties": {"src": RECORD, "key": {"kind": "setting"},
                                          "plain": {"kind": "setting", "default": False}}}},
    READ: {"module": "demo_utils.lib", "fn": "read", "description": "Read records.",
           "args_schema": {"type": "object", "required": ["src"],
                           "properties": {"src": RECORD, "unpinned": {"kind": "setting", "default": False}}}},
    SCRIBBLE: {"module": "demo_utils.lib", "fn": "scribble", "description": "Write without declaring it.",
               "args_schema": {"type": "object", "required": ["key"], "properties": {"key": {"kind": "setting"}}}},
    ERASE: {"module": "demo_utils.lib", "fn": "erase", "description": "Remove a version.",
            "writes": ["db"], "algorithm_version": "1",
            "args_schema": {"type": "object", "required": ["key", "version"],
                            "properties": {"key": {"kind": "setting"}, "version": {"kind": "setting"}}}},
    PRODUCE: {"module": "demo_utils.lib", "fn": "produce", "description": "Claim, write and commit a product.",
              "writes": ["db"], "algorithm_version": "1",
              "args_schema": {"type": "object", "required": ["subject"],
                              "properties": {"src": RECORD, "aux": RECORD, "subject": {"kind": "setting"},
                                             "fail": {"kind": "setting", "default": False},
                                             "commit": {"kind": "setting", "default": True}}}},
    OPEN: {"module": "demo_utils.lib", "fn": "open_path", "description": "Open a file by path.",
           "args_schema": {"type": "object", "required": ["where"], "properties": {"where": {"kind": "path"}}}},
}


def _toolkit(root):
    pkg = root / "demo_utils"
    (pkg / "skills").mkdir(parents=True)
    (pkg / "skills" / "__init__.py").write_text(f"TOOL_MANIFEST = {MANIFEST!r}\n")
    (pkg / "__init__.py").write_text("from demo_utils.skills import TOOL_MANIFEST\n")
    return root


def _client(store, key, secret):
    return boto3.client("s3", endpoint_url=store.s3_endpoint, region_name="us-east-1", aws_access_key_id=key,
                        aws_secret_access_key=secret, config=Config(s3={"addressing_style": "path"}))


def _issued(store, unit):
    """``unit``'s producer keys, issued as the run issues them (``store.keys.issue``, item S4)."""
    from auto_research.store import keys

    return asyncio.run(keys.issue(store, unit, "producer", RUN))


def _wide_key(store, principal):
    """A second key the catalog names for ``principal``, under a policy wider than any role's (every
    S3 action): the records must catch what a role's policy would refuse, should one ever allow it."""
    iam = store.admin_client("iam")
    name = f"{principal}_wide"
    iam.create_user(UserName=name)
    iam.put_user_policy(UserName=name, PolicyName="wide", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["s3:*"], "Resource": [
            f"arn:aws:s3:::{b}{s}" for b in store.buckets for s in ("", "/*")]}]}))
    key = iam.create_access_key(UserName=name)["AccessKey"]

    async def record():
        import psycopg

        async with await psycopg.AsyncConnection.connect(store.admin_dsn(), autocommit=True) as conn:
            await catalog.record_credential(conn, key_id=key["AccessKeyId"], agent=principal, run=RUN)

    asyncio.run(record())
    return _client(store, key["AccessKeyId"], key["SecretAccessKey"])


class World:
    """One store, one unit's provider over it, and the unit's and another unit's S3 clients."""

    def __init__(self, tmp_path, make_store, **store_fields):
        root = _toolkit(tmp_path / "tk")
        self.store = make_store(extra_config={"toolkits": [{
            "name": "research_toolkit", "root": str(root), "template_package": "demo_utils",
            "authoring_branch": "ai", "default_env": "analysis"}]}, **store_fields)
        toolkit_contract.bind({"toolkits": [{"name": "research_toolkit", "root": str(root),
                                             "template_package": "demo_utils", "authoring_branch": "ai",
                                             "default_env": "analysis"}]})
        launch.start(self.store)
        self.admin = self.store.admin_client("s3")
        for bucket in self.store.buckets:
            self.admin.create_bucket(Bucket=bucket)
            self.admin.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        from auto_research.graph.runtime import _store_records

        self.credentials = _issued(self.store, UNIT)
        self.mine = _client(self.store, self.credentials.access_key_id, self.credentials.secret_access_key)
        theirs = _issued(self.store, "analyst_worker_1")
        self.other = _client(self.store, theirs.access_key_id, theirs.secret_access_key)
        self.wide = None  # a wider key of this unit's, made by the tests that need one
        self.registry = ToolkitRegistry(root)
        self.receipts = ReceiptStore(tmp_path / "receipts")
        from auto_research.store import keys

        self.reconciler = asyncio.run(keys.issue(self.store, "reconciler", "reconciler", RUN))
        # as `attach_worker_runtime` builds it, with the runtime's reconciler login (`run_reconciler`)
        self.records = _store_records(self.credentials, self.reconciler)
        self.during = None  # what another unit does while the call runs
        self.sandbox = _FakeSandbox(tool_fn=self._tool)
        work = tmp_path / "work"
        work.mkdir(exist_ok=True)
        self.provider = SandboxToolProvider(self.registry, self.sandbox, cwd=work, receipts=self.receipts,
                                            receipt_source=UNIT, store_records=self.records)

    # -- the tool process (a fake: what a toolkit tool does over S3 with the unit's key) --------

    def _tool(self, module, fn, args):
        if self.during is not None:
            self.during()
        s3 = self.wide or self.mine
        if fn in ("make", "read"):
            records = args["src"] if isinstance(args["src"], list) else [args["src"]]
            for r in records:
                kwargs = {} if args.get("unpinned") else {"VersionId": r["version_id"]}
                try:
                    s3.get_object(Bucket=r["bucket"], Key=r["key"], **kwargs)["Body"].read()
                except Exception as exc:  # noqa: BLE001 — the tool fails as a process does: exit 1
                    return {"exit_code": 1, "stdout": "", "stderr": str(exc)}
        if fn == "make":
            condition = {} if args.get("plain") else {"IfNoneMatch": "*"}
            s3.put_object(Bucket="data", Key=args["key"], Body=b"product bytes", **condition)
        if fn == "scribble":
            s3.put_object(Bucket="data", Key=args["key"], Body=b"scribbled")
        if fn == "produce":
            return self._produce(args)
        if fn == "erase":
            if args["version"] == "*":  # a bulk delete
                s3.delete_objects(Bucket="data", Delete={"Objects": [{"Key": args["key"]}]})
            else:
                s3.delete_object(Bucket="data", Key=args["key"], VersionId=args["version"])
        return {"exit_code": 0, "stdout": "done"}

    def _produce(self, args):
        """What a toolkit tool does for a product (item S8's helper): claim the recipe with the unit's
        catalog login, write create-once under the recipe's key, commit."""
        import psycopg
        from psycopg.types.json import Jsonb

        inputs = [args["src"]] if args.get("src") else []
        recipe = {"kind": "k", "subject": args["subject"], "tool": PRODUCE, "algorithm_version": "1",
                  "settings": {}, "inputs": inputs}
        with psycopg.connect(self.credentials.dsn(), autocommit=True) as conn:
            claim = conn.execute("SELECT catalog.claim(%s)", (Jsonb(recipe),)).fetchone()[0]
            body = f"product of {args['subject']}".encode()
            key = f"k/{args['subject']}/{claim['recipe_hash'][:8]}/out.dat"
            for r in inputs + ([args["aux"]] if args.get("aux") else []):  # `aux`: read, never a recipe input
                self.mine.get_object(Bucket=r["bucket"], Key=r["key"], VersionId=r["version_id"])["Body"].read()
            version = self.mine.put_object(Bucket="data", Key=key, Body=body, IfNoneMatch="*")["VersionId"]
            if args.get("commit") is False:  # dies before its commit: the claim stays, the bytes are stored
                return {"exit_code": 1, "stdout": key}
            conn.execute("SELECT catalog.commit(%s, %s, %s, %s)", (claim["recipe_hash"], Jsonb([{
                "store": "db", "bucket": "data", "key": key, "version_id": version, "sha256": sha(body),
                "size": len(body)}]), "0" * 40, "fp"))
        return {"exit_code": 1 if args.get("fail") else 0, "stdout": key}

    def later_run(self, tmp_path, name="later"):
        """A later run reading the same store: a fresh receipt store (no in-run writer to link)."""
        self.receipts = ReceiptStore(tmp_path / f"receipts-{name}")
        self.provider.receipts = self.receipts

    def call(self, kind, args):
        name = next(d["name"] for d in self.registry.tool_definitions() if self.registry.kind_for_function(d["name"]) == kind)
        out = asyncio.run(self.provider.execute(name, args))
        return out, (self.receipts.get(f"{UNIT}:r{len(self.receipts.receipts(kind='result'))}")
                     if out.receipt_call_id else None)

    def seed(self, key, body=b"seed bytes", bucket="data"):
        return self.admin.put_object(Bucket=bucket, Key=key, Body=body)["VersionId"]


@pytest.fixture
def world(tmp_path, make_store):
    made = World(tmp_path, make_store, buckets=["data", "synth"], synthetic_buckets=["synth"])
    yield made
    store_config.reset()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_a_calls_writes_and_pinned_reads_are_on_its_receipt_with_the_runtimes_hash(world):
    v0 = world.seed("k/s1/in.txt")
    out, receipt = world.call(MAKE, {"src": {"store": "db", "bucket": "data", "key": "k/s1/in.txt"},
                                     "key": "k/s1/h1/out.txt"})
    assert not out.is_error, out.text
    (written,) = receipt.writes
    assert (written.store, written.bucket, written.key, written.sha256, written.size) == (
        "db", "data", "k/s1/h1/out.txt", sha(b"product bytes"), len(b"product bytes"))
    assert written.declared and written.overwrote is None and not written.delete_marker
    (read,) = receipt.inputs
    assert read.basis == "object" and read.object.version_id == v0 and read.sha256 == sha(b"seed bytes")
    assert receipt.flags == []
    assert f"wrote db data/k/s1/h1/out.txt version {written.version_id}" in out.text
    # A later call reading that version names the call that wrote it as its parent.
    out, child = world.call(READ, {"src": {"store": "db", "bucket": "data", "key": "k/s1/h1/out.txt"}})
    assert child.inputs[0].receipt == receipt.handle and receipt.handle in child.parents


def test_an_overwrite_is_flagged_on_the_write(world):
    before = world.seed("k/s1/h1/out.txt", b"first")
    out, receipt = world.call(MAKE, {"src": {"store": "db", "bucket": "data", "key": "k/s1/h1/out.txt"},
                                     "key": "k/s1/h1/out.txt", "plain": True})
    (written,) = receipt.writes
    assert written.overwrote == before and written.sha256 == sha(b"product bytes")
    assert f"over version {before}" in out.text


def test_a_write_the_manifest_does_not_declare_is_flagged_and_propagates(world):
    out, receipt = world.call(SCRIBBLE, {"key": "k/s2/x.txt"})
    (written,) = receipt.writes
    assert not written.declared and "undeclared_write" in receipt.flags
    assert "does not name in writes" in out.text
    _out, child = world.call(READ, {"src": {"store": "db", "bucket": "data", "key": "k/s2/x.txt"}})
    assert "undeclared_write" in child.flags  # through its parent


def test_another_units_write_in_the_same_window_is_not_this_calls(world):
    world.during = lambda: world.other.put_object(Bucket="data", Key="k/s3/theirs.txt", Body=b"theirs")
    world.seed("k/s3/in.txt")
    _out, receipt = world.call(MAKE, {"src": {"store": "db", "bucket": "data", "key": "k/s3/in.txt"},
                                      "key": "k/s3/mine.txt"})
    assert [w.key for w in receipt.writes] == ["k/s3/mine.txt"] and receipt.flags == []


def test_a_removed_version_is_flagged(world):
    world.wide = _wide_key(world.store, world.credentials.principal)  # a role's policy denies it
    version = world.seed("k/s4/a.txt")
    out, receipt = world.call(ERASE, {"key": "k/s4/a.txt", "version": version})
    assert "undeclared_write" in receipt.flags and f"removed db data/k/s4/a.txt version {version}" in out.text


def test_a_path_into_a_store_folder_is_refused(world):
    out, receipt = world.call(OPEN, {"where": str(world.store.root / "data" / "k")})
    assert out.is_error and "inside the folders of the store db" in out.text and receipt is None
    assert world.sandbox.tool_calls == []


def test_a_version_the_store_does_not_hold_is_refused_before_the_call(world):
    world.seed("k/s5/in.txt")
    out, _ = world.call(READ, {"src": {"store": "db", "bucket": "data", "key": "k/s5/in.txt",
                                       "version_id": "01M73NH5GR487V5R0HX60BHKKN"}})
    assert out.is_error and "holds no readable version" in out.text and world.sandbox.tool_calls == []
    out, _ = world.call(READ, {"src": {"store": "db", "bucket": "data", "key": "k/s5/none.txt"}})
    assert out.is_error and "holds no object" in out.text and world.sandbox.tool_calls == []


def test_a_read_in_a_synthetic_bucket_marks_the_result(world):
    world.seed("k/s6/fixture.txt", bucket="synth")
    _out, receipt = world.call(READ, {"src": {"store": "db", "bucket": "synth", "key": "k/s6/fixture.txt"}})
    assert receipt.flags == ["synthetic"] and receipt.inputs[0].object.bucket == "synth"


def test_an_unpinned_read_leaves_the_result_unverified(world):
    world.wide = _wide_key(world.store, world.credentials.principal)  # a role's policy refuses it
    world.seed("k/s7/in.txt")
    out, receipt = world.call(READ, {"src": {"store": "db", "bucket": "data", "key": "k/s7/in.txt"},
                                     "unpinned": True})
    assert "unverified" in receipt.flags and "without a version the log names" in out.text


def test_bytes_changed_in_the_store_folder_leave_the_reader_unverified(world):
    world.seed("k/s8/in.txt")
    _out, made = world.call(MAKE, {"src": {"store": "db", "bucket": "data", "key": "k/s8/in.txt"},
                                   "key": "k/s8/out.txt"})
    (written,) = made.writes
    (world.store.root / "data" / "k" / "s8" / "out.txt").write_bytes(b"edited on disk")  # versitygw #2633
    out, child = world.call(READ, {"src": {"store": "db", "bucket": "data", "key": "k/s8/out.txt",
                                           "version_id": written.version_id}})
    assert "unverified" in child.flags and child.inputs == []  # the changed bytes fail the store's checksum
    assert "could not be hashed" in out.text


def test_a_read_whose_bytes_differ_from_what_its_writer_recorded_is_not_linked(tmp_path):
    """The runtime's own hash of a read disagrees with the hash its in-run writer recorded: no parent
    link, the reader marked unverified, a note naming both (``capture.receipt_call``)."""
    from auto_research.provenance.capture import receipt_call
    from auto_research.schemas.artifacts import InputFile, StoreObject

    store = ReceiptStore(tmp_path / "receipts")
    written = StoreObject(store="db", bucket="data", key="k/a.dat", version_id="V1", size=3, sha256="a" * 64)
    writer = store.mint(source=UNIT, kind="result", produced_by=MAKE, data="made", writes=[written])
    assert store.object_writer("db", "data", "k/a.dat", "V1").handle == writer.handle
    same = written.model_copy()
    linked = receipt_call(store, source=UNIT, produced_by=READ, args={}, text="read", failed=False,
                          reads=[InputFile(object=same, sha256=same.sha256, basis="object")]).result
    assert linked.parents == [writer.handle] and linked.flags == []
    changed = written.model_copy(update={"sha256": "b" * 64})
    out = receipt_call(store, source=UNIT, produced_by=READ, args={}, text="read", failed=False,
                       reads=[InputFile(object=changed, sha256=changed.sha256, basis="object")])
    assert out.result.parents == [] and out.result.flags == ["unverified"]
    assert any("differs from what" in note and writer.handle in note for note in out.notes)


def test_a_bulk_delete_claims_no_other_units_write(world):
    world.seed("k/s11/old.txt")
    world.during = lambda: world.other.put_object(Bucket="data", Key="k/s11/theirs.txt", Body=b"theirs",
                                                  IfNoneMatch="*")
    _out, receipt = world.call(ERASE, {"key": "k/s11/old.txt", "version": "*"})
    assert all(w.key != "k/s11/theirs.txt" for w in receipt.writes)


def test_two_units_writing_one_key_in_one_window_leave_the_call_unverified(world):
    world.seed("k/s12/in.txt")
    world.during = lambda: world.other.put_object(Bucket="data", Key="k/s12/same.txt", Body=b"theirs")
    out, receipt = world.call(MAKE, {"src": {"store": "db", "bucket": "data", "key": "k/s12/in.txt"},
                                     "key": "k/s12/same.txt", "plain": True})
    assert receipt.writes == [] and "unverified" in receipt.flags and "written also by another key" in out.text


def test_a_log_line_this_reader_cannot_read_leaves_the_call_unverified(world):
    out, receipt = world.call(SCRIBBLE, {"key": "k/s13/a\nb.txt"})  # the key's newline splits its log line
    assert "unverified" in receipt.flags and "cannot read" in out.text


def test_a_store_without_a_read_record_takes_the_pinned_records_as_reads(world):
    v0 = world.seed("k/s9/in.txt")
    remote = RemoteStore(name="db", kind="remote", endpoint=world.store.s3_endpoint, buckets=("data", "synth"),
                         catalog_dsn="host=127.0.0.1 dbname=db user=ar_admin", admin_env=world.store.admin_env)
    world.provider.store_records = StoreRecords(agent=UNIT, run=RUN, writable=writable_area(["db"], [remote]),
                                                stores=[remote])
    world.provider.record_versions = world.provider.store_records.current_version
    _out, receipt = world.call(MAKE, {"src": {"store": "db", "bucket": "data", "key": "k/s9/in.txt"},
                                      "key": "k/s9/out.txt"})
    assert [i.object.version_id for i in receipt.inputs] == [v0]
    assert [w.key for w in receipt.writes] == ["k/s9/out.txt"]  # every listed change is the call's


# -- the access log, line by line (pure) -----------------------------------------------------------

_LINE = ("admin data [10/November/2026:15:23:54 -0600] 127.0.0.1 AKIAUNIT REQ1 s3_GetObject {key} {uri} "
         "200 - 0 0 0 0 - Boto3/1.43 ua/2.1 os/linux - HOST SigV4 - AuthHeader s3.us-east-1 - arn Yes")


def test_a_key_with_spaces_is_read_from_the_escaped_uri():
    line = parse_access_line(_LINE.format(key="a b/x.dat", uri="/data/a%20b/x.dat?versionId=V1"))
    assert (line.requester, line.bucket, line.key, line.version_id, line.status) == (
        "AKIAUNIT", "data", "a b/x.dat", "V1", 200)


def test_a_key_forging_another_uri_is_not_believed():
    forged = "x /data/other 200 - y"
    line = parse_access_line(_LINE.format(key=forged, uri="/data/x%20/data/other%20200%20-%20y?versionId=V2"))
    assert line.key == forged and line.version_id == "V2"


def test_a_bucket_request_has_no_key_and_a_header_is_no_request():
    line = parse_access_line(_LINE.format(key="-", uri="/data?delete"))
    assert line.key is None and line.parsed
    assert parse_access_line("log starts 2026-11-10 15:23:52 -0600 CST") is None


def test_an_object_input_renders_with_its_store_and_version():
    from auto_research.report.html import _input_html
    from auto_research.schemas.artifacts import InputFile, StoreObject

    obj = StoreObject(store="db", bucket="data", key="k/s/a.dat", version_id="V1", size=3, sha256="0" * 64)
    html = _input_html(InputFile(object=obj, sha256="0" * 64, basis="object"), {})
    assert "a.dat" in html and "data/k/s/a.dat" in html and "V1" in html


def test_no_obs_db_layout_rule_is_left_in_the_source():
    import auto_research

    src = os.path.dirname(auto_research.__file__)
    for folder in ("provenance", "store", "schemas"):
        for name in os.listdir(os.path.join(src, folder)):
            if name.endswith(".py"):
                text = open(os.path.join(src, folder, name), encoding="utf-8").read()
                for retired in ("catalog.json", "is_synthetic_path", "_catalog_entries", "hash_obsdb_entry"):
                    assert retired not in text, (name, retired)


def test_a_store_that_cannot_be_listed_before_the_call_refuses_it(world):
    launch.stop(world.store)
    out, receipt = world.call(SCRIBBLE, {"key": "k/s10/x.txt"})
    assert out.is_error and "This call was not run" in out.text and "did not list bucket" in out.text
    assert receipt is None and world.sandbox.tool_calls == []


# -- flags across runs (the catalog's product_flag) -------------------------------------------------


def _flags_of(world, recipe_hash):
    async def read():
        import psycopg

        async with await psycopg.AsyncConnection.connect(world.store.admin_dsn(), autocommit=True) as conn:
            return await catalog.product_flags(conn, recipe_hash)

    return asyncio.run(read())


def _product_of(world, key):
    import psycopg

    with psycopg.connect(world.store.admin_dsn(), autocommit=True) as conn:
        return conn.execute("SELECT recipe_hash FROM catalog.object WHERE key = %s", (key,)).fetchone()[0]


def test_a_flagged_calls_product_carries_its_flags_into_a_later_run(world, tmp_path):
    out, receipt = world.call(PRODUCE, {"subject": "s20", "fail": True})
    assert receipt.flags == ["failed"]
    key = receipt.writes[0].key
    stamped = _flags_of(world, _product_of(world, key))
    assert [(f["flag"], f["receipt"]) for f in stamped] == [("failed", f"{RUN}/{receipt.handle}")]
    assert stamped[0]["stamped_by"] == world.reconciler.principal  # the runtime's reconciler login
    clean_out, clean = world.call(PRODUCE, {"subject": "s21"})
    assert clean.flags == [] and _flags_of(world, _product_of(world, clean.writes[0].key)) == []

    world.later_run(tmp_path)
    out, reader = world.call(READ, {"src": {"store": "db", "bucket": "data", "key": key}})
    assert "failed" in reader.flags and reader.parents == []  # carried by the catalog, not a link
    assert "carries failed" in out.text


def test_an_ancestors_flag_reaches_a_reader_through_the_recipe(world, tmp_path):
    _out, first = world.call(PRODUCE, {"subject": "s22"})
    (made,) = first.writes
    _out, second = world.call(PRODUCE, {"subject": "s23", "src": {"store": "db", "bucket": "data", "key": made.key}})
    assert second.flags == []

    async def stamp():
        import psycopg

        async with await psycopg.AsyncConnection.connect(world.reconciler.dsn(), autocommit=True) as conn:
            return await catalog.stamp_flags(conn, _product_of(world, made.key), ["unverified"], "later/finding")

    assert asyncio.run(stamp()) == 1
    world.later_run(tmp_path)
    _out, reader = world.call(READ, {"src": {"store": "db", "bucket": "data", "key": second.writes[0].key}})
    assert reader.flags == ["unverified"]


def test_only_the_reconciler_stamps_and_a_stamp_is_checked(world):
    import psycopg

    _out, receipt = world.call(PRODUCE, {"subject": "s24"})
    recipe_hash = _product_of(world, receipt.writes[0].key)
    with psycopg.connect(world.credentials.dsn(), autocommit=True) as conn:  # the unit's producer login
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT catalog.stamp_flags(%s, %s, %s)", (recipe_hash, ["failed"], "r/x"))
    with psycopg.connect(world.reconciler.dsn(), autocommit=True) as conn:
        assert conn.execute("SELECT catalog.stamp_flags(%s, %s, %s)", (recipe_hash, ["failed", "failed"], "r/x")
                            ).fetchone()[0] == 1
        assert conn.execute("SELECT catalog.stamp_flags(%s, %s, %s)", (recipe_hash, ["failed"], "r/x")
                            ).fetchone()[0] == 0  # once per (product, flag, receipt)
        for flags, receipt, said in ((["fine"], "r/x", "flags are one or more"), ([], "r/x", "flags are one or more"),
                                     (["failed"], "", "name the receipt")):
            with pytest.raises(psycopg.errors.InvalidParameterValue, match=said):
                conn.execute("SELECT catalog.stamp_flags(%s, %s, %s)", (recipe_hash, flags, receipt))
        with pytest.raises(psycopg.errors.InvalidParameterValue, match="no product"):
            conn.execute("SELECT catalog.stamp_flags(%s, %s, %s)", ("0" * 64, ["failed"], "r/x"))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM catalog.product_flag")


def test_a_product_stored_but_never_committed_is_stamped_through_its_claim(world):
    """A tool that dies between its PUT and its commit: no object row records the write, but the
    unit's claim in the call's window names the product, so a later adoption inherits the flag."""
    _out, receipt = world.call(PRODUCE, {"subject": "s25", "commit": False})
    assert "failed" in receipt.flags
    import psycopg

    with psycopg.connect(world.store.admin_dsn(), autocommit=True) as conn:
        (recipe_hash, state), = conn.execute(
            "SELECT recipe_hash, state FROM catalog.product WHERE recipe->>'subject' = 's25'").fetchall()
    assert state == "claimed" and [f["flag"] for f in _flags_of(world, recipe_hash)] == ["failed"]


def test_a_stamp_that_cannot_reach_the_catalog_is_retried_with_the_next_call(world, monkeypatch):
    from auto_research.store import records as records_module

    calls = []
    original = records_module.StoreRecords._stamp

    def flaky(self, *args):
        calls.append(args)
        if len(calls) == 1:
            raise OSError("catalog away")
        return original(self, *args)

    monkeypatch.setattr(records_module.StoreRecords, "_stamp", flaky)
    out, failed = world.call(PRODUCE, {"subject": "s26", "fail": True})
    assert "could not be stamped" in out.text and _flags_of(world, _product_of(world, failed.writes[0].key)) == []
    world.call(PRODUCE, {"subject": "s27"})  # an unflagged call: the pending stamp lands here
    assert [f["flag"] for f in _flags_of(world, _product_of(world, failed.writes[0].key))] == ["failed"]


def test_product_flags_are_append_only_even_for_the_owner(world):
    import psycopg

    _out, receipt = world.call(PRODUCE, {"subject": "s28", "fail": True})
    with psycopg.connect(world.store.admin_dsn(), autocommit=True) as conn:  # the cluster's superuser
        for statement in ("UPDATE catalog.product_flag SET flag = 'failed'", "DELETE FROM catalog.product_flag",
                          "TRUNCATE catalog.product_flag"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
                conn.execute(statement)


def test_the_human_clears_a_flag_and_readers_stop_carrying_it_and_the_runtime_cannot(world, tmp_path, capsys):
    import psycopg

    from auto_research.store.__main__ import main

    _out, made = world.call(PRODUCE, {"subject": "s29", "fail": True})
    key = made.writes[0].key
    recipe_hash = _product_of(world, key)
    secret = tmp_path / "human.pw"  # the human's own file; no .env or runtime process holds it
    secret.write_text("a human secret\n")
    secret.chmod(0o600)
    with pytest.raises(SystemExit, match="password authentication failed"):  # made without a password
        main(["clear-flag", "--store", "db", "--recipe", recipe_hash, "--flag", "failed", "--reason", "x",
              "--password-file", str(secret)])
    main(["human-password", "--store", "db", "--password-file", str(secret)])
    pw = ["--password-file", str(secret)]
    with psycopg.connect(world.store.admin_dsn(), autocommit=True) as conn:  # the runtime's superuser admin
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="human's own login"):
            conn.execute("SELECT catalog.clear_flag(%s, 'failed', 'looks fine')", (recipe_hash,))
    with psycopg.connect(world.reconciler.dsn(), autocommit=True) as conn:  # the runtime's login
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT catalog.clear_flag(%s, 'failed', 'looks fine')", (recipe_hash,))
    with psycopg.connect(world.credentials.dsn(), autocommit=True) as conn:  # the unit's producer login
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT catalog.clear_flag(%s, 'failed', 'looks fine')", (recipe_hash,))
    with pytest.raises(SystemExit, match="carries no live flag unverified"):
        main(["clear-flag", "--store", "db", "--recipe", recipe_hash, "--flag", "unverified", "--reason", "x", *pw])
    with pytest.raises(SystemExit, match="no product"):
        main(["clear-flag", "--store", "db", "--recipe", "0" * 64, "--flag", "failed", "--reason", "x", *pw])
    with pytest.raises(SystemExit):
        main(["clear-flag", "--store", "db", "--recipe", recipe_hash, "--flag", "failed", "--reason", "  ", *pw])
    with psycopg.connect(world.reconciler.dsn(), autocommit=True) as conn:  # a second receipt's stamp
        conn.execute("SELECT catalog.stamp_flags(%s, %s, %s)", (recipe_hash, ["failed"], "run-2/analyst_worker_1:r2"))
    capsys.readouterr()
    main(["clear-flag", "--store", "db", "--recipe", recipe_hash, "--flag", "failed",
          "--reason", "the tool exited 1 after a good commit", *pw])
    printed = capsys.readouterr().out
    assert f"{RUN}/{made.handle}, run-2/analyst_worker_1:r2" in printed and "products made from this one" in printed
    history = _flags_of(world, recipe_hash)
    assert [(f["action"], f["flag"]) for f in history] == [("stamp", "failed"), ("stamp", "failed"), ("cleared", "failed")]
    assert history[2]["stamped_by"] == "ar_human"  # who: the human's own login
    assert history[2]["reason"] == "the tool exited 1 after a good commit"
    world.later_run(tmp_path)
    _out, reader = world.call(READ, {"src": {"store": "db", "bucket": "data", "key": key}})
    assert reader.flags == []
    with psycopg.connect(world.reconciler.dsn(), autocommit=True) as conn:
        # A retry of a stamp already cleared adds nothing; a stamp from another receipt is live again.
        assert conn.execute("SELECT catalog.stamp_flags(%s, %s, %s)",
                            (recipe_hash, ["failed"], f"{RUN}/{made.handle}")).fetchone()[0] == 0
        assert conn.execute("SELECT flag FROM catalog.live_flag WHERE recipe_hash = %s", (recipe_hash,)).fetchall() == []
        conn.execute("SELECT catalog.stamp_flags(%s, %s, %s)", (recipe_hash, ["failed"], "run-3/analyst_worker_0:r1"))
        assert conn.execute("SELECT flag FROM catalog.live_flag WHERE recipe_hash = %s",
                            (recipe_hash,)).fetchall() == [("failed",)]


def test_carried_flags_are_not_stamped_and_a_clear_upstream_reaches_the_descendants(world, tmp_path):
    """Ruling 11-10: a call stamps on its products only the flags it earned itself; what it carried
    from an ancestor stays derived through ancestry, so clearing the ancestor clears its descendants."""
    from auto_research.store.__main__ import main

    _out, parent = world.call(PRODUCE, {"subject": "s30", "fail": True})
    parent_key = parent.writes[0].key
    world.later_run(tmp_path)
    _out, child = world.call(PRODUCE, {"subject": "s31", "src": {"store": "db", "bucket": "data", "key": parent_key}})
    assert "failed" in child.flags  # the receipt shows what it carried
    assert _flags_of(world, _product_of(world, child.writes[0].key)) == []  # but stamps nothing it carried
    _out, reader = world.call(READ, {"src": {"store": "db", "bucket": "data", "key": child.writes[0].key}})
    assert reader.flags == ["failed"]  # derived through the child's recipe input
    secret = tmp_path / "human.pw"
    secret.write_text("another secret\n")
    secret.chmod(0o600)
    main(["human-password", "--store", "db", "--password-file", str(secret)])
    main(["clear-flag", "--store", "db", "--recipe", _product_of(world, parent_key), "--flag", "failed",
          "--reason", "the parent's exit 1 came after a good commit", "--password-file", str(secret)])
    world.later_run(tmp_path, "after-clear")  # receipts already minted keep what they carried
    _out, again = world.call(READ, {"src": {"store": "db", "bucket": "data", "key": child.writes[0].key}})
    assert again.flags == []


def test_the_humans_pointer_override_runs_as_its_own_login(world, tmp_path):
    import psycopg

    from auto_research.store.__main__ import main

    _out, made = world.call(PRODUCE, {"subject": "s32"})
    recipe_hash = _product_of(world, made.writes[0].key)
    with psycopg.connect(world.reconciler.dsn(), autocommit=True) as conn:
        conn.execute("SELECT catalog.verify(%s)", (recipe_hash,))
    secret = tmp_path / "human.pw"
    secret.write_text("override secret\n")
    secret.chmod(0o600)
    main(["human-password", "--store", "db", "--password-file", str(secret)])
    main(["move-current", "--store", "db", "--bucket", "data", "--kind", "k", "--subject", "s32", "--recipe", recipe_hash,
          "--reason", "checked by hand", "--password-file", str(secret)])
    with psycopg.connect(world.store.admin_dsn(), autocommit=True) as conn:
        moved_by, = conn.execute("SELECT moved_by FROM catalog.current WHERE bucket = 'data' AND kind = 'k' AND subject = 's32'").fetchone()
    assert moved_by == "ar_human"


def test_a_carried_flag_no_recipe_input_yields_is_stamped(world, tmp_path):
    """A read that is no recipe input (a calibration, a choice of settings) leaves no ancestry to derive
    its flag from, so the product is stamped with it."""
    _out, flagged = world.call(PRODUCE, {"subject": "s33", "fail": True})
    world.later_run(tmp_path)
    _out, child = world.call(PRODUCE, {"subject": "s34", "aux": {"store": "db", "bucket": "data",
                                                                  "key": flagged.writes[0].key}})
    stamped = _flags_of(world, _product_of(world, child.writes[0].key))
    assert [f["flag"] for f in stamped] == ["failed"]


def test_the_runtimes_superuser_cannot_act_as_the_human(world, tmp_path):
    import psycopg

    _out, made = world.call(PRODUCE, {"subject": "s35", "fail": True})
    recipe_hash = _product_of(world, made.writes[0].key)
    with psycopg.connect(world.store.admin_dsn(), autocommit=True) as conn:
        conn.execute("SET SESSION AUTHORIZATION ar_human")  # an impersonation the superuser can make
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="not the human's own login"):
            conn.execute("SELECT catalog.clear_flag(%s, 'failed', 'x')", (recipe_hash,))


def test_the_human_password_file_is_the_humans_only_and_keeps_no_line_ending(world, tmp_path):
    from auto_research.store.__main__ import main

    secret = tmp_path / "human.pw"
    secret.write_text("windows secret\r\n")
    with pytest.raises(SystemExit, match="readable by others"):
        main(["human-password", "--store", "db", "--password-file", str(secret)])
    secret.chmod(0o600)
    main(["human-password", "--store", "db", "--password-file", str(secret)])
    import psycopg

    from auto_research.store.__main__ import _human_dsn

    with psycopg.connect(_human_dsn(world.store, "windows secret"), autocommit=True) as conn:
        assert conn.execute("SELECT session_user").fetchone()[0] == "ar_human"


def test_the_humans_login_is_made_again_on_an_existing_catalog_and_no_other_login_joins_owner(world):
    import psycopg

    from auto_research.store import catalog as catalog_module

    with psycopg.connect(world.store.admin_dsn(), autocommit=True) as conn:
        conn.execute("DROP ROLE ar_human")
    asyncio.run(catalog_module.apply_schema(world.store.admin_dsn()))
    asyncio.run(catalog_module.check_schema(world.store.admin_dsn()))
    with psycopg.connect(world.store.admin_dsn(), autocommit=True) as conn:
        conn.execute("CREATE ROLE sneaky LOGIN IN ROLE owner")
    try:
        with pytest.raises(catalog_module.CatalogError, match="login sneaky is in owner"):
            asyncio.run(catalog_module.check_schema(world.store.admin_dsn()))
    finally:
        with psycopg.connect(world.store.admin_dsn(), autocommit=True) as conn:
            conn.execute("DROP ROLE sneaky")
