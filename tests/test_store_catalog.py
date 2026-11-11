"""The store catalog (``auto_research.store.catalog`` + ``catalog.sql``) on a throwaway Postgres.

One server per module (initdb + pg_ctl in a temp folder, unix socket only, trust auth), one fresh
database per test with the schema installed through :func:`catalog.apply_schema`. Agents are login
roles issued through :func:`catalog.issue_role`, so every call below runs as the agent it names and
the SQL functions see that agent as ``session_user``.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import errors
from psycopg.types.json import Jsonb

from auto_research.store import catalog


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """A Postgres cluster in a temp folder; yields its socket folder."""
    root = tmp_path_factory.mktemp("pg")
    data, log = root / "data", root / "log"
    subprocess.run([shutil.which("initdb"), "-D", str(data), "-U", "postgres", "--auth=trust", "--no-sync",
                    "-E", "UTF8", "--locale=C"], check=True, capture_output=True)
    subprocess.run([shutil.which("pg_ctl"), "-D", str(data), "-l", str(log), "-w",
                    "-o", f"-k {root} -c listen_addresses='' -F", "start"], check=True, capture_output=True)
    yield str(root)
    subprocess.run([shutil.which("pg_ctl"), "-D", str(data), "-m", "immediate", "-w", "stop"], capture_output=True)


class Db:
    """One test database: ``dsn(user)`` connects as ``user``; ``agent(cls)`` issues a fresh agent role."""

    def __init__(self, socket: str, name: str):
        self.socket, self.name, self.tag = socket, name, name[1:9]
        self.issued: set[str] = set()

    def dsn(self, user: str = "postgres") -> str:
        return f"host={self.socket} user={user} dbname={self.name}"

    async def connect(self, user: str = "postgres") -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(self.dsn(user), autocommit=True)

    async def agent(self, role_class: str, label: str) -> psycopg.AsyncConnection:
        name = f"{label}_{self.tag}"
        self.issued.add(name)
        async with await self.connect() as admin:
            await catalog.issue_role(admin, name=name, role_class=role_class, run="run1", password="pw",
                                     valid_until=datetime.now(UTC) + timedelta(hours=1))
        return await self.connect(name)


@pytest.fixture
def db(server):
    name = "t" + uuid.uuid4().hex
    with psycopg.connect(f"host={server} user=postgres dbname=postgres", autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    d = Db(server, name)
    asyncio.run(catalog.apply_schema(d.dsn()))
    yield d

    async def end_all():  # the cluster serves one catalog at a time: no login outlives its test
        async with await d.connect() as admin:
            for role in d.issued:
                await catalog.end_role(admin, name=role)
    asyncio.run(end_all())


def recipe(subject: str = "01002901", **settings) -> dict:
    return {"kind": "ixpe.pcube", "subject": subject, "tool": "xspec_utils.pcube", "algorithm_version": "2",
            "settings": settings or {"emin": 2.0, "emax": 8.0},
            "inputs": [{"store": "db", "bucket": "ixpe", "key": "data/obs/01/01002901/evt2.fits", "version_id": "v1"}]}


def objects(h: str, n: int = 1) -> list[dict]:
    return [{"store": "db", "bucket": "ixpe", "key": f"products/pcube/{h}/out{i}.fits", "version_id": f"v{i}",
             "sha256": f"{i:064x}", "size": 100 + i} for i in range(n)]


async def call(conn: psycopg.AsyncConnection, query: str, *params):
    cur = await conn.execute(query, params)
    return (await cur.fetchone())[0]


async def claim(conn, r: dict, lease: str = "1 hour") -> dict:
    return await call(conn, "SELECT catalog.claim(%s, %s::interval)", Jsonb(r), lease)


async def commit(conn, h: str, objs: list[dict], toolkit: str = "abc123", env: str = "fp1") -> dict:
    return await call(conn, "SELECT catalog.commit(%s, %s, %s, %s)", h, Jsonb(objs), toolkit, env)


async def actions(db: Db) -> list[str]:
    async with await db.connect() as admin:
        cur = await admin.execute("SELECT action FROM catalog.event ORDER BY id")
        return [row[0] for row in await cur.fetchall()]


async def committed_product(db: Db, producer: psycopg.AsyncConnection, r: dict | None = None) -> str:
    h = (await claim(producer, r or recipe()))["recipe_hash"]
    await commit(producer, h, objects(h))
    return h


# ---------------------------------------------------------------- schema


def test_schema_is_installed_once_and_checked(server):
    name = "t" + uuid.uuid4().hex
    with psycopg.connect(f"host={server} user=postgres dbname=postgres", autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    d = Db(server, name)

    async def body():
        with pytest.raises(catalog.CatalogError, match="no catalog schema"):
            await catalog.check_schema(d.dsn())
        await catalog.apply_schema(d.dsn())
        await catalog.apply_schema(d.dsn())  # already at this version: nothing to do
        await catalog.check_schema(d.dsn())
        async with await d.connect() as admin:
            await admin.execute("INSERT INTO catalog.schema_version (version) VALUES (%s)", (catalog.SCHEMA_VERSION + 1,))
        with pytest.raises(catalog.CatalogError, match="version"):
            await catalog.check_schema(d.dsn())
        with pytest.raises(catalog.CatalogError, match="no upgrade"):
            await catalog.apply_schema(d.dsn())
    asyncio.run(body())


def test_tables_and_functions_are_owned_by_owner(db):
    async def body():
        async with await db.connect() as admin:
            cur = await admin.execute("SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'catalog'")
            assert [r[0] for r in await cur.fetchall()] == ["owner"]
            cur = await admin.execute("SELECT DISTINCT pg_get_userbyid(proowner) FROM pg_proc "
                                      "WHERE pronamespace = 'catalog'::regnamespace")
            assert [r[0] for r in await cur.fetchall()] == ["owner"]
    asyncio.run(body())


# ---------------------------------------------------------------- claim


def test_claim_then_in_progress_then_reuse(db):
    async def body():
        a, b = await db.agent("producer", "wa"), await db.agent("producer", "wb")
        got = await claim(a, recipe())
        assert got["status"] == "claimed" and got["owner"] == f"wa_{db.tag}"
        h = got["recipe_hash"]
        assert h == await call(a, "SELECT catalog.recipe_hash(%s)", Jsonb(recipe()))
        busy = await claim(b, recipe())
        assert busy["status"] == "in_progress" and busy["owner"] == f"wa_{db.tag}"
        assert (await commit(a, h, objects(h, 2)))["status"] == "committed"
        reuse = await claim(b, recipe())
        assert reuse["status"] == "reuse" and reuse["state"] == "committed"
        assert [o["key"] for o in reuse["objects"]] == [o["key"] for o in objects(h, 2)]
        assert await actions(db) == ["credential.issue", "credential.issue", "claim", "claim.in_progress",
                                     "commit", "claim.reuse"]
        async with await db.connect() as admin:
            rows = await catalog.objects(admin, h)
            assert {r["created_by"] for r in rows} == {f"wa_{db.tag}"}
            p = await catalog.product(admin, h)
            assert (p["toolkit_commit"], p["env_fingerprint"], p["lease_until"]) == ("abc123", "fp1", None)
            assert p["recipe"] == recipe()
        await a.close(); await b.close()
    asyncio.run(body())


def test_claim_identity_is_the_recipe(db):
    async def body():
        a = await db.agent("producer", "wa")
        h1 = (await claim(a, recipe()))["recipe_hash"]
        reordered = dict(reversed(list(recipe().items())))
        assert (await claim(a, reordered))["recipe_hash"] == h1  # jsonb fixes key order and spacing
        assert (await claim(a, recipe(emin=3.0, emax=8.0)))["recipe_hash"] != h1
        assert (await claim(a, recipe(subject="01002902")))["recipe_hash"] != h1
        renewed = await claim(a, recipe(), "2 hours")
        assert renewed["status"] == "claimed" and renewed["recipe_hash"] == h1
        assert (await actions(db))[-1] == "claim.renew"
        await a.close()
    asyncio.run(body())


@pytest.mark.parametrize("bad", [
    {k: v for k, v in recipe().items() if k != "settings"},
    {**recipe(), "toolkit_commit": "abc"},
    {**recipe(), "tool": ""},
    {**recipe(), "tool": "  "},
    {**recipe(), "settings": [1, 2]},
    {**recipe(), "inputs": [{"store": "db", "bucket": "ixpe", "key": "k"}]},
    {**recipe(), "inputs": [{"store": "db", "bucket": "ixpe", "key": "k", "version_id": 3}]},
    {**recipe(), "inputs": [{"store": "db", "bucket": "ixpe", "key": "k", "version": "v1"}]},  # the retired name
])
def test_claim_refuses_a_malformed_recipe(db, bad):
    async def body():
        a = await db.agent("producer", "wa")
        with pytest.raises(errors.InvalidParameterValue, match="claim refused"):
            await claim(a, bad)
        await a.close()
    asyncio.run(body())


@pytest.mark.parametrize("lease", ["0 seconds", "-1 hour", "2 days"])
def test_claim_refuses_a_lease_out_of_range(db, lease):
    async def body():
        a = await db.agent("producer", "wa")
        with pytest.raises(errors.InvalidParameterValue, match="lease"):
            await claim(a, recipe(), lease)
        await a.close()
    asyncio.run(body())


def test_claims_expire_by_lease(db):
    async def body():
        a, b = await db.agent("producer", "wa"), await db.agent("producer", "wb")
        first = await claim(a, recipe(), "200 milliseconds")
        h = first["recipe_hash"]
        assert first["generation"] == 1
        assert (await claim(a, recipe(), "200 milliseconds"))["generation"] == 1  # renewing a live lease keeps it
        busy = await claim(b, recipe())
        assert (busy["status"], busy["generation"]) == ("in_progress", 1)
        time.sleep(0.4)
        taken = await claim(b, recipe())
        assert taken["status"] == "claimed" and taken["owner"] == f"wb_{db.tag}"
        assert taken["generation"] == 2  # a takeover is a new production, written to its own folder
        assert (await actions(db))[-1] == "claim.takeover"
        with pytest.raises(errors.InsufficientPrivilege, match=f"held by wb_{db.tag}"):
            await commit(a, h, objects(h))
        assert (await commit(b, h, objects(h)))["status"] == "committed"
        await a.close(); await b.close()
    asyncio.run(body())


def test_a_racing_claim_waits_then_sees_in_progress(db):
    """B's claim lands while A's insert is uncommitted: B waits on the row, then reads A's claim."""
    async def body():
        a, b = await db.agent("producer", "wa"), await db.agent("producer", "wb")
        await a.set_autocommit(False)
        async with a.transaction():
            assert (await claim(a, recipe()))["status"] == "claimed"
            racing = asyncio.create_task(claim(b, recipe()))
            await asyncio.sleep(0.3)
            assert not racing.done()  # blocked on A's uncommitted row
        got = await racing
        assert got["status"] == "in_progress" and got["owner"] == f"wa_{db.tag}"
        await a.close(); await b.close()
    asyncio.run(body())


# ---------------------------------------------------------------- commit and fail


def test_commit_refusals(db):
    async def body():
        a, b = await db.agent("producer", "wa"), await db.agent("producer", "wb")
        unknown = "f" * 64
        with pytest.raises(errors.InvalidParameterValue, match="no claim"):
            await commit(a, unknown, objects(unknown))
        h = (await claim(a, recipe()))["recipe_hash"]
        with pytest.raises(errors.InsufficientPrivilege, match="held by"):
            await commit(b, h, objects(h))
        with pytest.raises(errors.InvalidParameterValue, match="toolkit commit"):
            await commit(a, h, objects(h), toolkit="")
        with pytest.raises(errors.InvalidParameterValue, match="non-empty list"):
            await commit(a, h, [])
        with pytest.raises(errors.InvalidParameterValue, match="64 hex"):
            await commit(a, h, [{**objects(h)[0], "sha256": "xyz"}])
        with pytest.raises(errors.InvalidParameterValue, match="64 hex"):
            await commit(a, h, [{**objects(h)[0], "extra": 1}])
        with pytest.raises(errors.InvalidParameterValue, match="size"):
            await commit(a, h, [{**objects(h)[0], "size": 1.5}])
        assert (await commit(a, h, objects(h)))["status"] == "committed"
        with pytest.raises(errors.InvalidParameterValue, match="is committed, not claimed"):
            await commit(a, h, objects(h))
        # a version already recorded by another product is refused, and the claim stays claimed
        h2 = (await claim(a, recipe(subject="x")))["recipe_hash"]
        with pytest.raises(errors.UniqueViolation, match="already recorded"):
            await commit(a, h2, objects(h))
        async with await db.connect() as admin:
            assert (await catalog.product(admin, h2))["state"] == "claimed"
        await a.close(); await b.close()
    asyncio.run(body())


def test_a_renewal_continues_the_production_and_a_restart_after_expiry_begins_one(db):
    async def body():
        a = await db.agent("producer", "wa")
        h = (await claim(a, recipe(subject="restart"), "300 milliseconds"))["recipe_hash"]
        async with await db.connect() as admin:
            started = (await catalog.product(admin, h))["claimed_at"]
            assert (await claim(a, recipe(subject="restart"), "300 milliseconds"))["generation"] == 1
            assert (await catalog.product(admin, h))["claimed_at"] == started  # a renewal keeps its start
            assert (await actions(db))[-1] == "claim.renew"
            time.sleep(0.4)
            again = await claim(a, recipe(subject="restart"))
            assert again["generation"] == 2 and (await actions(db))[-1] == "claim.restart"
            assert (await catalog.product(admin, h))["claimed_at"] > started
        await a.close()
    asyncio.run(body())


def test_fail_then_retry_and_nondeterministic(db):
    async def body():
        a, b = await db.agent("producer", "wa"), await db.agent("producer", "wb")
        h = (await claim(a, recipe()))["recipe_hash"]
        with pytest.raises(errors.InsufficientPrivilege, match="held by"):
            await call(b, "SELECT catalog.fail(%s, %s)", h, "no")
        with pytest.raises(errors.InvalidParameterValue, match="reason"):
            await call(a, "SELECT catalog.fail(%s, %s)", h, "")
        assert (await call(a, "SELECT catalog.fail(%s, %s)", h, "xspec crashed"))["status"] == "failed"
        with pytest.raises(errors.InvalidParameterValue, match="not claimed"):
            await call(a, "SELECT catalog.fail(%s, %s)", h, "again")
        retry = await claim(b, recipe())
        assert retry["status"] == "claimed" and retry["owner"] == f"wb_{db.tag}"
        assert retry["generation"] == 2
        assert (await actions(db))[-1] == "claim.retry"
        got = await call(b, "SELECT catalog.fail(%s, %s, true)", h, "412: the store holds other bytes")
        assert got["status"] == "nondeterministic"
        again = await claim(a, recipe())
        assert again == {"status": "nondeterministic", "recipe_hash": h, "reason": "412: the store holds other bytes"}
        await a.close(); await b.close()
    asyncio.run(body())


# ---------------------------------------------------------------- roles and direct writes


def test_each_class_calls_only_its_functions(db):
    async def body():
        r, p = await db.agent("reader", "lib"), await db.agent("producer", "wa")
        with pytest.raises(errors.InsufficientPrivilege):
            await claim(r, recipe())
        assert (await call(r, "SELECT count(*) FROM catalog.product")) == 0
        h = await committed_product(db, p)
        for query, params in [("SELECT catalog.verify(%s)", (h,)),
                              ("SELECT catalog.move_current(%s, %s, %s, %s, 0, 'x')", ("ixpe", "ixpe.pcube", "01002901", h)),
                              ("SELECT catalog.adopt_orphan(%s, '[]', 'c', 'e', 'p', 'r', 1)", (h,)),
                              ("SELECT catalog._log('x', NULL, '{}')", ())]:
            with pytest.raises(errors.InsufficientPrivilege):
                await call(p, query, *params)
        await r.close(); await p.close()
    asyncio.run(body())


@pytest.mark.parametrize("role_class", ["reader", "producer", "reconciler"])
def test_agents_cannot_write_tables_directly(db, role_class):
    async def body():
        async with await db.agent("producer", "wa") as p:
            h = await committed_product(db, p)
        conn = await db.agent(role_class, "x")
        statements = [
            ("UPDATE catalog.product SET state = 'verified' WHERE recipe_hash = %s", (h,)),
            ("DELETE FROM catalog.product WHERE recipe_hash = %s", (h,)),
            ("UPDATE catalog.object SET sha256 = %s", ("0" * 64,)),
            ("DELETE FROM catalog.object", ()),
            ("INSERT INTO catalog.current VALUES ('b', 'k', 's', %s, 1, 'me', now(), 'r')", (h,)),
            ("DELETE FROM catalog.event", ()),
            ("INSERT INTO catalog.credential (kind, key_id, agent, run) VALUES ('s3', 'K', 'a', 'r')", ()),
            ("CREATE TABLE catalog.mine (x int)", ()),
            ("CREATE TABLE public.mine (x int)", ()),
        ]
        for query, params in statements:
            with pytest.raises(errors.InsufficientPrivilege):
                await conn.execute(query, params)
        await conn.close()
    asyncio.run(body())


def test_append_only_tables_refuse_even_the_owner(db):
    async def body():
        async with await db.agent("producer", "wa") as p:
            await committed_product(db, p)
        async with await db.connect() as admin:
            await admin.execute("SET ROLE owner")
            for query in ["UPDATE catalog.object SET size = 0", "DELETE FROM catalog.object",
                          "TRUNCATE catalog.object CASCADE", "UPDATE catalog.event SET action = 'x'",
                          "DELETE FROM catalog.event", "TRUNCATE catalog.event", "TRUNCATE catalog.current_history",
                          "TRUNCATE catalog.object_check"]:
                with pytest.raises(errors.InsufficientPrivilege, match="append-only"):
                    await admin.execute(query)
    asyncio.run(body())


# ---------------------------------------------------------------- reconciler


def test_verify_paths(db):
    async def body():
        p = await db.agent("producer", "wa")
        async with await db.agent("reconciler", "rec") as r:
            h = (await claim(p, recipe()))["recipe_hash"]
            with pytest.raises(errors.InvalidParameterValue, match="not committed"):
                await catalog.verify(r, h)
            await commit(p, h, objects(h))
            with pytest.raises(errors.InvalidParameterValue, match="reason"):
                await catalog.verify(r, h, "failed")
            with pytest.raises(errors.InvalidParameterValue, match="outcome"):
                await catalog.verify(r, h, "fine")
            assert (await catalog.verify(r, h))["status"] == "verified"
            row = await catalog.product(r, h)
            assert row["state"] == "verified" and row["verified_at"] is not None
            assert (await catalog.verify(r, h, "failed", "sha256 differs on disk"))["status"] == "failed"
            row = await catalog.product(r, h)
            assert (row["state"], row["reason"]) == ("failed", "sha256 differs on disk")
            assert [x["recipe_hash"] for x in await catalog.products(r, "failed")] == [h]
            # its objects hold the recipe's keys: no retry, no adoption
            assert await claim(p, recipe()) == {"status": "failed", "recipe_hash": h, "reason": "sha256 differs on disk"}
            with pytest.raises(errors.InvalidParameterValue, match="only an expired claim adopts"):
                await catalog.adopt_orphan(r, h, objects(h, 2)[1:], toolkit_commit="c", env_fingerprint="e", generation=1,
                                           producer=f"wa_{db.tag}", reason="more")
        await p.close()
    asyncio.run(body())


def test_adopt_orphan(db):
    async def body():
        p = await db.agent("producer", "wa")
        me = f"wa_{db.tag}"

        async def adopt(h, producer=me, reason="object under the recipe key, no commit", objs=None, generation=1):
            return await catalog.adopt_orphan(r, h, objs or objects(h), toolkit_commit="c", env_fingerprint="e",
                                              generation=generation,
                                              producer=producer, reason=reason)
        async with await db.agent("reconciler", "rec") as r:
            with pytest.raises(errors.InvalidParameterValue, match="no product row"):
                await adopt("e" * 64)
            h = (await claim(p, recipe(), "300 milliseconds"))["recipe_hash"]  # the producer stops after its PUT
            with pytest.raises(errors.InvalidParameterValue, match="only an expired claim adopts"):
                await adopt(h)  # the claim is live: its holder may still commit
            time.sleep(0.4)
            with pytest.raises(errors.InvalidParameterValue, match="reason"):
                await adopt(h, reason="")
            with pytest.raises(errors.InvalidParameterValue, match=f"names {me}, not someone"):
                await adopt(h, producer="someone")
            with pytest.raises(errors.InvalidParameterValue, match="at generation 1, not 2"):
                await adopt(h, generation=2)
            assert (await adopt(h))["status"] == "committed"
            row = await catalog.product(r, h)
            assert (row["state"], row["owner"], row["toolkit_commit"]) == ("committed", me, "c")
            assert [o["created_by"] for o in await catalog.objects(r, h)] == [me]
            with pytest.raises(errors.InvalidParameterValue, match="only an expired claim adopts"):
                await adopt(h, objs=objects(h, 2)[1:])
            # a producer's own fail is final: nothing it stored is adopted over it
            h2 = (await claim(p, recipe(subject="x")))["recipe_hash"]
            await call(p, "SELECT catalog.fail(%s, %s)", h2, "crashed after the PUT")
            with pytest.raises(errors.InvalidParameterValue, match="only an expired claim adopts"):
                await adopt(h2)
        await p.close()
    asyncio.run(body())


def test_a_pointer_is_keyed_by_bucket_kind_and_subject(db):
    """Two buckets may use one subject string for different things: each keeps its own pointer, and a
    product's bucket is the one its objects lie in."""
    async def body():
        p = await db.agent("producer", "wa")
        async with await db.agent("reconciler", "rec") as r:
            here = await committed_product(db, p)
            h = (await claim(p, recipe(emin=5.0, emax=8.0)))["recipe_hash"]
            there = [{**o, "bucket": "nicer"} for o in objects(h)]
            await commit(p, h, there)
            assert ((await catalog.product(r, here))["bucket"], (await catalog.product(r, h))["bucket"]) == ("ixpe", "nicer")
            for x in (here, h):
                await catalog.verify(r, x)
            with pytest.raises(errors.InvalidParameterValue, match=r"is a product of \(nicer, ixpe.pcube, 01002901\)"):
                await catalog.move_current(r, "ixpe", "ixpe.pcube", "01002901", h, 0, "wrong bucket")
            assert (await catalog.move_current(r, "ixpe", "ixpe.pcube", "01002901", here, 0, "x"))["status"] == "moved"
            assert (await catalog.move_current(r, "nicer", "ixpe.pcube", "01002901", h, 0, "y"))["status"] == "moved"
            assert (await catalog.current(r, "ixpe", "ixpe.pcube", "01002901"))["recipe_hash"] == here
            assert (await catalog.current(r, "nicer", "ixpe.pcube", "01002901"))["recipe_hash"] == h
            # demoting one bucket's product leaves the other bucket's pointer alone
            await catalog.verify(r, h, "failed", "changed on disk")
            assert (await catalog.current(r, "nicer", "ixpe.pcube", "01002901"))["recipe_hash"] is None
            assert (await catalog.current(r, "ixpe", "ixpe.pcube", "01002901"))["recipe_hash"] == here
            assert [x["bucket"] for x in await catalog.current_history(r, "nicer", "ixpe.pcube", "01002901")] == ["nicer"] * 2
        # one bucket per product
        h3 = (await claim(p, recipe(emin=6.0, emax=8.0)))["recipe_hash"]
        mixed = objects(h3, 2)
        mixed[1]["bucket"] = "nicer"
        with pytest.raises(errors.InvalidParameterValue, match="lie in one bucket of one store; these name db/ixpe, db/nicer"):
            await commit(p, h3, mixed)
        await p.close()
    asyncio.run(body())


def test_move_current_compare_and_swap(db):
    async def body():
        p = await db.agent("producer", "wa")
        async with await db.agent("reconciler", "rec") as r:
            h1 = await committed_product(db, p)
            h2 = await committed_product(db, p, recipe(emin=3.0, emax=8.0))
            other = await committed_product(db, p, recipe(subject="01002902"))
            with pytest.raises(errors.InvalidParameterValue, match="only a verified"):
                await catalog.move_current(r, "ixpe", "ixpe.pcube", "01002901", h1, 0, "newest")
            for h in (h1, h2, other):
                await catalog.verify(r, h)
            with pytest.raises(errors.InvalidParameterValue, match="is a product of"):
                await catalog.move_current(r, "ixpe", "ixpe.pcube", "01002901", other, 0, "newest")
            with pytest.raises(errors.InvalidParameterValue, match="reason"):
                await catalog.move_current(r, "ixpe", "ixpe.pcube", "01002901", h1, 0, "")

            moved = await catalog.move_current(r, "ixpe", "ixpe.pcube", "01002901", h1, 0, "first verified")
            assert moved["status"] == "moved" and moved["current"]["seq"] == 1
            lost = await catalog.move_current(r, "ixpe", "ixpe.pcube", "01002901", h2, 0, "stale read")
            assert lost["status"] == "mismatch"
            assert (lost["current"]["recipe_hash"], lost["current"]["seq"]) == (h1, 1)
            moved = await catalog.move_current(r, "ixpe", "ixpe.pcube", "01002901", h2, 1, "newer verified")
            assert (moved["current"]["recipe_hash"], moved["current"]["seq"]) == (h2, 2)
            lost = await catalog.move_current(r, "ixpe", "ixpe.pcube", "01002901", h1, 1, "stale read")
            assert lost["status"] == "mismatch" and lost["current"]["seq"] == 2
            nothing = await catalog.move_current(r, "ixpe", "ixpe.pcube", "01002902", other, 4, "no pointer yet")
            assert nothing == {"status": "mismatch", "current": None}

            hist = await catalog.current_history(r, "ixpe", "ixpe.pcube", "01002901")
            assert [(x["seq"], x["recipe_hash"], x["previous"]) for x in hist] == [(1, h1, None), (2, h2, h1)]
            assert {x["moved_by"] for x in hist} == {f"rec_{db.tag}"}
            assert (await catalog.current(r, "ixpe", "ixpe.pcube", "01002901"))["recipe_hash"] == h2

        # the human (owner) rolls back with the same call
        async with await db.connect() as admin:
            back = await catalog.move_current(admin, "ixpe", "ixpe.pcube", "01002901", h1, 2, "roll back: h2 mis-binned")
            assert (back["status"], back["current"]["recipe_hash"], back["current"]["moved_by"]) == ("moved", h1, "postgres")
            with pytest.raises(errors.InsufficientPrivilege, match="append-only"):
                await admin.execute("DELETE FROM catalog.current_history")
        assert (await actions(db)).count("move_current.mismatch") == 3
        await p.close()
    asyncio.run(body())


@pytest.mark.parametrize("expected", [0, 1])
def test_a_racing_move_waits_then_gets_the_winner(db, expected):
    """B's move lands while A's is uncommitted, both reading the same seq: B waits, then loses to A."""
    async def body():
        p = await db.agent("producer", "wa")
        a, b = await db.agent("reconciler", "ra"), await db.agent("reconciler", "rb")
        h0, h1, h2 = [await committed_product(db, p, recipe(emin=float(i), emax=8.0)) for i in range(3)]
        for h in (h0, h1, h2):
            await catalog.verify(a, h)
        if expected:
            await catalog.move_current(a, "ixpe", "ixpe.pcube", "01002901", h0, 0, "first")
        await a.set_autocommit(False)
        async with a.transaction():
            assert (await catalog.move_current(a, "ixpe", "ixpe.pcube", "01002901", h1, expected, "a"))["status"] == "moved"
            racing = asyncio.create_task(catalog.move_current(b, "ixpe", "ixpe.pcube", "01002901", h2, expected, "b"))
            await asyncio.sleep(0.3)
            assert not racing.done()
        lost = await racing
        assert lost["status"] == "mismatch"
        assert (lost["current"]["recipe_hash"], lost["current"]["seq"]) == (h1, expected + 1)
        for c in (p, a, b):
            await c.close()
    asyncio.run(body())


def test_demoting_the_current_product_moves_the_pointer_back(db):
    async def body():
        p = await db.agent("producer", "wa")
        async with await db.agent("reconciler", "rec") as r:
            h1, h2, h3 = [await committed_product(db, p, recipe(emin=float(i), emax=8.0)) for i in range(3)]
            for h in (h1, h2, h3):
                await catalog.verify(r, h)
            await catalog.move_current(r, "ixpe", "ixpe.pcube", "01002901", h1, 0, "first")
            await catalog.move_current(r, "ixpe", "ixpe.pcube", "01002901", h2, 1, "newer")
            await catalog.verify(r, h2, "failed", "sha256 differs on disk")
            now = await catalog.current(r, "ixpe", "ixpe.pcube", "01002901")
            assert (now["recipe_hash"], now["seq"], now["moved_by"]) == (h1, 3, f"rec_{db.tag}")
            await catalog.verify(r, h1, "failed", "object missing")
            now = await catalog.current(r, "ixpe", "ixpe.pcube", "01002901")
            assert (now["recipe_hash"], now["seq"]) == (None, 4)
            hist = await catalog.current_history(r, "ixpe", "ixpe.pcube", "01002901")
            assert [(x["seq"], x["recipe_hash"], x["previous"]) for x in hist] == \
                [(1, h1, None), (2, h2, h1), (3, h1, h2), (4, None, h1)]
            moved = await catalog.move_current(r, "ixpe", "ixpe.pcube", "01002901", h3, 4, "only verified left")
            assert (moved["current"]["recipe_hash"], moved["current"]["seq"]) == (h3, 5)
        await p.close()
    asyncio.run(body())


def test_a_move_racing_a_demotion_is_refused(db):
    """verify demotes h while move_current(h) is in flight: the move waits, then refuses."""
    async def body():
        p = await db.agent("producer", "wa")
        a, b = await db.agent("reconciler", "ra"), await db.agent("reconciler", "rb")
        h = await committed_product(db, p)
        await catalog.verify(a, h)
        await a.set_autocommit(False)
        async with a.transaction():
            await catalog.verify(a, h, "failed", "object missing")
            racing = asyncio.create_task(catalog.move_current(b, "ixpe", "ixpe.pcube", "01002901", h, 0, "newest"))
            await asyncio.sleep(0.3)
            assert not racing.done()
        with pytest.raises(errors.InvalidParameterValue, match="only a verified"):
            await racing
        assert await catalog.current(b, "ixpe", "ixpe.pcube", "01002901") is None
        for c in (p, a, b):
            await c.close()
    asyncio.run(body())


# ---------------------------------------------------------------- credentials


def test_issue_and_end_roles(db):
    async def body():
        name = f"wa_{db.tag}"
        conn = await db.agent("producer", "wa")
        async with await db.connect() as admin:
            with pytest.raises(catalog.CatalogError, match="not issued by this catalog"):
                await catalog.issue_role(admin, name="postgres", role_class="reader", run="run1", password="x",
                                         valid_until=datetime.now(UTC) + timedelta(hours=1))
            with pytest.raises(catalog.CatalogError, match="role class"):
                await catalog.issue_role(admin, name="z", role_class="owner", run="run1", password="x",
                                         valid_until=datetime.now(UTC) + timedelta(hours=1))
            with pytest.raises(catalog.CatalogError, match="none of"):
                await catalog.issue_role(admin, name="reconciler", role_class="reader", run="run1", password="x",
                                         valid_until=datetime.now(UTC) + timedelta(hours=1))
            with pytest.raises(catalog.CatalogError, match="not issued by this catalog"):
                await catalog.end_role(admin, name="postgres")
            await conn.execute("ALTER ROLE CURRENT_USER SET statement_timeout = 0")  # the agent changes itself

            await catalog.end_role(admin, name=name)
            with pytest.raises(psycopg.OperationalError):
                await conn.execute("SELECT 1")  # its open session was terminated
            with pytest.raises(psycopg.OperationalError, match="not permitted to log in"):
                await db.connect(name)

            # the next run re-issues the same agent name, here in another class
            await catalog.issue_role(admin, name=name, role_class="reader", run="run2", password="y",
                                     valid_until=datetime.now(UTC) + timedelta(hours=1))
            async with await db.connect(name) as again:
                with pytest.raises(errors.InsufficientPrivilege):
                    await claim(again, recipe())
            cur = await admin.execute("SELECT run, revoked_at IS NULL FROM catalog.credential "
                                      "WHERE kind = 'postgres' AND key_id = %s ORDER BY id", (name,))
            assert await cur.fetchall() == [("run1", False), ("run2", True)]
            cur = await admin.execute("SELECT rolsuper, rolcreaterole, rolvaliduntil IS NOT NULL, rolconfig "
                                      "FROM pg_roles WHERE rolname = %s", (name,))
            assert await cur.fetchone() == (False, False, True, None)  # the agent's own settings are gone
            cur = await admin.execute("SELECT rolpassword FROM pg_authid WHERE rolname = %s", (name,))
            assert (await cur.fetchone())[0].startswith("SCRAM-SHA-256$")
    asyncio.run(body())


def test_s3_credentials_are_recorded_and_revoked(db):
    async def body():
        async with await db.connect() as admin:
            await catalog.record_credential(admin, key_id="AKIA1", agent="analyst_worker_0", run="run1")
            with pytest.raises(errors.UniqueViolation):
                await catalog.record_credential(admin, key_id="AKIA1", agent="lib", run="run1")
            await catalog.revoke_credential(admin, key_id="AKIA1")
            with pytest.raises(catalog.CatalogError, match="no live s3 credential"):
                await catalog.revoke_credential(admin, key_id="AKIA1")
        assert await actions(db) == ["credential.issue", "credential.revoke"]
    asyncio.run(body())


def test_check_schema_refuses_roles_outside_the_catalog(db):
    async def body():
        stray = f"stray_{db.tag}"
        async with await db.connect() as admin:
            await admin.execute(f"CREATE ROLE {stray} LOGIN IN ROLE producer")
            with pytest.raises(catalog.CatalogError, match=f"login {stray} holds producer without a live credential"):
                await catalog.check_schema(db.dsn())
            await admin.execute(f"DROP ROLE {stray}")
            await catalog.check_schema(db.dsn())
            await admin.execute("ALTER ROLE reader LOGIN")
            try:
                with pytest.raises(catalog.CatalogError, match="reader can log in"):
                    await catalog.check_schema(db.dsn())
            finally:
                await admin.execute("ALTER ROLE reader NOLOGIN")
    asyncio.run(body())
