"""The reconciler: the runtime's pass that keeps a store's catalog true to its bytes (item S6).

One pass, :func:`reconcile`, runs as a ``reconciler`` login the runtime issues for itself
(:func:`reconciler_session`, through ``catalog.issue_role``) and reads the store with its admin key:

1. **Sweep orphans.** Every object version the store lists that no ``catalog.object`` row records is
   read back (HEAD) for its immutable metadata (:data:`META_RECIPE_HASH` …). The versions an expired
   claim's producer stored are adopted (``catalog.adopt_orphan``) when its :data:`RECIPE_FILE` (written
   last, :class:`RecipeFile`) is among them and every object it lists is present; one whose live claim
   is still running is pending; a version at a recorded key of a committed product, carrying that
   product's recipe hash with other bytes, makes the product ``nondeterministic``; anything else
   (including what a producer stored before its own ``fail``, which is final) is flagged.
2. **Verify.** Each ``committed`` product's objects are hashed by the runtime (GET + SHA-256) and
   checked against their rows: listed, same size, same bytes; the version's metadata and the store's
   own full-object SHA-256 (HEAD, ChecksumMode) are a second check. Verdict through ``catalog.verify``.
   Each ``verified`` product's objects get the cheap check: listed, same size, and the size and
   LastModified the last hash recorded (``catalog.object_check``); a disagreement is hashed again. A
   version gone or changed on disk (an edit outside the gateway keeps its version id and stored
   checksum, probe P9) fails its product.
3. **Promote.** For each (bucket, kind, subject), ``current`` moves forward to the newest (``claimed_at``)
   qualifying product: verified, made by a manifest key of a declared toolkit at a commit reachable
   from that toolkit's authoring branch, not synthetic, its committing call not flagged failed,
   undeclared_write or unverified (``catalog.product_flag``) (``catalog.move_current``, compare-and-swap on ``seq``; on
   a mismatch the pointer is re-read and the decision made again, at most :data:`PROMOTE_ATTEMPTS`
   times). A pointer the human's own login (``ar_human``) moved holds until a product claimed after that
   move qualifies.

Findings go to the catalog's event log (``catalog.flag``, idempotent) and to the returned
:class:`Report`, which the runtime records. :func:`check_store_integrity` is the preflight item: the
cheap check only, read-only, with the admin credentials.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
import re
import secrets
import subprocess
import weakref
from collections import defaultdict
from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from psycopg import errors
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from auto_research.store import catalog
from auto_research.store.config import LocalStore, RemoteStore, stores

Store = LocalStore | RemoteStore

#: The immutable ``x-amz-meta-*`` names the toolkit's helper writes on every object it stores (S8
#: mirrors them); boto3 returns them in ``Metadata`` without the ``x-amz-meta-`` prefix.
META_RECIPE_HASH = "recipe-hash"
META_SHA256 = "sha256"
META_PRODUCER = "producer"
META_TOOLKIT_COMMIT = "toolkit-commit"
META_ENV_FINGERPRINT = "env-fingerprint"
#: The completeness marker: the helper writes it LAST in a product's folder, listing every other object
#: of the product (:class:`RecipeFile`); adoption requires it and every object it lists.
RECIPE_FILE = "recipe.json"
#: A recipe file larger than this is not read (it lists objects, it holds none).
RECIPE_FILE_MAX = 16 << 20
#: How often a promotion re-reads the pointer after a compare-and-swap mismatch before it is flagged.
PROMOTE_ATTEMPTS = 3
#: How long a reconciler login may open new sessions (an open session outlives it).
SESSION_LIFETIME = timedelta(hours=1)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CHUNK = 1 << 20
_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
_SESSION_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary())


class RecipeObject(BaseModel):
    """One object a product stored, as its :data:`RECIPE_FILE` lists it (in the recipe file's bucket)."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)


class RecipeFile(BaseModel):
    """The :data:`RECIPE_FILE` the toolkit's helper (S8) writes last in a product's folder: the recipe as
    claimed, its hash, the claim generation that produced it, and every other object it stored."""

    model_config = ConfigDict(extra="forbid")

    recipe_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation: int = Field(ge=1, description="catalog.claim's answer for this production")
    recipe: dict[str, Any]
    objects: list[RecipeObject] = Field(min_length=1)


@dataclass
class Finding:
    """One flag: ``action`` (logged as ``flag.<action>``), the product it concerns (or None),
    ``detail`` (the object and the reason), and whether this pass logged it first (``logged``)."""

    action: str
    recipe_hash: str | None
    detail: dict[str, Any]
    logged: bool = False


@dataclass
class Report:
    """What one pass did to one store; :meth:`as_dict` is what the runtime records."""

    store: str
    verified: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    nondeterministic: dict[str, str] = field(default_factory=dict)
    adopted: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    promoted: list[dict[str, Any]] = field(default_factory=list)
    flags: list[Finding] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- the reconciler's login


@asynccontextmanager
async def reconciler_session(store: Store, *, run: str) -> AsyncIterator[psycopg.AsyncConnection]:
    """An autocommit connection as the runtime's ``reconciler`` login for ``run``, issued on entry
    (``catalog.issue_role``, a fresh password) and ended on exit (``catalog.end_role``). Sessions of
    one login are serialised within the process, so ending one never cuts another."""
    name = _login_name(run)
    lock = _SESSION_LOCKS.setdefault(asyncio.get_running_loop(), {}).setdefault(name, asyncio.Lock())
    async with lock:
        password = secrets.token_urlsafe(24)
        admin_dsn = store.admin_dsn()
        async with await psycopg.AsyncConnection.connect(admin_dsn, autocommit=True) as admin:
            await catalog.issue_role(admin, name=name, role_class="reconciler", run=run, password=password,
                                     valid_until=datetime.now(UTC) + SESSION_LIFETIME)
        try:
            async with await psycopg.AsyncConnection.connect(
                    make_conninfo(admin_dsn, user=name, password=password), autocommit=True) as conn:
                yield conn
        finally:
            async with await psycopg.AsyncConnection.connect(admin_dsn, autocommit=True) as admin:
                await catalog.end_role(admin, name=name)


def _login_name(run: str) -> str:
    """``ar_reconciler_<run>``, folded to a Postgres role name (a hash suffix keeps long runs apart)."""
    slug = re.sub(r"[^a-z0-9_]", "_", run.lower())
    name = f"ar_reconciler_{slug}"
    if len(name) > 63:
        name = f"{name[:50]}_{hashlib.sha256(run.encode()).hexdigest()[:12]}"
    return name


# ---------------------------------------------------------------- the pass


async def reconcile(store: Store, *, run: str, conn: psycopg.AsyncConnection | None = None,
                    s3: Any = None) -> Report:
    """Sweep, verify and promote ``store`` once; returns the :class:`Report`.

    ``conn``: an autocommit connection as a ``reconciler`` login (the runtime may hold one for the
    run); without it, one is issued for ``run`` and ended afterwards (:func:`reconciler_session`).
    ``s3``: the reconciler's own store keys (S4's ``keys.Credentials``: ``access_key_id``,
    ``secret_access_key``; a reader-class policy is enough, every read is pinned to a version); without
    them the pass signs with the admin key (the CLI).
    Qualifying for "current" is judged against the declared toolkits (``toolkit_contract``). Raises
    when a bucket cannot be listed: nothing is concluded from a partial listing.
    """
    if conn is None:
        async with reconciler_session(store, run=run) as session:
            return await reconcile(store, run=run, conn=session, s3=s3)
    report = Report(store=store.name)
    s3 = _client(store, s3)
    listed_at = await _clock(conn)
    listed = await asyncio.to_thread(_list_versions, s3, store.buckets)
    await _sweep_orphans(conn, s3, store, listed, await _rows(conn, store.name), report)
    await _check_products(conn, s3, store, listed, listed_at, await _rows(conn, store.name), report)
    await _promote(conn, store, _Qualifier(conn, store), report)
    return report


async def _sweep_orphans(conn, s3, store: Store, listed: dict, rows: list[dict], report: Report) -> None:
    recorded = {(r["bucket"], r["key"], r["version_id"]) for r in rows}
    by_hash: dict[str | None, list[tuple[tuple[str, str, str], dict]]] = defaultdict(list)
    for where in sorted(set(listed) - recorded):
        try:
            head = await asyncio.to_thread(_head, s3, *where)
        except Exception as exc:  # noqa: BLE001 - one unreadable version is flagged, the pass goes on
            await _flag(conn, report, "unreachable", None, _where(where, reason=f"HEAD failed: {exc}"))
            continue
        meta = head.get("Metadata") or {}
        by_hash[meta.get(META_RECIPE_HASH)].append((where, head))
    products = {p["recipe_hash"]: p for p in await _products(conn, [h for h in by_hash if h])}
    for recipe_hash, versions in by_hash.items():
        p = products.get(recipe_hash)
        if p is None:
            why = (f"its {META_RECIPE_HASH} names no product" if recipe_hash
                   else f"it carries no {META_RECIPE_HASH} metadata")
            for where, _ in versions:
                await _flag(conn, report, "orphan", recipe_hash, _where(where, reason=f"no row records it; {why}"))
        elif p["state"] == "claimed" and not p["expired"]:
            report.pending.append(recipe_hash)
        elif p["state"] == "claimed":
            await _adopt(conn, s3, store, p, versions, report)
        elif p["state"] in ("committed", "verified"):
            await _extra_versions(conn, s3, store, p, versions, report)
        else:
            for where, _ in versions:
                await _flag(conn, report, "orphan", recipe_hash,
                            _where(where, reason=f"no row records it; its product is {p['state']} (final)"))


async def _adopt(conn, s3, store: Store, p: dict, versions: list, report: Report) -> None:
    """Adopt what an expired claim's producer stored: the objects its complete :data:`RECIPE_FILE`
    lists, and that file. Without one, nothing is adopted (the producer stopped before it finished)."""
    recipe_hash = p["recipe_hash"]
    heads = dict(versions)
    problems, chosen = [], None
    for where in sorted(w for w in heads if w[1].rsplit("/", 1)[-1] == RECIPE_FILE):
        try:
            problem, manifest = await _recipe_file_problem(s3, p, where, heads)
        except Exception as exc:  # noqa: BLE001 - an unreadable recipe file adopts nothing
            problem, manifest = f"could not be read: {exc}", None
        if problem is None:
            chosen = (where, manifest)
            break
        problems.append(f"{_name(where)}: {problem}")
    if chosen is None:
        why = "; ".join(problems) or f"no {RECIPE_FILE} among its versions (stored before the product was complete)"
        for where in heads:
            await _flag(conn, report, "orphan", recipe_hash, _where(where, reason=f"not adopted: {why}"))
        return
    where, manifest = chosen
    members = [(where[0], o.key, o.version_id) for o in manifest.objects] + [where]
    meta = heads[where]["Metadata"]
    objects = [{"store": store.name, "bucket": b, "key": k, "version_id": v,
                "sha256": heads[(b, k, v)]["Metadata"][META_SHA256], "size": heads[(b, k, v)]["ContentLength"]}
               for b, k, v in members]
    try:
        await catalog.adopt_orphan(conn, recipe_hash, objects, toolkit_commit=meta[META_TOOLKIT_COMMIT],
                                   env_fingerprint=meta[META_ENV_FINGERPRINT], producer=meta[META_PRODUCER],
                                   reason=f"reconciler: stored complete under an expired claim "
                                          f"(generation {manifest.generation}), never committed",
                                   generation=manifest.generation)
    except psycopg.Error as exc:
        now = await catalog.product(conn, recipe_hash)
        if now is None or now["state"] not in ("committed", "verified"):  # committed meanwhile: nothing to say
            await _flag(conn, report, "adopt_refused", recipe_hash, {"reason": _message(exc)})
        return
    report.adopted.append(recipe_hash)
    for other in set(heads) - set(members):
        await _flag(conn, report, "orphan", recipe_hash,
                    _where(other, reason=f"not listed by the adopted {_name(where)}"))


async def _recipe_file_problem(s3, p: dict, where: tuple[str, str, str],
                               heads: dict) -> tuple[str | None, RecipeFile | None]:
    """Why the recipe file at ``where`` does not complete ``p``'s production, or ``(None, manifest)``."""
    head = heads[where]
    if head["ContentLength"] > RECIPE_FILE_MAX:
        return f"is {head['ContentLength']} bytes, more than a recipe file holds", None
    body = await asyncio.to_thread(_read_version, s3, *where)
    try:
        manifest = RecipeFile.model_validate_json(body)
    except ValidationError as exc:
        return f"is not a recipe file ({exc.error_count()} problems: {exc.errors()[0]['msg']})", None
    meta = head.get("Metadata") or {}
    provenance = (meta.get(META_PRODUCER), meta.get(META_TOOLKIT_COMMIT), meta.get(META_ENV_FINGERPRINT))
    if manifest.recipe_hash != p["recipe_hash"] or manifest.recipe != p["recipe"]:
        return "names another recipe", None
    if manifest.generation != p["generation"]:
        return f"was written by generation {manifest.generation}; the claim is at {p['generation']}", None
    if provenance[0] != p["owner"]:
        return f"its {META_PRODUCER} {provenance[0]!r} is not the claim's owner {p['owner']!r}", None
    if not provenance[1] or not provenance[2]:
        return f"it lacks {META_TOOLKIT_COMMIT} or {META_ENV_FINGERPRINT} metadata", None
    if meta.get(META_SHA256) != hashlib.sha256(body).hexdigest():
        return f"its {META_SHA256} metadata is not the sha256 of its bytes", None
    for o in manifest.objects:
        member = heads.get((where[0], o.key, o.version_id))
        if member is None:
            return f"lists {o.key} version {o.version_id}, which is not among the unrecorded versions", None
        m = member.get("Metadata") or {}
        if member["ContentLength"] != o.size or m.get(META_SHA256) != o.sha256:
            return f"lists {o.key} version {o.version_id} with a size or sha256 the version does not carry", None
        if (m.get(META_PRODUCER), m.get(META_TOOLKIT_COMMIT), m.get(META_ENV_FINGERPRINT)) != provenance:
            return f"lists {o.key} version {o.version_id}, which names another producer or provenance", None
    return None, manifest


async def _extra_versions(conn, s3, store: Store, p: dict, versions: list, report: Report) -> None:
    """Versions carrying a committed product's recipe hash that no row recorded when the pass read the
    rows: other bytes at one of its recorded keys are a second output of one recipe (nondeterministic,
    one verdict); anything else is flagged. Rows are read again first, so a commit landing during the
    pass is not mistaken for an orphan. A version hashed before at its listed size and LastModified
    (``object_check``) is not hashed again."""
    recipe_hash = p["recipe_hash"]
    rows = await catalog.objects(conn, recipe_hash)
    checks = await _checks(conn, store.name)
    now_recorded = {(r["bucket"], r["key"], r["version_id"]) for r in rows}
    mine = {(r["bucket"], r["key"]): r["sha256"] for r in rows}
    for where, head in versions:
        if where in now_recorded or recipe_hash in report.nondeterministic:
            continue
        recorded = mine.get(where[:2])
        if recorded is None:
            await _flag(conn, report, "orphan", recipe_hash,
                        _where(where, reason="carries the product's recipe hash at a key it never recorded"))
            continue
        check = checks.get(where)
        if check is not None and (check["size"], check["mtime"]) == (head["ContentLength"], head["LastModified"]):
            sha256 = check["sha256"]
        else:
            try:
                sha256 = await asyncio.to_thread(_hash_version, s3, *where)
            except Exception as exc:  # noqa: BLE001
                await _flag(conn, report, "unreachable", recipe_hash, _where(where, reason=f"GET failed: {exc}"))
                continue
            await _record_check(conn, store.name, where, head["ContentLength"], head["LastModified"], sha256)
        if sha256 == recorded:
            await _flag(conn, report, "orphan", recipe_hash,
                        _where(where, reason="a second version of a recorded object, with the same bytes"))
            continue
        reason = (f"{where[0]}/{where[1]} holds a second version {where[2]} carrying this recipe hash "
                  f"with other bytes (sha256 {sha256}, recorded {recorded})")
        await _flag(conn, report, "nondeterministic", recipe_hash, _where(where, reason=reason))
        await _verdict(conn, report, recipe_hash, "nondeterministic", reason)


async def _check_products(conn, s3, store: Store, listed: dict, listed_at: datetime, rows: list[dict],
                          report: Report) -> None:
    """Verify each committed product (the runtime's hash, then HEAD); the cheap check on each verified
    one. A row written after the listing began, or in a bucket the store does not declare, gets no
    verdict."""
    checks = await _checks(conn, store.name)
    by_product: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_product[r["recipe_hash"]].append(r)
    for recipe_hash, objs in by_product.items():
        state = objs[0]["state"]
        if state in ("failed", "nondeterministic"):
            for r in objs:  # terminal already: a missing object is only recorded
                if r["bucket"] in store.buckets and _key(r) not in listed and r["created_at"] < listed_at:
                    await _flag(conn, report, "missing", recipe_hash, _where(_key(r), reason="not in the store"))
            continue
        if state not in ("committed", "verified"):
            continue
        problems, unread = [], False
        for r in objs:
            if r["bucket"] not in store.buckets:
                await _flag(conn, report, "unchecked", recipe_hash,
                            _where(_key(r), reason=f"bucket {r['bucket']} is not one the store declares"))
                unread = True
                continue
            if _key(r) not in listed and r["created_at"] >= listed_at:
                unread = True  # committed while the pass ran: the next pass checks it
                continue
            try:
                problem = await _object_problem(s3, r, listed, checks, first=state == "committed",
                                                record=lambda *a: _record_check(conn, store.name, *a))
            except Exception as exc:  # noqa: BLE001 - no verdict on what could not be read
                await _flag(conn, report, "unreachable", recipe_hash, _where(_key(r), reason=f"read failed: {exc}"))
                unread = True
                continue
            if problem:
                action, reason = problem
                problems.append(reason)
                await _flag(conn, report, action, recipe_hash, _where(_key(r), reason=reason))
        if problems:
            await _verdict(conn, report, recipe_hash, "failed", "; ".join(problems))
        elif state == "committed" and not unread:
            await _verdict(conn, report, recipe_hash, "verified", None)


async def _object_problem(s3, r: dict, listed: dict, checks: dict, *, first: bool,
                          record=None) -> tuple[str, str] | None:
    """Why recorded object ``r`` no longer holds the bytes its row names: ``(action, reason)``, or None.

    ``first`` (a committed product's verify): the runtime hashes the bytes, then reads the version's
    metadata and the store's own full-object SHA-256 (HEAD) as a second check. Otherwise the cheap
    check: the version is listed with its row's size and with the size and LastModified of its last
    recorded hash (``checks``); a disagreement is hashed again. Each hash is passed to ``record``.
    Action ``edited``: the version changed after its row was written or after its last hash;
    ``mismatch``: the row names other bytes than the store holds; ``missing``: the version is gone.
    """
    where = _key(r)
    version = listed.get(where)
    if version is None:
        return "missing", f"{_name(where)} is not in the store"
    size, mtime = version["Size"], version["LastModified"]
    check = checks.get(where)
    known = check is not None and (check["size"], check["mtime"]) == (size, mtime)
    changed = mtime > r["created_at"] or (check is not None and not known)
    if size != r["size"]:
        return ("edited" if changed else "mismatch"), f"{_name(where)} is {size} bytes, recorded {r['size']}"
    if first or not known:
        sha256 = await asyncio.to_thread(_hash_version, s3, *where)
        if record is not None:
            await record(where, size, mtime, sha256)
    else:
        sha256 = check["sha256"]
    if sha256 != r["sha256"]:
        return (("edited", f"{_name(where)} changed after it was recorded: its bytes hash to sha256 {sha256}, "
                           f"recorded {r['sha256']}") if changed else
                ("mismatch", f"{_name(where)} bytes hash to sha256 {sha256}, recorded {r['sha256']}"))
    if first:
        head = await asyncio.to_thread(_head, s3, *where)
        meta = head.get("Metadata") or {}
        for name, expected in ((META_RECIPE_HASH, r["recipe_hash"]), (META_SHA256, r["sha256"])):
            if meta.get(name) not in (None, expected):
                return "mismatch", f"{_name(where)} carries {name} {meta[name]}, recorded {expected}"
        stored = _stored_sha256(head)
        if stored is not None and stored != r["sha256"]:
            return "mismatch", f"{_name(where)} was stored with sha256 {stored}, recorded {r['sha256']}"
    return None


async def _promote(conn, store: Store, qualifier: "_Qualifier", report: Report) -> None:
    """Move each (bucket, kind, subject)'s ``current`` forward to its newest qualifying product, in the
    buckets the store declares (a bucket it no longer declares gets no verdict and no move)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT DISTINCT bucket, kind, subject FROM catalog.product "
                          "WHERE state = 'verified' AND bucket = ANY(%s) ORDER BY bucket, kind, subject",
                          (list(store.buckets),))
        groups = [(row["bucket"], row["kind"], row["subject"]) for row in await cur.fetchall()]
    for group in groups:
        await _promote_one(conn, group, qualifier, report)
    for root, problem in qualifier.git_problems.items():
        await _flag(conn, report, "unqualified", None, {"toolkit_root": root, "reason": problem})


async def _promote_one(conn, group: tuple[str, str, str], qualifier: "_Qualifier", report: Report) -> None:
    """One (bucket, kind, subject): decide, compare-and-swap; on a mismatch or a refusal (the product
    changed meanwhile) read the pointer again and decide again, at most :data:`PROMOTE_ATTEMPTS` times."""
    bucket, kind, subject = group
    for _ in range(PROMOTE_ATTEMPTS):
        pointer = await catalog.current(conn, bucket, kind, subject)
        best = await _newest_qualifying(conn, group, qualifier, pointer)
        if best is None:
            return
        reason = (f"newest qualifying product: verified, made by {best['tool']} at toolkit commit "
                  f"{best['toolkit_commit']}, claimed {best['claimed_at'].isoformat()}")
        try:
            moved = await catalog.move_current(conn, bucket, kind, subject, best["recipe_hash"],
                                               pointer["seq"] if pointer else 0, reason)
        except errors.InvalidParameterValue:  # demoted since it was read: decide again
            continue
        if moved["status"] == "moved":
            report.promoted.append({"bucket": bucket, "kind": kind, "subject": subject,
                                    "recipe_hash": best["recipe_hash"],
                                    "previous": pointer["recipe_hash"] if pointer else None,
                                    "seq": moved["current"]["seq"], "reason": reason})
            return
    await _flag(conn, report, "promotion_contended", None,
                {"bucket": bucket, "kind": kind, "subject": subject,
                 "reason": f"the pointer moved under each of {PROMOTE_ATTEMPTS} attempts"})


async def _newest_qualifying(conn, group: tuple[str, str, str], qualifier: "_Qualifier",
                             pointer: dict | None) -> dict | None:
    """The newest (claimed_at) verified product of ``(bucket, kind, subject)`` that qualifies
    (:class:`_Qualifier`) and is claimed after the current product (forward only: a newer current
    product stays, qualifying in this run or not). A pointer the human moved on purpose
    (``human_override``: ``move_current`` as the human's own login, ``catalog.HUMAN_ROLE``) holds until a
    product claimed after that move qualifies; a move by the admin role or any runtime login, and every
    demotion, is the runtime's and holds nothing."""
    since = None
    if pointer is not None and pointer["recipe_hash"] is not None:
        since = (await catalog.product(conn, pointer["recipe_hash"]))["claimed_at"]
    if pointer is not None and pointer["human_override"]:
        since = max(since, pointer["moved_at"]) if since is not None else pointer["moved_at"]
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT recipe_hash, recipe, recipe ->> 'tool' AS tool, toolkit_commit, claimed_at FROM catalog.product "
            "WHERE bucket = %s AND kind = %s AND subject = %s AND state = 'verified' "
            "AND (%s::timestamptz IS NULL OR claimed_at > %s::timestamptz) "
            "ORDER BY claimed_at DESC, committed_at DESC, recipe_hash DESC", (*group, since, since))
        candidates = await cur.fetchall()
    for p in candidates:
        if await qualifier.qualifies(p):
            return p
    return None


#: The flags live on a product or an ancestor (``catalog.live_flag``: stamped from the calls that made
#: them, item S5, and not cleared by the human) that keep it from becoming current: every receipt flag.
BLOCKING_FLAGS = frozenset({"failed", "synthetic", "undeclared_write", "unverified"})


class _Qualifier:
    """Whether a verified product may become current: made by a tool of a declared toolkit
    (a manifest key, read as data), at a commit reachable from that toolkit's authoring branch, not
    synthetic (no object of it, and no input of it or of its ancestors, lies in a synthetic bucket), and
    neither it nor an ancestor stamped with one of :data:`BLOCKING_FLAGS` or (an ancestor) failed by the
    reconciler's check."""

    def __init__(self, conn, store: Store):
        self.conn = conn
        self.tools: dict | None = None  # read at the first candidate: a pass with none reads no toolkit
        declared = {s.name: s for s in stores()}
        declared[store.name] = store
        self.synthetic_buckets = {(name, b) for name, s in declared.items() for b in s.synthetic_buckets}
        self._commits: dict[tuple[str, str], bool] = {}
        #: toolkit root -> why git could not answer (flagged once per pass as ``unqualified``)
        self.git_problems: dict[str, str] = {}
        self._synthetic: dict[str, bool] = {}
        self._flagged: dict[str, bool] = {}

    async def qualifies(self, p: dict) -> bool:
        if self.tools is None:
            self.tools = _registered_tools()
        if p["tool"] not in self.tools:
            return False
        return (self._reachable(self.tools[p["tool"]], p["toolkit_commit"] or "")
                and not await self.synthetic(p["recipe_hash"], p["recipe"])
                and not await self.flagged(p["recipe_hash"], p["recipe"], own=True))

    def _reachable(self, contract, commit: str) -> bool:
        """Whether ``commit`` is reachable from the toolkit's authoring branch (the runtime's own branch;
        it never looks at the human's trusted one)."""
        key = (str(contract.root), commit)
        if key not in self._commits:
            done = None
            if _COMMIT.match(commit):
                done = subprocess.run(["git", "-C", str(contract.root), "merge-base", "--is-ancestor", commit,
                                       f"refs/heads/{contract.authoring_branch}"], capture_output=True, text=True)
                if done.returncode not in (0, 1):  # 1: not an ancestor (or unknown); else git could not answer
                    branch = subprocess.run(["git", "-C", str(contract.root), "rev-parse", "--verify", "--quiet",
                                             f"refs/heads/{contract.authoring_branch}"], capture_output=True)
                    if branch.returncode != 0:
                        self.git_problems[str(contract.root)] = (
                            f"toolkit {contract.name}: authoring branch {contract.authoring_branch} not found "
                            f"in {contract.root}; nothing it made can become current")
            self._commits[key] = done is not None and done.returncode == 0
        return self._commits[key]

    async def synthetic(self, recipe_hash: str, recipe: dict) -> bool:
        if recipe_hash not in self._synthetic:
            self._synthetic[recipe_hash] = False  # an input never descends from its own product
            objects = await catalog.objects(self.conn, recipe_hash)
            places = [(o["store"], o["bucket"]) for o in objects]
            places += [(i["store"], i["bucket"]) for i in recipe.get("inputs", [])]
            found = any(place in self.synthetic_buckets for place in places)
            for i in recipe.get("inputs", []) if not found else ():
                parent = await _producer_of(self.conn, i)
                if parent is not None and await self.synthetic(parent["recipe_hash"], parent["recipe"]):
                    found = True
                    break
            self._synthetic[recipe_hash] = found
        return self._synthetic[recipe_hash]


    async def flagged(self, recipe_hash: str, recipe: dict, own: bool = False) -> bool:
        """Whether the product or an ancestor carries a blocking flag, or an ancestor is failed or
        nondeterministic. The candidate's own flags are read fresh (``own``); a stamp landing after
        that read is caught by ``catalog.move_current``."""
        if own or recipe_hash not in self._flagged:
            self._flagged[recipe_hash] = False  # an input never descends from its own product
            stamped = await catalog.live_flags(self.conn, recipe_hash)
            found = bool(BLOCKING_FLAGS & stamped)
            for i in recipe.get("inputs", []) if not found else ():
                parent = await _producer_of(self.conn, i)
                if parent is not None and (parent["state"] in ("failed", "nondeterministic")
                                           or await self.flagged(parent["recipe_hash"], parent["recipe"])):
                    found = True
                    break
            self._flagged[recipe_hash] = found
        return self._flagged[recipe_hash]


def _registered_tools() -> dict:
    """Every manifest key of every declared toolkit, with the toolkit's contract (manifests read as
    data, no toolkit code runs)."""
    from auto_research.sandbox.registry import ToolkitRegistry
    from auto_research.toolkit_contract import toolkits

    return {name: contract for contract in toolkits() for name in ToolkitRegistry(contract.root).list_tools()}


async def _producer_of(conn, record: dict) -> dict | None:
    """The product that recorded object ``record`` ({store, bucket, key, version_id}), or None."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT p.recipe_hash, p.recipe, p.state FROM catalog.object o JOIN catalog.product p "
                          "USING (recipe_hash) WHERE o.store = %s AND o.bucket = %s AND o.key = %s "
                          "AND o.version_id = %s",
                          (record["store"], record["bucket"], record["key"], record["version_id"]))
        return await cur.fetchone()



# ---------------------------------------------------------------- the preflight item


async def integrity_problems(store: Store) -> list[str]:
    """The cheap check over every committed or verified product's objects, read-only with the admin
    credentials: each object listed, of its recorded size, and with the size and LastModified of its
    last recorded hash (else hashed, nothing recorded). Returns one line per object that disagrees."""
    s3 = _client(store)
    async with await psycopg.AsyncConnection.connect(store.admin_dsn(), autocommit=True) as conn:
        listed_at = await _clock(conn)
        listed = await asyncio.to_thread(_list_versions, s3, store.buckets)
        rows = [r for r in await _rows(conn, store.name) if r["state"] in ("committed", "verified")
                and r["bucket"] in store.buckets and (_key(r) in listed or r["created_at"] < listed_at)]
        checks = await _checks(conn, store.name)
    problems = []
    for r in rows:
        if problem := await _object_problem(s3, r, listed, checks, first=False):
            problems.append(f"product {r['recipe_hash'][:12]} ({r['kind']}, {r['subject']}): {problem[1]}")
    return problems


async def check_store_integrity() -> None:
    """Preflight ``store_integrity``: no recorded object of a committed or verified product is missing
    or changed on disk. Checks only; the launch's reconcile pass (``reconcile``) marks such a product
    failed and moves its ``current`` back."""
    problems = []
    for store in stores():
        try:
            found = await integrity_problems(store)
        except Exception as exc:  # noqa: BLE001 - every store's problem is reported together
            problems.append(f"store {store.name}: integrity check could not run: {exc}")
            continue
        problems += [f"store {store.name}: {p}" for p in found]
    if problems:
        raise RuntimeError("; ".join(problems) + ". A reconcile pass (python -m auto_research.store.reconcile) "
                           "marks each such product failed and moves its current pointer back.")


# ---------------------------------------------------------------- catalog and S3 helpers


async def _rows(conn, store_name: str) -> list[dict]:
    """Every object row of ``store_name`` with its product's state, kind and subject."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT o.*, p.state, p.kind, p.subject FROM catalog.object o "
                          "JOIN catalog.product p USING (recipe_hash) WHERE o.store = %s ORDER BY o.id",
                          (store_name,))
        return await cur.fetchall()


async def _checks(conn, store_name: str) -> dict[tuple[str, str, str], dict]:
    """The latest recorded hash of each version of ``store_name`` (``catalog.object_check``)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT DISTINCT ON (bucket, key, version_id) bucket, key, version_id, size, mtime, sha256 "
                          "FROM catalog.object_check WHERE store = %s ORDER BY bucket, key, version_id, id DESC",
                          (store_name,))
        return {(r["bucket"], r["key"], r["version_id"]): r for r in await cur.fetchall()}


async def _record_check(conn, store_name: str, where: tuple[str, str, str], size: int, mtime: datetime,
                        sha256: str) -> None:
    async with conn.transaction():
        await conn.execute("SELECT catalog.record_check(%s, %s, %s, %s, %s, %s, %s)",
                           (store_name, *where, size, mtime, sha256))


async def _clock(conn) -> datetime:
    cur = await conn.execute("SELECT clock_timestamp()")
    return (await cur.fetchone())[0]


async def _products(conn, hashes: list[str]) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT p.*, p.lease_until <= clock_timestamp() AS expired "
            "FROM catalog.product p WHERE p.recipe_hash = ANY(%s)", (hashes,))
        return await cur.fetchall()


async def _verdict(conn, report: Report, recipe_hash: str, outcome: str, reason: str | None) -> None:
    """``catalog.verify``; a product that changed state since the pass read it (another reconciler, the
    human) is flagged, not judged."""
    try:
        await catalog.verify(conn, recipe_hash, outcome, reason)
    except errors.InvalidParameterValue as exc:
        await _flag(conn, report, "verdict_refused", recipe_hash,
                    {"outcome": outcome, "reason": reason, "refusal": _message(exc)})
        return
    if outcome == "verified":
        report.verified.append(recipe_hash)
    else:
        getattr(report, outcome)[recipe_hash] = reason


async def _flag(conn, report: Report, action: str, recipe_hash: str | None, detail: dict) -> None:
    async with conn.transaction():
        cur = await conn.execute("SELECT catalog.flag(%s, %s, %s)", (action, recipe_hash, Jsonb(detail)))
        logged = (await cur.fetchone())[0]
    report.flags.append(Finding(action, recipe_hash, detail, logged))


def _client(store: Store, credentials: Any = None):
    """An S3 client signed with ``credentials`` (``access_key_id``, ``secret_access_key``), else with the
    admin key; boto3's own checksum handling is off: the reconciler compares and hashes itself, so a
    changed object reads back as its bytes rather than as an exception."""
    import boto3
    from botocore.config import Config

    if credentials is not None:
        key, secret = credentials.access_key_id, credentials.secret_access_key
    else:
        key, secret = store.admin_s3()
    return boto3.client("s3", endpoint_url=store.s3_endpoint, region_name=store.region, aws_access_key_id=key,
                        aws_secret_access_key=secret,
                        config=Config(s3={"addressing_style": "path"}, connect_timeout=5, read_timeout=60,
                                      retries={"max_attempts": 2}, request_checksum_calculation="when_required",
                                      response_checksum_validation="when_required"))


def _list_versions(s3, buckets: Collection[str]) -> dict[tuple[str, str, str], dict]:
    """Every object version in ``buckets`` (delete markers left out), by (bucket, key, version id)."""
    out = {}
    for bucket in buckets:
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
            for v in page.get("Versions", []):
                out[(bucket, v["Key"], v["VersionId"])] = v
    return out


def _head(s3, bucket: str, key: str, version_id: str) -> dict:
    return s3.head_object(Bucket=bucket, Key=key, VersionId=version_id, ChecksumMode="ENABLED")


def _hash_version(s3, bucket: str, key: str, version_id: str) -> str:
    body = s3.get_object(Bucket=bucket, Key=key, VersionId=version_id)["Body"]
    digest = hashlib.sha256()
    for chunk in iter(lambda: body.read(_CHUNK), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _read_version(s3, bucket: str, key: str, version_id: str) -> bytes:
    return s3.get_object(Bucket=bucket, Key=key, VersionId=version_id)["Body"].read(RECIPE_FILE_MAX + 1)


def _stored_sha256(head: dict) -> str | None:
    """The store's full-object SHA-256 of a version, as hex; None for a multipart (composite) or
    non-SHA-256 checksum."""
    value = head.get("ChecksumSHA256")
    if not value or "-" in value or head.get("ChecksumType", "FULL_OBJECT") != "FULL_OBJECT":
        return None
    try:
        return base64.b64decode(value, validate=True).hex()
    except binascii.Error:
        return None


def _key(r: dict) -> tuple[str, str, str]:
    return r["bucket"], r["key"], r["version_id"]


def _name(where: tuple[str, str, str]) -> str:
    return f"{where[0]}/{where[1]} version {where[2]}"


def _where(where: tuple[str, str, str], **extra) -> dict:
    return {"bucket": where[0], "key": where[1], "version_id": where[2], **extra}


def _message(exc: Exception) -> str:
    diag = getattr(exc, "diag", None)
    return (diag.message_primary if diag is not None and diag.message_primary else str(exc)).strip()


def main(argv: list[str] | None = None) -> int:
    """``python -m auto_research.store.reconcile [store …]``: one pass per named store (default: every
    declared store), the report printed as JSON. For the human; the runtime calls :func:`reconcile`."""
    import json
    import sys

    names = (argv if argv is not None else sys.argv[1:]) or [s.name for s in stores()]
    chosen = [s for s in stores() if s.name in names]
    for store in chosen:
        report = asyncio.run(reconcile(store, run=f"cli-{os.getpid()}"))
        print(json.dumps(report.as_dict(), indent=2, default=str))
    return 0 if len(chosen) == len(names) else 2


if __name__ == "__main__":
    raise SystemExit(main())
