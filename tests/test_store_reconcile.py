"""The reconciler (``auto_research.store.reconcile``) on one throwaway store: real versitygw (+ IAM) and
Postgres started by ``store.launch.start`` in temporary folders, the catalog installed by the launch.

Producers are agent login roles issued through ``catalog.issue_role`` that claim, commit and fail through
the catalog's own SQL functions; their bytes go in with the admin S3 key (the per-agent keys are S4's).
Every test names its own subject, so the products of one test never meet another's in a promotion.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import shutil
import socket
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg import errors
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from auto_research.config_paths import CONFIG_DIR_ENV
from auto_research.preflight import Preflight, PreflightError, build_default_preflight
from auto_research.store import catalog, launch, reconcile
from auto_research.store import config as store_config
from auto_research import toolkit_contract
from tests.fixtures import toolkit as fixture

pytestmark = pytest.mark.skipif(
    not all(shutil.which(b) for b in ("versitygw", "initdb", "pg_ctl", "git")),
    reason="versitygw, Postgres (initdb, pg_ctl) and git are needed")

SECRETS = {"T_S3_KEY": "testadmin", "T_S3_SECRET": "testsecret0123456789", "T_PG_PASSWORD": "pg secret"}
ADMIN = {"s3_access_key": "T_S3_KEY", "s3_secret_key": "T_S3_SECRET", "catalog_password": "T_PG_PASSWORD"}
#: A manifest key of the fixture toolkit (registered through the configuration's ``toolkits:`` block).
TOOL = f"{fixture.TOOLKIT_NAME}.{fixture.PKG_NAME}.{fixture.TOOL_NAME}"
BUCKET = "alpha"
SYNTHETIC = "gamma"
KIND = "table"


def _free_ports(n: int) -> list[int]:
    sockets = [socket.socket() for _ in range(n)]
    for s in sockets:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in sockets]
    for s in sockets:
        s.close()
    return ports


@dataclass
class Env:
    store: store_config.LocalStore
    root: Path
    commit: str  # the fixture toolkit's authoring-branch HEAD
    off_branch: str  # a commit the toolkit's repository holds, unreachable from its authoring branch

    def s3(self):
        return self.store.admin_client("s3")

    def path(self, key: str) -> Path:
        """The file holding ``key``'s latest version in the gateway's root."""
        return self.store.root / BUCKET / key

    async def admin(self) -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(self.store.admin_dsn(), autocommit=True)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("reconcile")
    sockets = tempfile.mkdtemp(prefix="ars-", dir="/dev/shm")
    try:
        with pytest.MonkeyPatch.context() as mp:
            for name, value in SECRETS.items():
                mp.setenv(name, value)
            mp.setattr("auto_research.env._ENV_FILE", tmp / "absent.env")
            s3, iam, cat = _free_ports(3)
            (tmp / "root").mkdir()
            entry = {"name": "db", "kind": "local", "root": str(tmp / "root"), "state_dir": str(tmp / "state"),
                     "socket_dir": sockets, "ports": {"s3": s3, "iam": iam, "catalog": cat},
                     "buckets": [BUCKET, "beta", SYNTHETIC], "synthetic_buckets": [SYNTHETIC], "admin_env": ADMIN}
            toolkit_root = fixture.build_toolkit(tmp / "toolkit", env_base="astropy")
            config = fixture.write_config(tmp / "config", toolkit_root, analysis_prefix=tmp / "astropy",
                                          stores=[entry])
            mp.setenv(CONFIG_DIR_ENV, str(config))
            store_config.reset()
            toolkit_contract.reset()
            store = store_config.store()
            launch.start(store)
            try:
                client = store.admin_client("s3")
                for bucket in store.buckets:
                    client.create_bucket(Bucket=bucket)
                    client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
                head = fixture.git(toolkit_root, "rev-parse", "HEAD").strip()
                side = fixture.git(toolkit_root, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "side").strip()
                yield Env(store, tmp, head, side)
            finally:
                launch.stop(store)
                shutil.rmtree(sockets, ignore_errors=True)
                store_config.reset()
                toolkit_contract.reset()
    finally:  # also when the store never started
        shutil.rmtree(sockets, ignore_errors=True)


# ---------------------------------------------------------------- a producer, as the toolkit helper acts


class Producer:
    """An agent login in ``producer`` that claims, stores and commits through the real interfaces."""

    def __init__(self, env: Env, label: str = "w"):
        self.env, self.name, self.password = env, f"{label}_{uuid.uuid4().hex[:8]}", secrets.token_urlsafe(12)
        self.claims: dict[str, dict] = {}  # recipe hash -> catalog.claim's answer, with the recipe

    async def __aenter__(self) -> "Producer":
        async with await self.env.admin() as admin:
            await catalog.issue_role(admin, name=self.name, role_class="producer", run="t", password=self.password,
                                     valid_until=datetime.now(UTC) + timedelta(hours=1))
        self.conn = await psycopg.AsyncConnection.connect(
            make_conninfo(self.env.store.admin_dsn(), user=self.name, password=self.password), autocommit=True)
        return self

    async def __aexit__(self, *exc) -> None:
        await self.conn.close()

    async def claim(self, subject: str, *, tool: str = TOOL, n: int = 0, lease: str = "1 hour",
                    inputs: list[dict] = ()) -> str:
        recipe = {"kind": KIND, "subject": subject, "tool": tool, "algorithm_version": "1",
                  "settings": {"n": n}, "inputs": [{k: o[k] for k in ("store", "bucket", "key", "version_id")}
                                                   for o in inputs]}
        cur = await self.conn.execute("SELECT catalog.claim(%s, %s::interval)", (Jsonb(recipe), lease))
        answer = (await cur.fetchone())[0]
        assert answer["status"] == "claimed", answer
        self.claims[answer["recipe_hash"]] = {**answer, "recipe": recipe}
        return answer["recipe_hash"]

    def put(self, recipe_hash: str, data: bytes, *, name: str = "out.dat", meta: dict | None = None,
            multipart: bool = False, bucket: str = BUCKET) -> dict:
        """Store ``data`` create-once in the production's folder (``<kind>/<subject>/<short hash>[.<n>]/``,
        n the claim generation, omitted for 1) with the helper's metadata; the object row the producer
        would commit."""
        claim = self.claims[recipe_hash]
        folder = recipe_hash[:8] + (f".{claim['generation']}" if claim["generation"] > 1 else "")
        key = f"{KIND}/{claim['recipe']['subject']}/{folder}/{name}"
        sha = hashlib.sha256(data).hexdigest()
        metadata = {reconcile.META_RECIPE_HASH: recipe_hash, reconcile.META_SHA256: sha,
                    reconcile.META_PRODUCER: self.name, reconcile.META_TOOLKIT_COMMIT: self.env.commit,
                    reconcile.META_ENV_FINGERPRINT: "fp1"}
        metadata = {k: v for k, v in {**metadata, **(meta or {})}.items() if v is not None}
        s3 = self.env.s3()
        if multipart:
            up = s3.create_multipart_upload(Bucket=bucket, Key=key, Metadata=metadata, ChecksumAlgorithm="CRC32")
            part = s3.upload_part(Bucket=bucket, Key=key, UploadId=up["UploadId"], PartNumber=1, Body=data,
                                  ChecksumAlgorithm="CRC32")
            done = s3.complete_multipart_upload(
                Bucket=bucket, Key=key, UploadId=up["UploadId"], IfNoneMatch="*",
                MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": part["ETag"],
                                            "ChecksumCRC32": part["ChecksumCRC32"]}]})
            version = done["VersionId"]
        else:
            version = s3.put_object(Bucket=bucket, Key=key, Body=data, IfNoneMatch="*", ChecksumAlgorithm="SHA256",
                                    Metadata=metadata)["VersionId"]
        return {"store": "db", "bucket": bucket, "key": key, "version_id": version, "sha256": sha, "size": len(data)}

    def finish(self, recipe_hash: str, objects: list[dict], *, generation: int | None = None,
               listed: list[dict] | None = None) -> dict:
        """Write the production's recipe file LAST, listing ``listed`` (default ``objects``); its row."""
        claim = self.claims[recipe_hash]
        body = reconcile.RecipeFile(
            recipe_hash=recipe_hash, generation=generation or claim["generation"], recipe=claim["recipe"],
            objects=[{k: o[k] for k in ("key", "version_id", "sha256", "size")} for o in listed or objects])
        return self.put(recipe_hash, body.model_dump_json().encode(), name=reconcile.RECIPE_FILE)

    async def commit(self, recipe_hash: str, objects: list[dict], *, toolkit_commit: str | None = None) -> None:
        await self.conn.execute("SELECT catalog.commit(%s, %s, %s, 'fp1')",
                                (recipe_hash, Jsonb(objects), toolkit_commit or self.env.commit))

    async def fail(self, recipe_hash: str, reason: str) -> None:
        await self.conn.execute("SELECT catalog.fail(%s, %s)", (recipe_hash, reason))

    async def product(self, subject: str, data: bytes, *, n: int = 0, tool: str = TOOL, bucket: str = BUCKET,
                      inputs: list[dict] = (), toolkit_commit: str | None = None) -> tuple[str, dict]:
        recipe_hash = await self.claim(subject, n=n, tool=tool, inputs=inputs)
        obj = self.put(recipe_hash, data, bucket=bucket)
        objects = [obj] if bucket != BUCKET else [obj, self.finish(recipe_hash, [obj])]
        await self.commit(recipe_hash, objects, toolkit_commit=toolkit_commit)
        return recipe_hash, obj


async def _human(env: Env) -> psycopg.AsyncConnection:
    """A session as the human's own login (``catalog.HUMAN_ROLE``), its password set through the admin."""
    from auto_research.store.__main__ import _human_dsn

    async with await env.admin() as admin:
        await catalog.set_human_password(admin, "human secret")
    return await psycopg.AsyncConnection.connect(_human_dsn(env.store, "human secret"), autocommit=True)


async def _state(env: Env, recipe_hash: str) -> dict:
    async with await env.admin() as conn:
        return await catalog.product(conn, recipe_hash)


async def _current(env: Env, subject: str) -> dict | None:
    async with await env.admin() as conn:
        return await catalog.current(conn, BUCKET, KIND, subject)


def _flags(report, recipe_hash=None, action=None):
    return [f for f in report.flags if (recipe_hash is None or f.recipe_hash == recipe_hash)
            and (action is None or f.action == action)]


def _subject() -> str:
    return f"subj-{uuid.uuid4().hex[:8]}"


def _later(path: Path, seconds: float = 10) -> None:
    """Move a file's mtime past the second its row was written (LastModified has 1 s precision)."""
    t = time.time() + seconds
    os.utime(path, (t, t))


# ---------------------------------------------------------------- verify


def test_a_committed_product_whose_bytes_match_is_verified_and_becomes_current(env):
    async def go():
        subject = _subject()
        async with Producer(env) as w:
            h, _ = await w.product(subject, b"product bytes")
        report = await reconcile.reconcile(env.store, run="t-verify")
        assert h in report.verified and not _flags(report, h)
        assert (await _state(env, h))["state"] == "verified"
        assert (await _current(env, subject))["recipe_hash"] == h
        assert [p["recipe_hash"] for p in report.promoted if p["kind"] == KIND].count(h) == 1
    asyncio.run(go())


def test_a_recorded_sha256_the_bytes_do_not_have_fails_the_product(env):
    async def go():
        async with Producer(env) as w:
            h = await w.claim(_subject())
            obj = w.put(h, b"real bytes", meta={reconcile.META_SHA256: None})
            await w.commit(h, [{**obj, "sha256": "0" * 64}])
        report = await reconcile.reconcile(env.store, run="t-verify")
        assert "bytes hash to sha256" in report.failed[h]
        assert (await _state(env, h))["state"] == "failed"
        assert _flags(report, h, "mismatch")
    asyncio.run(go())


def test_the_stores_own_checksum_is_a_second_check(env):
    async def go():
        async with Producer(env) as w:
            h = await w.claim(_subject())
            obj = w.put(h, b"stored bytes", meta={reconcile.META_SHA256: None})
            row = {**obj, "sha256": hashlib.sha256(b"STORED BYTES").hexdigest()}
            await w.commit(h, [row])
        path = env.path(obj["key"])  # the bytes on disk become the row's; the store's checksum stays
        before = path.stat()
        path.write_bytes(b"STORED BYTES")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        report = await reconcile.reconcile(env.store, run="t-verify")
        assert "was stored with sha256" in report.failed[h]
    asyncio.run(go())


def test_metadata_naming_another_recipe_fails_the_product(env):
    async def go():
        async with Producer(env) as w:
            h = await w.claim(_subject())
            obj = w.put(h, b"bytes", meta={reconcile.META_RECIPE_HASH: "f" * 64})
            await w.commit(h, [obj])
        report = await reconcile.reconcile(env.store, run="t-verify")
        assert f"carries {reconcile.META_RECIPE_HASH} {'f' * 64}" in report.failed[h]
    asyncio.run(go())


@pytest.mark.parametrize("honest", [True, False])
def test_a_multipart_object_is_verified_by_the_runtimes_own_hash(env, honest):
    async def go():
        async with Producer(env) as w:
            h = await w.claim(_subject())
            obj = w.put(h, b"multipart body " * 50, multipart=True, meta={reconcile.META_SHA256: None})
            await w.commit(h, [obj if honest else {**obj, "sha256": "1" * 64}])
        head = env.s3().head_object(Bucket=BUCKET, Key=obj["key"], VersionId=obj["version_id"], ChecksumMode="ENABLED")
        assert head.get("ChecksumType") == "COMPOSITE"  # the store holds no full-object SHA-256
        report = await reconcile.reconcile(env.store, run="t-verify")
        if honest:
            assert h in report.verified
        else:
            assert "bytes hash to sha256" in report.failed[h] and _flags(report, h, "mismatch")
    asyncio.run(go())


def test_a_second_version_with_other_bytes_at_a_recorded_key_is_nondeterministic(env):
    async def go():
        subject = _subject()
        async with Producer(env) as w:
            h, obj = await w.product(subject, b"first output")
            await reconcile.reconcile(env.store, run="t-verify")
            assert (await _current(env, subject))["recipe_hash"] == h
            # an unconditioned PUT over the create-once key, carrying the same recipe hash
            env.s3().put_object(Bucket=BUCKET, Key=obj["key"], Body=b"second output",
                                Metadata={reconcile.META_RECIPE_HASH: h})
        report = await reconcile.reconcile(env.store, run="t-verify")
        assert "with other bytes" in report.nondeterministic[h]
        assert (await _state(env, h))["state"] == "nondeterministic"
        assert (await _current(env, subject))["recipe_hash"] is None  # demoted, nothing verified to fall back to
    asyncio.run(go())


# ---------------------------------------------------------------- sweep


def test_a_complete_orphan_of_an_expired_claim_is_adopted_then_verified(env):
    async def go():
        async with Producer(env) as w:
            h = await w.claim(_subject(), lease="1 second")
            obj = w.put(h, b"orphaned output")
            done = w.finish(h, [obj])
            await asyncio.sleep(1.2)
        report = await reconcile.reconcile(env.store, run="t-sweep")
        assert h in report.adopted and h in report.verified
        assert h in [p["recipe_hash"] for p in report.promoted]  # an adopted product has its bucket
        async with await env.admin() as conn:
            assert (await catalog.product(conn, h))["bucket"] == BUCKET
            rows = await catalog.objects(conn, h)
        assert [(r["key"], r["version_id"], r["sha256"]) for r in rows] == [
            (o["key"], o["version_id"], o["sha256"]) for o in (obj, done)]
    asyncio.run(go())


@pytest.mark.parametrize("stopped", ["before its recipe file", "with a recipe file listing more",
                                     "and then failed it"])
def test_an_incomplete_or_failed_production_is_never_adopted(env, stopped):
    async def go():
        async with Producer(env) as w:
            h = await w.claim(_subject(), lease="1 second")
            obj = w.put(h, b"first of two outputs")
            if stopped == "with a recipe file listing more":
                ghost = {**obj, "key": obj["key"] + ".second", "version_id": "never-stored"}
                w.finish(h, [obj], listed=[obj, ghost])
            elif stopped == "and then failed it":
                w.finish(h, [obj])
                await w.fail(h, "the tool crashed after its uploads")
            await asyncio.sleep(1.2)
        report = await reconcile.reconcile(env.store, run="t-sweep")
        assert h not in report.adopted
        reasons = [f.detail["reason"] for f in _flags(report, h, "orphan")]
        expected = {"before its recipe file": "no recipe.json among its versions",
                    "with a recipe file listing more": "which is not among the unrecorded versions",
                    "and then failed it": "its product is failed (final)"}[stopped]
        assert reasons and all(expected in r for r in reasons), reasons
        assert (await _state(env, h))["state"] == ("failed" if stopped == "and then failed it" else "claimed")
    asyncio.run(go())


@pytest.mark.parametrize("second", ["commits", "abandons complete"])
def test_a_takeover_writes_its_own_folder_and_the_abandoned_one_stays_flagged(env, second):
    async def go():
        subject = _subject()
        async with Producer(env, "wa") as a, Producer(env, "wb") as b:
            h = await a.claim(subject, lease="1 second")
            first = a.put(h, b"first production")
            a.finish(h, [first])  # complete, but its lease ran out before the commit
            await asyncio.sleep(1.2)
            h2 = await b.claim(subject, lease="1 second" if second == "abandons complete" else "1 hour")
            assert h2 == h and b.claims[h]["generation"] == 2
            mine = b.put(h, b"second production")  # its own folder: no create-once key meets a's
            assert mine["key"].split("/")[-2] == f"{h[:8]}.2" and first["key"].split("/")[-2] == h[:8]
            done = b.finish(h, [mine])
            if second == "commits":
                await b.commit(h, [mine, done])
            else:
                await asyncio.sleep(1.2)
        report = await reconcile.reconcile(env.store, run="t-takeover")
        assert h in report.verified and (h in report.adopted) == (second == "abandons complete")
        async with await env.admin() as conn:
            rows = await catalog.objects(conn, h)
        assert {r["version_id"] for r in rows} == {mine["version_id"], done["version_id"]}
        flagged = {f.detail["version_id"] for f in _flags(report, h, "orphan")}
        assert first["version_id"] in flagged  # the abandoned production stays, flagged
    asyncio.run(go())


def test_an_upload_under_a_live_claim_is_pending(env):
    async def go():
        async with Producer(env) as w:
            h = await w.claim(_subject())
            w.put(h, b"in flight")
            report = await reconcile.reconcile(env.store, run="t-sweep")
        assert h in report.pending and not _flags(report, h)
        assert (await _state(env, h))["state"] == "claimed"
    asyncio.run(go())


def test_orphans_whose_metadata_does_not_match_a_claim_are_flagged_once(env):
    async def go():
        async with Producer(env) as w:
            h = await w.claim(_subject(), lease="1 second")
            obj = w.put(h, b"someone else's", meta={reconcile.META_PRODUCER: "another_agent"})
            w.finish(h, [obj], )
            await asyncio.sleep(1.2)
        stray = env.s3().put_object(Bucket=BUCKET, Key=f"stray/{uuid.uuid4().hex}", Body=b"no metadata")
        first = await reconcile.reconcile(env.store, run="t-sweep")
        mine = [f for f in _flags(first, h, "orphan") if f.detail["version_id"] == obj["version_id"]]
        assert len(mine) == 1 and mine[0].logged
        assert "lists" in mine[0].detail["reason"] and "another producer" in mine[0].detail["reason"]
        (bare,) = [f for f in _flags(first, None, "orphan") if f.detail["version_id"] == stray["VersionId"]]
        assert "no recipe-hash metadata" in bare.detail["reason"] and bare.logged
        assert (await _state(env, h))["state"] == "claimed"
        second = await reconcile.reconcile(env.store, run="t-sweep")
        assert not any(f.logged for f in _flags(second, h, "orphan"))  # the event log holds each flag once
        async with await env.admin() as conn:
            cur = await conn.execute("SELECT count(*) FROM catalog.event WHERE action = 'flag.orphan' "
                                     "AND recipe_hash = %s", (h,))
            assert (await cur.fetchone())[0] == 2  # the output and its recipe file
    asyncio.run(go())


def test_a_recorded_object_gone_from_the_store_fails_its_product(env):
    async def go():
        subject = _subject()
        async with Producer(env) as w:
            h, obj = await w.product(subject, b"will vanish")
        await reconcile.reconcile(env.store, run="t-sweep")
        env.s3().delete_object(Bucket=BUCKET, Key=obj["key"], VersionId=obj["version_id"])
        report = await reconcile.reconcile(env.store, run="t-sweep")
        assert "is not in the store" in report.failed[h]
        assert _flags(report, h, "missing")
        assert (await _current(env, subject))["recipe_hash"] is None
    asyncio.run(go())


async def _check_count(env: Env, obj: dict) -> int:
    async with await env.admin() as conn:
        cur = await conn.execute("SELECT count(*) FROM catalog.object_check WHERE bucket = %s AND key = %s "
                                 "AND version_id = %s", (obj["bucket"], obj["key"], obj["version_id"]))
        return (await cur.fetchone())[0]


def test_an_edit_before_the_first_verify_that_keeps_the_mtime_is_found_by_the_hash(env):
    async def go():
        async with Producer(env) as w:
            h, obj = await w.product(_subject(), b"as committed")
        path = env.path(obj["key"])
        before = path.stat()
        path.write_bytes(b"AS COMMITTED")  # same size, written back with its old mtime (cp -p)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        report = await reconcile.reconcile(env.store, run="t-edit")
        assert "bytes hash to sha256" in report.failed[h]
    asyncio.run(go())


def test_a_touched_object_is_hashed_once_and_then_known(env):
    async def go():
        async with Producer(env) as w:
            h, obj = await w.product(_subject(), b"touched later")
        await reconcile.reconcile(env.store, run="t-check")
        assert await _check_count(env, obj) == 1  # the first verify's hash
        await reconcile.reconcile(env.store, run="t-check")
        assert await _check_count(env, obj) == 1  # known: no hash
        _later(env.path(obj["key"]))
        report = await reconcile.reconcile(env.store, run="t-check")
        assert h not in report.failed and not _flags(report, h)
        assert await _check_count(env, obj) == 2  # hashed once more, same bytes
        assert _preflight_problems(env) == []
        await reconcile.reconcile(env.store, run="t-check")
        assert await _check_count(env, obj) == 2
        async with await env.admin() as conn:
            await conn.execute("SET ROLE owner")
            with pytest.raises(errors.InsufficientPrivilege, match="append-only"):
                await conn.execute("UPDATE catalog.object_check SET size = 0")
    asyncio.run(go())


def _preflight_problems(env: Env) -> list[str]:
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(1) as pool:  # the item runs in its own loop, as the launch does
        return pool.submit(asyncio.run, reconcile.integrity_problems(env.store)).result()


@pytest.mark.parametrize("edit", ["other size", "same size", "touched only"])
def test_an_edit_on_disk_outside_the_gateway_fails_the_product_and_current_moves_back(env, edit):
    async def go():
        subject = _subject()
        async with Producer(env) as w:
            older, _ = await w.product(subject, b"older product", n=0)
            await reconcile.reconcile(env.store, run="t-edit")
            h, obj = await w.product(subject, b"newer product", n=1)
        await reconcile.reconcile(env.store, run="t-edit")
        assert (await _current(env, subject))["recipe_hash"] == h
        path = env.path(obj["key"])
        if edit == "other size":
            path.write_bytes(b"edited by hand")
        elif edit == "same size":
            path.write_bytes(b"NEWER PRODUCT")
        _later(path)
        version = env.s3().head_object(Bucket=BUCKET, Key=obj["key"], VersionId=obj["version_id"])
        assert version["VersionId"] == obj["version_id"]  # the gateway cannot tell (probe P9)
        report = await reconcile.reconcile(env.store, run="t-edit")
        if edit == "touched only":
            assert h not in report.failed and not _flags(report, h)
            assert (await _current(env, subject))["recipe_hash"] == h
            return
        expected = "bytes, recorded" if edit == "other size" else "changed after it was recorded"
        assert expected in report.failed[h] and _flags(report, h, "edited")
        assert (await _state(env, h))["state"] == "failed"
        assert (await _current(env, subject))["recipe_hash"] == older
    asyncio.run(go())


# ---------------------------------------------------------------- promote


def test_the_newest_qualifying_product_wins(env):
    async def go():
        subject = _subject()
        async with Producer(env) as w:
            first, _ = await w.product(subject, b"one", n=0)
            second, _ = await w.product(subject, b"two", n=1)
            unregistered, _ = await w.product(subject, b"three", n=2, tool=f"{fixture.TOOLKIT_NAME}.x_utils.gone")
            broken = await w.claim(subject, n=3)
            obj = w.put(broken, b"four")
            await w.commit(broken, [{**obj, "size": 99}])
            unknown_commit, _ = await w.product(subject, b"five", n=4, toolkit_commit="0123abcd" * 5)
            off_branch, _ = await w.product(subject, b"five b", n=8, toolkit_commit=env.off_branch)
            synthetic, fake = await w.product(subject, b"six", n=5, bucket=SYNTHETIC)
            from_synthetic, child = await w.product(subject, b"seven", n=6, inputs=[fake])
            grandchild, _ = await w.product(subject, b"eight", n=7, inputs=[child])
        report = await reconcile.reconcile(env.store, run="t-promote")
        newer = {unregistered, unknown_commit, off_branch, synthetic, from_synthetic, grandchild}
        assert {first, second} | newer <= set(report.verified) and broken in report.failed
        assert _flags(report, broken, "mismatch")  # the producer's row, not an edit on disk
        assert (await _current(env, subject))["recipe_hash"] == second
        async with await env.admin() as conn:
            history = await catalog.current_history(conn, BUCKET, KIND, subject)
        assert [r["recipe_hash"] for r in history] == [second]
        assert "newest qualifying product" in history[0]["reason"]
    asyncio.run(go())


@pytest.mark.parametrize("flag", ["failed", "undeclared_write", "unverified"])
def test_a_product_whose_committing_call_was_flagged_never_becomes_current(env, flag):
    """The runtime stamps a call receipt's flags on the products the call committed
    (``catalog.stamp_flags``, item S5); a product carrying one is verified but never promoted."""
    async def go():
        subject = _subject()
        async with Producer(env) as w:
            older, _ = await w.product(subject, b"one", n=0)
            newer, _ = await w.product(subject, b"two", n=1)
        async with await env.admin() as conn:
            assert await catalog.stamp_flags(conn, newer, [flag], "run/analyst_worker_0:r1") == 1
        report = await reconcile.reconcile(env.store, run="t-flagged")
        assert {older, newer} <= set(report.verified)
        assert (await _current(env, subject))["recipe_hash"] == older
    asyncio.run(go())


def test_a_stamp_after_promotion_takes_the_pointer_back_and_the_runtime_never_moves_onto_it(env):
    async def go():
        subject = _subject()
        async with Producer(env) as w:
            older, _ = await w.product(subject, b"one", n=0)
            newer, _ = await w.product(subject, b"two", n=1)
        await reconcile.reconcile(env.store, run="t-late-1")
        assert (await _current(env, subject))["recipe_hash"] == newer
        async with reconcile.reconciler_session(env.store, run="t-late-stamp") as conn:  # as the runtime stamps
            await catalog.stamp_flags(conn, newer, ["failed"], "run/analyst_worker_0:r9")
        # The stamp took the pointer off it: no earlier qualifying product was ever current, so none.
        assert (await _current(env, subject))["recipe_hash"] is None
        await reconcile.reconcile(env.store, run="t-late-2")  # the next pass promotes the unflagged one
        assert (await _current(env, subject))["recipe_hash"] == older
        async with reconcile.reconciler_session(env.store, run="t-late-3") as conn:
            pointer = await catalog.current(conn, BUCKET, KIND, subject)
            with pytest.raises(psycopg.errors.InvalidParameterValue, match="carries flags"):
                await catalog.move_current(conn, BUCKET, KIND, subject, newer, pointer["seq"], "try")
        async with await env.admin() as conn:  # nor the runtime's superuser admin
            pointer = await catalog.current(conn, BUCKET, KIND, subject)
            with pytest.raises(psycopg.errors.InvalidParameterValue, match="only the human's own login"):
                await catalog.move_current(conn, BUCKET, KIND, subject, newer, pointer["seq"], "try")
            await catalog.set_human_password(conn, "human secret")
        from auto_research.store.__main__ import _human_dsn

        async with await psycopg.AsyncConnection.connect(_human_dsn(env.store, "human secret"), autocommit=True) as conn:
            pointer = await catalog.current(conn, BUCKET, KIND, subject)  # the human's own login may
            moved = await catalog.move_current(conn, BUCKET, KIND, subject, newer, pointer["seq"], "human override")
            assert moved["status"] == "moved" and moved["current"]["moved_by"] == "ar_human"
    asyncio.run(go())


def test_a_flag_the_human_cleared_no_longer_blocks_promotion(env):
    async def go():
        subject = _subject()
        async with Producer(env) as w:
            older, _ = await w.product(subject, b"one", n=0)
            newer, _ = await w.product(subject, b"two", n=1)
        async with reconcile.reconciler_session(env.store, run="t-clear-stamp") as conn:
            await catalog.stamp_flags(conn, newer, ["failed"], "run/analyst_worker_0:r4")
        await reconcile.reconcile(env.store, run="t-clear-1")
        assert (await _current(env, subject))["recipe_hash"] == older
        async with await env.admin() as conn:  # the human sets its own login's password
            await catalog.set_human_password(conn, "human secret")
        from auto_research.store.__main__ import _human_dsn

        async with await psycopg.AsyncConnection.connect(_human_dsn(env.store, "human secret"), autocommit=True) as conn:
            await catalog.clear_flag(conn, newer, "failed", "exit 1 after a good commit")
            assert await catalog.live_flags(conn, newer) == set()
        await reconcile.reconcile(env.store, run="t-clear-2")
        assert (await _current(env, subject))["recipe_hash"] == newer
    asyncio.run(go())


def test_a_product_made_from_a_flagged_product_never_becomes_current(env):
    async def go():
        subject = _subject()
        async with Producer(env) as w:
            plain, _ = await w.product(subject, b"one", n=0)
            parent, made = await w.product(_subject(), b"parent", n=1)
            child, _ = await w.product(subject, b"child", n=2, inputs=[made])
        async with await env.admin() as conn:
            await catalog.stamp_flags(conn, parent, ["unverified"], "run/analyst_worker_0:r3")
        await reconcile.reconcile(env.store, run="t-ancestor")
        assert (await _current(env, subject))["recipe_hash"] == plain
    asyncio.run(go())


def test_two_buckets_sharing_a_subject_keep_their_own_pointers(env):
    async def go():
        subject = _subject()
        async with Producer(env) as w:
            here, _ = await w.product(subject, b"in alpha", n=0)
            there, _ = await w.product(subject, b"in beta", n=1, bucket="beta")
        report = await reconcile.reconcile(env.store, run="t-buckets")
        moved = {(p["bucket"], p["recipe_hash"]) for p in report.promoted if p["subject"] == subject}
        assert moved == {(BUCKET, here), ("beta", there)}
        async with await env.admin() as conn:
            assert (await catalog.current(conn, BUCKET, KIND, subject))["recipe_hash"] == here
            assert (await catalog.current(conn, "beta", KIND, subject))["recipe_hash"] == there
    asyncio.run(go())


def test_the_registered_tools_default_to_the_declared_toolkits_manifests(env):
    async def go():
        subject = _subject()
        async with Producer(env) as w:
            h, _ = await w.product(subject, b"default tools", n=0)
            await w.product(subject, b"unknown tool", n=1, tool=f"{fixture.TOOLKIT_NAME}.x_utils.gone")
        await reconcile.reconcile(env.store, run="t-promote")
        assert (await _current(env, subject))["recipe_hash"] == h
    asyncio.run(go())


def test_a_human_override_holds_until_a_newer_product_qualifies(env):
    async def go():
        subject = _subject()
        async with Producer(env) as w:
            old, _ = await w.product(subject, b"old", n=0)
            new, _ = await w.product(subject, b"new", n=1)
            await reconcile.reconcile(env.store, run="t-promote")
            pointer = await _current(env, subject)
            assert pointer["recipe_hash"] == new
            async with await env.admin() as admin:  # the runtime's admin role: a runtime move, held by nothing
                await catalog.move_current(admin, BUCKET, KIND, subject, old, pointer["seq"], "admin")
            await reconcile.reconcile(env.store, run="t-promote")
            pointer = await _current(env, subject)
            assert pointer["recipe_hash"] == new
            async with await _human(env) as human:  # the human's own login rolls back
                moved = await catalog.move_current(human, BUCKET, KIND, subject, old, pointer["seq"], "roll back")
            assert moved["status"] == "moved" and moved["current"]["human_override"] is True
            report = await reconcile.reconcile(env.store, run="t-promote")
            assert not [p for p in report.promoted if p["kind"] == KIND and p["recipe_hash"] in (old, new)]
            assert (await _current(env, subject))["recipe_hash"] == old
            newest, _ = await w.product(subject, b"newest", n=2)
        await reconcile.reconcile(env.store, run="t-promote")
        assert (await _current(env, subject))["recipe_hash"] == newest
    asyncio.run(go())


def _interleave(monkeypatch, env, move):
    """After the reconciler reads a pointer, ``move(admin, pointer)`` moves it first (a real concurrent
    mover, so the reconciler's compare-and-swap meets a newer seq)."""
    real = catalog.current

    async def current_then_move(conn, bucket, kind, subject):
        pointer = await real(conn, bucket, kind, subject)
        if subject == env.contended:
            async with await env.admin() as admin:
                await move(admin, pointer)
        return pointer

    monkeypatch.setattr(catalog, "current", current_then_move)


def test_a_demotion_by_the_admin_fallback_does_not_block_re_promotion(env):
    """A stamp through the admin role (the runtime without a reconciler login) takes the pointer off its
    product; that move is the runtime's, so the next pass promotes the earlier product again."""
    async def go():
        subject = _subject()
        async with Producer(env) as w:
            older, _ = await w.product(subject, b"older", n=0)
            newer, _ = await w.product(subject, b"newer", n=1)
        await reconcile.reconcile(env.store, run="t-fallback")
        assert (await _current(env, subject))["recipe_hash"] == newer
        async with await env.admin() as admin:
            await catalog.stamp_flags(admin, newer, ["failed"], "run/analyst_worker_0:r7")
        pointer = await _current(env, subject)
        assert (pointer["recipe_hash"], pointer["moved_by"], pointer["human_override"]) == (
            None, env.store.catalog.admin_user, False)
        await reconcile.reconcile(env.store, run="t-fallback")
        assert (await _current(env, subject))["recipe_hash"] == older
    asyncio.run(go())


@pytest.mark.parametrize("who", ["the admin impersonating the human", "the human's own session"])
def test_a_demotion_is_never_a_human_override(env, who):
    """Only the human's own move_current is an override: a demotion (here a stamp) holds nothing,
    whoever's session ran it, and an impersonation records the login that authenticated."""
    async def go():
        subject = _subject()
        async with Producer(env) as w:
            older, _ = await w.product(subject, b"older", n=0)
            newer, _ = await w.product(subject, b"newer", n=1)
        await reconcile.reconcile(env.store, run="t-demote")
        human = await _human(env)  # sets the human's password
        try:
            if who == "the human's own session":
                await catalog.stamp_flags(human, newer, ["failed"], "run/analyst_worker_0:r8")
            else:
                async with await env.admin() as admin:
                    await admin.execute(f"SET SESSION AUTHORIZATION {catalog.HUMAN_ROLE}")
                    await catalog.stamp_flags(admin, newer, ["failed"], "run/analyst_worker_0:r8")
        finally:
            await human.close()
        pointer = await _current(env, subject)
        assert pointer["recipe_hash"] is None and pointer["human_override"] is False
        if who != "the human's own session":
            assert pointer["moved_by"] == env.store.catalog.admin_user  # the login that authenticated
        await reconcile.reconcile(env.store, run="t-demote")
        assert (await _current(env, subject))["recipe_hash"] == older
    asyncio.run(go())


def test_a_compare_and_swap_mismatch_rereads_and_decides_again(env, monkeypatch):
    async def go():
        subject = env.contended = _subject()
        async with Producer(env) as w:
            old, _ = await w.product(subject, b"old", n=0)
            await reconcile.reconcile(env.store, run="t-cas")
            new, _ = await w.product(subject, b"new", n=1)
        moved_once = []

        async def human_moves_once(_admin, pointer):
            if not moved_once:  # the human re-points at the old product between read and swap
                async with await _human(env) as human:
                    moved_once.append(await catalog.move_current(human, BUCKET, KIND, subject, old, pointer["seq"],
                                                                 "human: keep the old one"))
        _interleave(monkeypatch, env, human_moves_once)
        report = await reconcile.reconcile(env.store, run="t-cas")
        monkeypatch.undo()
        assert moved_once[0]["status"] == "moved"
        assert new in report.verified and not [p for p in report.promoted if p["recipe_hash"] == new]
        assert (await _current(env, subject))["recipe_hash"] == old  # the re-read saw the human's move
        async with await env.admin() as conn:
            cur = await conn.execute("SELECT count(*) FROM catalog.event WHERE action = 'move_current.mismatch' "
                                     "AND recipe_hash = %s", (new,))
            assert (await cur.fetchone())[0] == 1
    asyncio.run(go())


def test_a_pointer_that_moves_under_every_attempt_is_flagged_not_chased(env, monkeypatch):
    async def go():
        subject = env.contended = _subject()
        async with Producer(env) as w:
            a, _ = await w.product(subject, b"a", n=0)
            b, _ = await w.product(subject, b"b", n=1)
            await reconcile.reconcile(env.store, run="t-cas")
            c, _ = await w.product(subject, b"c", n=2)
        other = reconcile._login_name("t-cas-other")

        async def rival_reconciler(admin, pointer):  # another reconciler login, moving between a and b
            target = a if pointer["recipe_hash"] != a else b
            async with await psycopg.AsyncConnection.connect(
                    make_conninfo(env.store.admin_dsn(), user=other, password="x"), autocommit=True) as rival:
                await catalog.move_current(rival, BUCKET, KIND, subject, target, pointer["seq"], "rival")
        async with await env.admin() as admin:
            await catalog.issue_role(admin, name=other, role_class="reconciler", run="t", password="x",
                                     valid_until=datetime.now(UTC) + timedelta(hours=1))
        try:
            _interleave(monkeypatch, env, rival_reconciler)
            report = await reconcile.reconcile(env.store, run="t-cas")
        finally:
            monkeypatch.undo()
            async with await env.admin() as admin:
                await catalog.end_role(admin, name=other)
        (flag,) = [f for f in _flags(report, None, "promotion_contended") if f.detail["subject"] == subject]
        assert str(reconcile.PROMOTE_ATTEMPTS) in flag.detail["reason"]
        assert (await _current(env, subject))["recipe_hash"] in (a, b)
    asyncio.run(go())


def test_a_pointer_never_moves_back_to_an_older_product(env):
    async def go():
        subject = _subject()
        rival = reconcile._login_name("t-forward-other")
        async with Producer(env) as w:
            older, _ = await w.product(subject, b"older", n=0)
            await reconcile.reconcile(env.store, run="t-forward")
            newer, _ = await w.product(subject, b"newer", n=1, tool=f"{fixture.TOOLKIT_NAME}.x_utils.gone")
        async with await env.admin() as admin:  # a run whose toolkit holds that tool made it current
            await catalog.issue_role(admin, name=rival, role_class="reconciler", run="t", password="x",
                                     valid_until=datetime.now(UTC) + timedelta(hours=1))
        try:
            async with await psycopg.AsyncConnection.connect(
                    make_conninfo(env.store.admin_dsn(), user=rival, password="x"), autocommit=True) as other:
                await reconcile.reconcile(env.store, run="t-forward-other", conn=other)  # verifies newer
                pointer = await catalog.current(other, BUCKET, KIND, subject)
                await catalog.move_current(other, BUCKET, KIND, subject, newer, pointer["seq"], "its own toolkit")
        finally:
            async with await env.admin() as admin:
                await catalog.end_role(admin, name=rival)
        report = await reconcile.reconcile(env.store, run="t-forward")
        assert not [p for p in report.promoted if p["kind"] == KIND and p["recipe_hash"] == older]
        assert (await _current(env, subject))["recipe_hash"] == newer
    asyncio.run(go())


def test_a_product_committed_during_the_pass_is_left_for_the_next_one(env, monkeypatch):
    async def go():
        subject = _subject()
        real, committed = reconcile._rows, []

        async def rows_then_commit(conn, name):
            rows = await real(conn, name)
            if not committed:  # after the listing, before the products are checked
                async with Producer(env) as w:
                    committed.append(await w.product(subject, b"late arrival"))
            return rows
        monkeypatch.setattr(reconcile, "_rows", rows_then_commit)
        report = await reconcile.reconcile(env.store, run="t-race")
        monkeypatch.undo()
        h, _ = committed[0]
        assert h not in report.failed and h not in report.verified and not _flags(report, h)
        assert (await _state(env, h))["state"] == "committed"
        assert h in (await reconcile.reconcile(env.store, run="t-race")).verified
    asyncio.run(go())


def test_two_extra_versions_give_one_nondeterministic_verdict(env):
    async def go():
        async with Producer(env) as w:
            h, obj = await w.product(_subject(), b"first output")
        for body in (b"second output", b"third output"):
            env.s3().put_object(Bucket=BUCKET, Key=obj["key"], Body=body, Metadata={reconcile.META_RECIPE_HASH: h})
        report = await reconcile.reconcile(env.store, run="t-nondet")
        assert h in report.nondeterministic and not _flags(report, h, "verdict_refused")
        assert (await _state(env, h))["state"] == "nondeterministic"
    asyncio.run(go())


def test_a_row_in_a_bucket_the_store_does_not_declare_gets_no_verdict(env):
    async def go():
        async with Producer(env) as w:
            h = await w.claim(_subject())
            await w.commit(h, [{"store": "db", "bucket": "zeta", "key": "k", "version_id": "v1",
                                "sha256": "2" * 64, "size": 1}])
        report = await reconcile.reconcile(env.store, run="t-bucket")
        assert h not in report.failed and h not in report.verified
        (flag,) = _flags(report, h, "unchecked")
        assert "zeta is not one the store declares" in flag.detail["reason"]
        assert (await _state(env, h))["state"] == "committed"
    asyncio.run(go())


def test_an_owner_restarting_after_expiry_is_a_new_generation_and_its_old_files_are_not_adopted(env):
    async def go():
        async with Producer(env) as w:
            subject = _subject()
            h = await w.claim(subject, lease="1 second")
            old = w.put(h, b"first attempt")
            w.finish(h, [old])  # complete, but not committed before the lease ran out
            await asyncio.sleep(1.2)
            await w.claim(subject, lease="1 second")  # the same owner starts over: generation 2
            assert w.claims[h]["generation"] == 2
            await asyncio.sleep(1.2)
        report = await reconcile.reconcile(env.store, run="t-restart")
        assert h not in report.adopted
        reasons = [f.detail["reason"] for f in _flags(report, h, "orphan")]
        assert reasons and all("was written by generation 1; the claim is at 2" in r for r in reasons), reasons
    asyncio.run(go())


@pytest.mark.parametrize("fault", ["a listed size the version does not have", "its own sha256 metadata wrong",
                                   "no toolkit-commit metadata"])
def test_a_recipe_file_that_does_not_vouch_for_its_production_adopts_nothing(env, fault):
    async def go():
        async with Producer(env) as w:
            h = await w.claim(_subject(), lease="1 second")
            obj = w.put(h, b"an output")
            if fault == "a listed size the version does not have":
                w.finish(h, [obj], listed=[{**obj, "size": obj["size"] + 1}])
            else:
                claim = w.claims[h]
                body = reconcile.RecipeFile(recipe_hash=h, generation=claim["generation"], recipe=claim["recipe"],
                                            objects=[{k: obj[k] for k in ("key", "version_id", "sha256", "size")}])
                meta = ({reconcile.META_SHA256: "3" * 64} if fault == "its own sha256 metadata wrong"
                        else {reconcile.META_TOOLKIT_COMMIT: None})
                w.put(h, body.model_dump_json().encode(), name=reconcile.RECIPE_FILE, meta=meta)
            await asyncio.sleep(1.2)
        report = await reconcile.reconcile(env.store, run="t-recipe")
        assert h not in report.adopted and _flags(report, h, "orphan")
        expected = {"a listed size the version does not have": "a size or sha256 the version does not carry",
                    "its own sha256 metadata wrong": "is not the sha256 of its bytes",
                    "no toolkit-commit metadata": "lacks toolkit-commit"}[fault]
        assert all(expected in f.detail["reason"] for f in _flags(report, h, "orphan"))
    asyncio.run(go())


def test_two_reconcilers_adopting_at_once_leave_no_refusal_behind(env):
    async def go():
        async with Producer(env) as w:
            h = await w.claim(_subject(), lease="1 second")
            obj = w.put(h, b"adopted once")
            w.finish(h, [obj])
            await asyncio.sleep(1.2)
        first, second = await asyncio.gather(reconcile.reconcile(env.store, run="t-twin-a"),
                                              reconcile.reconcile(env.store, run="t-twin-b"))
        assert (h in first.adopted) != (h in second.adopted)
        assert not _flags(first, h, "adopt_refused") and not _flags(second, h, "adopt_refused")
        assert (await _state(env, h))["state"] == "verified"
    asyncio.run(go())


def test_a_second_version_with_the_same_bytes_is_hashed_once(env):
    async def go():
        async with Producer(env) as w:
            h, obj = await w.product(_subject(), b"same bytes twice")
        again = env.s3().put_object(Bucket=BUCKET, Key=obj["key"], Body=b"same bytes twice",
                                    Metadata={reconcile.META_RECIPE_HASH: h})
        twin = {**obj, "version_id": again["VersionId"]}
        for _ in range(2):
            report = await reconcile.reconcile(env.store, run="t-twin")
            assert h not in report.nondeterministic
        assert await _check_count(env, twin) == 1
    asyncio.run(go())


def test_the_runtime_passes_the_reconcilers_own_least_privilege_keys(env):
    async def go():
        from auto_research.store import keys

        subject = _subject()
        async with Producer(env) as w:
            h = await w.claim(subject, lease="1 second")
            obj = w.put(h, b"adopted with reader keys")
            w.finish(h, [obj])
            await asyncio.sleep(1.2)
        credentials = await keys.issue(env.store, "reconciler", "reconciler", "t-keys")
        try:
            async with await psycopg.AsyncConnection.connect(credentials.dsn(), autocommit=True) as conn:
                report = await reconcile.reconcile(env.store, run="t-keys", conn=conn, s3=credentials)
            with pytest.raises(Exception, match="AccessDenied"):  # the keys write nothing
                reconcile._client(env.store, credentials).put_object(Bucket=BUCKET, Key="x", Body=b"x")
        finally:
            await keys.revoke(env.store, "reconciler", "t-keys")
        assert h in report.adopted and h in report.verified
        assert (await _current(env, subject))["recipe_hash"] == h
    asyncio.run(go())


# ---------------------------------------------------------------- login, flag function, preflight


def test_the_reconciler_login_is_ended_after_the_pass(env):
    async def go():
        await reconcile.reconcile(env.store, run="t-login")
        async with await env.admin() as conn:
            cur = await conn.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s",
                                     (reconcile._login_name("t-login"),))
            assert (await cur.fetchone())[0] is False
        await catalog.check_schema(env.store.admin_dsn())
    asyncio.run(go())


def test_only_the_reconciler_flags_and_a_flag_is_logged_once(env):
    async def go():
        async with reconcile.reconciler_session(env.store, run="t-flag") as conn:
            cur = await conn.execute("SELECT catalog.flag('orphan', NULL, %s)", (Jsonb({"key": "k1"}),))
            assert (await cur.fetchone())[0] is True
            cur = await conn.execute("SELECT catalog.flag('orphan', NULL, %s)", (Jsonb({"key": "k1"}),))
            assert (await cur.fetchone())[0] is False
            with pytest.raises(errors.InvalidParameterValue):
                await conn.execute("SELECT catalog.flag('Bad Action', NULL, '{}')")
        async with Producer(env) as w:
            with pytest.raises(errors.InsufficientPrivilege):
                await w.conn.execute("SELECT catalog.flag('orphan', NULL, '{}')")
    asyncio.run(go())


def _preflight_item() -> str | None:
    checks = Preflight()
    checks.register("store_integrity", dict(build_default_preflight()._checks)["store_integrity"])
    try:
        asyncio.run(checks.run_checklist())
    except PreflightError as exc:
        return str(exc)
    return None


def test_the_preflight_item_names_an_edited_object_until_a_pass_fails_its_product(env):
    async def setup():
        await reconcile.reconcile(env.store, run="t-preflight")
        async with Producer(env) as w:
            h, obj = await w.product(_subject(), b"checked at launch")
        await reconcile.reconcile(env.store, run="t-preflight")
        return h, obj
    h, obj = asyncio.run(setup())
    assert _preflight_item() is None
    env.path(obj["key"]).write_bytes(b"CHECKED AT LAUNCH")  # same size: found by mtime, then hash
    _later(env.path(obj["key"]))
    message = _preflight_item()
    assert f"product {h[:12]}" in message and "changed after it was recorded" in message
    assert asyncio.run(_state(env, h))["state"] == "verified"  # the item only checks
    asyncio.run(reconcile.reconcile(env.store, run="t-preflight"))
    assert _preflight_item() is None
