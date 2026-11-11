"""The key adapter: one S3 user and one Postgres login per agent per run (Phase 3 step 0b, S4).

Each agent is its own S3 user and Postgres role for the run (human ruling 2026-11-10): the role is its
policy, the key lives as long as the run, and only the run's end (or a dead agent) revokes it.

```
issue(store, unit, role_class, run)                    revoke(store, unit, run)
  IAM user  <principal>  (versitygw IAM)                 access keys deleted (catalog: revoked)
    inline policy "ar-role"  (role template)             inline policy and user deleted
    access key           ─► catalog.record_credential    Postgres login NOLOGIN, sessions ended
  Postgres login <principal> in role_class                 (catalog.end_role)
    random password, VALID UNTIL issue + ROLE_VALID_FOR
  ─► Credentials: .env() = AWS_* + PG*, for the unit's jailed tool processes only
```

Role templates (inline identity policy over the store's buckets): ``reader`` lists and reads **pinned**
versions only (no ``s3:GetObject``: an unpinned read is refused, ruling (e)); ``producer`` adds writes
(``PutObject``, multipart; no tagging: mutable facts live in the catalog) on every key of the store's buckets (human ruling: every product
lives at ``<bucket>/<kind>/<subject>/<short hash>/``, no separate prefix); the runtime's ``reconciler``
login reads as a reader. Every policy denies deletes, versioning, bucket policy and lifecycle changes.
Create-only writes are the toolkit helper's (``If-None-Match:*``), never the policy's (ruling: option A).

Server-specific code is this adapter only (portability rule): ``store.adapters["keys"]`` names it,
``versitygw_iam`` (built) or ``aws_sts`` (a remote store; not built, refused with its reason).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

#: The inline policy's name on every agent user.
POLICY_NAME = "ar-role"
#: The backstop on a login's life (Postgres VALID UNTIL); the run's end revokes it first.
ROLE_VALID_FOR = timedelta(days=7)
#: The variables a unit's jailed tool processes receive, and nothing else of the credential.
ENV_NAMES = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_ENDPOINT_URL_S3", "AWS_REGION",
             "PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE", "TOOLKIT_STORE")
#: The variable naming the store the keys reach: the name a tool writes into its records (the CI
#: pipeline's throwaway store sets the same, ``store.ci.ENV_STORE``).
STORE_NAME_VARIABLE = "TOOLKIT_STORE"
#: The role classes a key can be issued for: an agent's, or the runtime's reconciler.
ROLE_CLASSES = ("reader", "producer", "reconciler")

_READ = ("s3:GetObjectVersion", "s3:GetObjectVersionTagging")
_LIST = ("s3:ListBucket", "s3:ListBucketVersions")
_WRITE = ("s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts")
_DENIED = ("s3:DeleteObject", "s3:DeleteObjectVersion", "s3:PutBucketVersioning", "s3:PutBucketPolicy",
           "s3:DeleteBucketPolicy", "s3:PutLifecycleConfiguration", "s3:DeleteBucket")


@dataclass(frozen=True)
class Credentials:
    """One unit's keys to one store for one run. Its secrets never reach a log or a prompt: the
    representation hides them, and :meth:`env` is handed only to the unit's jailed tool processes."""

    store: str
    unit: str
    run: str
    role_class: str
    principal: str  # the IAM user and the Postgres login: one name
    access_key_id: str
    secret_access_key: str = field(repr=False)
    pg_password: str = field(repr=False)
    s3_endpoint: str = ""
    region: str = ""
    pg_port: int = 0
    pg_database: str = ""

    def env(self) -> dict[str, str]:
        """The variables a tool process reaches the store with (boto3 and libpq read them)."""
        return {"AWS_ACCESS_KEY_ID": self.access_key_id, "AWS_SECRET_ACCESS_KEY": self.secret_access_key,
                "AWS_ENDPOINT_URL_S3": self.s3_endpoint, "AWS_REGION": self.region,
                "PGHOST": "127.0.0.1", "PGPORT": str(self.pg_port), "PGUSER": self.principal,
                "PGPASSWORD": self.pg_password, "PGDATABASE": self.pg_database, STORE_NAME_VARIABLE: self.store}

    def dsn(self) -> str:
        """The login's libpq conninfo (password included) over 127.0.0.1, as a tool connects."""
        from psycopg.conninfo import make_conninfo

        return make_conninfo(host="127.0.0.1", port=self.pg_port, dbname=self.pg_database,
                             user=self.principal, password=self.pg_password)


def principal(run: str, unit: str) -> str:
    """The one name of ``unit``'s S3 user and Postgres login in ``run``: ``ar_<run>_<unit>_<hash>``,
    lower-case letters, digits and ``_`` (both name rules), at most 62 characters; the hash of the
    unspelled pair keeps two pairs that spell alike apart."""
    digest = hashlib.sha256(f"{run}\0{unit}".encode()).hexdigest()[:8]
    stem = re.sub(r"[^a-z0-9]+", "_", f"{run}_{unit}".lower()).strip("_")[:50]
    return f"ar_{stem}_{digest}"


def policy(store, role_class: str) -> dict:
    """The inline identity policy of ``role_class`` over ``store``'s buckets."""
    if role_class not in ROLE_CLASSES:
        raise ValueError(f"role class {role_class!r} is not one of {', '.join(ROLE_CLASSES)}")
    buckets = [f"arn:aws:s3:::{b}" for b in store.buckets]
    keys = [f"{b}/*" for b in buckets]
    statements = [{"Sid": "List", "Effect": "Allow", "Action": list(_LIST), "Resource": buckets},
                  {"Sid": "ReadPinned", "Effect": "Allow", "Action": list(_READ), "Resource": keys}]
    if role_class == "producer":
        statements.append({"Sid": "Write", "Effect": "Allow", "Action": list(_WRITE), "Resource": keys})
    statements.append({"Sid": "Never", "Effect": "Deny", "Action": list(_DENIED), "Resource": buckets + keys})
    return {"Version": "2012-10-17", "Statement": statements}


def _adapter(store) -> None:
    """Refuse a store whose key adapter is not built."""
    kind = store.adapters["keys"]
    if kind == "aws_sts":
        raise NotImplementedError(
            f"store {store.name}: per-agent keys for a remote store come from the aws_sts key adapter, "
            "which is not built yet (Phase 3 step 0b builds versitygw_iam for local stores only)")
    if kind != "versitygw_iam":
        raise NotImplementedError(f"store {store.name}: no key adapter named {kind!r}")


def _code(exc: Exception) -> str:
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "")


def _drop_user(iam, name: str) -> list[str]:
    """Delete ``name``'s access keys, inline policy and user (each absent part passes); the key ids
    deleted."""
    from botocore.exceptions import ClientError

    try:
        keys = [k["AccessKeyId"] for k in iam.list_access_keys(UserName=name)["AccessKeyMetadata"]]
    except ClientError as exc:
        if _code(exc) == "NoSuchEntity":
            return []
        raise
    for key in keys:
        try:
            iam.delete_access_key(UserName=name, AccessKeyId=key)
        except ClientError as exc:  # another revoke of the same principal got there first
            if _code(exc) != "NoSuchEntity":
                raise
    for call in (lambda: iam.delete_user_policy(UserName=name, PolicyName=POLICY_NAME),
                 lambda: iam.delete_user(UserName=name)):
        try:
            call()
        except ClientError as exc:
            if _code(exc) != "NoSuchEntity":
                raise
    return keys


def _make_user(iam, name: str, document: dict) -> dict:
    """A fresh user ``name`` with the inline policy and one access key (``{AccessKeyId,
    SecretAccessKey}``); an earlier user of that name is dropped first (a resumed run)."""
    _drop_user(iam, name)
    iam.create_user(UserName=name)
    iam.put_user_policy(UserName=name, PolicyName=POLICY_NAME, PolicyDocument=json.dumps(document))
    return iam.create_access_key(UserName=name)["AccessKey"]


async def _forget_keys(conn, name: str) -> None:
    """Mark every live S3 key the catalog records for ``name`` revoked: also the ones an earlier
    revoke deleted from IAM but could not mark (its catalog did not answer)."""
    from auto_research.store import catalog

    cur = await conn.execute("SELECT key_id FROM catalog.credential WHERE kind = 's3' AND agent = %s "
                             "AND revoked_at IS NULL", (name,))
    for (key_id,) in await cur.fetchall():
        try:
            await catalog.revoke_credential(conn, key_id=key_id)
        except catalog.CatalogError:  # another revoke of the same principal marked it first
            pass


class _principal_lock:
    """The principal's Postgres advisory lock on ``conn`` (a session lock, held across autocommit
    statements): two issues or revokes of one principal at once (two launches sweeping one dead run)
    take turns instead of colliding in ``ALTER ROLE`` ("tuple concurrently updated")."""

    def __init__(self, conn, name: str) -> None:
        self.conn, self.name = conn, name

    async def __aenter__(self):
        await self.conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (self.name,))

    async def __aexit__(self, *exc) -> None:
        await self.conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (self.name,))


async def _role_exists(conn, name: str) -> bool:
    cur = await conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
    return await cur.fetchone() is not None


async def issue(store, unit: str, role_class: str, run: str) -> Credentials:
    """Issue ``unit`` its keys to ``store`` for ``run``: an S3 user with the ``role_class`` policy and
    an access key (recorded in the catalog), and a Postgres login in ``role_class`` with a random
    password, valid for :data:`ROLE_VALID_FOR`. Keys an earlier issue of the same (run, unit) left
    are revoked first, and the login's open sessions ended. A failure part-way revokes what was made
    and raises (a failed revoke is noted on the error)."""
    import psycopg

    from auto_research.store import catalog

    _adapter(store)
    document = policy(store, role_class)
    name = principal(run, unit)
    password = secrets.token_urlsafe(32)
    until = datetime.now(timezone.utc) + ROLE_VALID_FOR
    iam = store.admin_client("iam")
    try:
        async with await psycopg.AsyncConnection.connect(store.admin_dsn(), autocommit=True) as conn, \
                _principal_lock(conn, name):
            await asyncio.to_thread(_drop_user, iam, name)
            await _forget_keys(conn, name)
            if await _role_exists(conn, name):  # an earlier issue's sessions end with it
                await catalog.end_role(conn, name=name)
            key = await asyncio.to_thread(_make_user, iam, name, document)
            await catalog.record_credential(conn, key_id=key["AccessKeyId"], agent=name, run=run)
            await catalog.issue_role(conn, name=name, role_class=role_class, run=run, password=password,
                                     valid_until=until)
    except BaseException as exc:
        # After the lock is let go: the rollback takes it on a connection of its own.
        try:
            await revoke(store, unit, run)
        except Exception as cleanup:  # noqa: BLE001 - the issue's own error is the one raised
            exc.add_note(f"revoking the partial issue of {name} failed too: {cleanup}")
        raise
    return Credentials(store=store.name, unit=unit, run=run, role_class=role_class, principal=name,
                       access_key_id=key["AccessKeyId"], secret_access_key=key["SecretAccessKey"],
                       pg_password=password, s3_endpoint=store.s3_endpoint, region=store.region,
                       pg_port=getattr(getattr(store, "ports", None), "catalog", 0),
                       pg_database=store.catalog.database)


async def revoke(store, unit: str, run: str) -> None:
    """Revoke ``unit``'s keys to ``store`` for ``run`` (:func:`revoke_principal`)."""
    await revoke_principal(store, principal(run, unit))


async def revoke_principal(store, name: str) -> None:
    """Revoke the keys of principal ``name`` in ``store``: its access keys, policy and S3 user deleted
    (the catalog marks every live key of ``name`` revoked), its Postgres login ended (NOLOGIN, then
    its open sessions terminated, ``catalog.end_role``). What was never made passes; every failure is
    attempted past and raised together."""
    import psycopg

    from auto_research.store import catalog

    _adapter(store)
    problems: list[str] = []
    try:
        await asyncio.to_thread(_drop_user, store.admin_client("iam"), name)
    except Exception as exc:  # noqa: BLE001 - the login is still ended below
        problems.append(f"S3 user {name}: {exc}")
    async with await psycopg.AsyncConnection.connect(store.admin_dsn(), autocommit=True) as conn, \
            _principal_lock(conn, name):
        if not problems:
            await _forget_keys(conn, name)
        if await _role_exists(conn, name):
            try:
                await catalog.end_role(conn, name=name)
            except Exception as exc:  # noqa: BLE001 - reported with the S3 part
                problems.append(f"Postgres login {name}: {exc}")
    if problems:
        raise RuntimeError(f"store {store.name}: revoking {name}'s keys failed: " + "; ".join(problems))


async def live_runs(store) -> dict[str, list[str]]:
    """``{run: [principal, ...]}`` of every live key the store's catalog records."""
    import psycopg

    async with await psycopg.AsyncConnection.connect(store.admin_dsn(), autocommit=True) as conn:
        cur = await conn.execute("SELECT run, agent FROM catalog.credential WHERE revoked_at IS NULL "
                                 "GROUP BY run, agent ORDER BY run, min(issued_at)")
        out: dict[str, list[str]] = {}
        for run, agent in await cur.fetchall():
            out.setdefault(run, []).append(agent)
        return out


async def revoke_run(store, run: str) -> list[str]:
    """Revoke every live key ``run`` holds in ``store`` (its units' and its reconciler's); the
    principals revoked. Every principal is attempted; failures are raised together."""
    names = (await live_runs(store)).get(run, [])
    problems = []
    for name in names:
        try:
            await revoke_principal(store, name)
        except Exception as exc:  # noqa: BLE001 - every principal is attempted
            problems.append(str(exc))
    if problems:
        raise RuntimeError("; ".join(problems))
    return names


class RunKeys:
    """The keys one run issued, by (store, unit): issued as units are attached, revoked together when
    the run ends (``RunControl.credentials``, leaf to root) or one unit's when it dies."""

    def __init__(self, run: str) -> None:
        self.run = run
        self._held: dict[tuple[str, str], tuple[object, Credentials]] = {}

    async def issue(self, store, unit: str, role_class: str | None = None) -> Credentials:
        """``unit``'s keys to ``store``, of ``role_class`` (default: the store's ``role_of(unit)``).
        Held from before the issue starts, so one that fails or is cancelled part-way is revoked again
        with the rest."""
        self._held[(store.name, unit)] = (store, None)
        credentials = await issue(store, unit, role_class or store.role_of(unit), self.run)
        self._held[(store.name, unit)] = (store, credentials)
        return credentials

    def held(self) -> list[Credentials]:
        """The keys issued and not yet revoked, in issue order."""
        return [credentials for _, credentials in self._held.values() if credentials is not None]

    def pending(self) -> bool:
        """Whether anything is left to revoke (an issue that failed part-way included)."""
        return bool(self._held)

    async def revoke(self, unit: str) -> None:
        """Revoke every key ``unit`` holds (a dead agent)."""
        await self._revoke([k for k in self._held if k[1] == unit])

    async def revoke_all(self) -> None:
        """Revoke every key the run holds, the last issued first; idempotent."""
        await self._revoke(list(reversed(self._held)))

    async def _revoke(self, entries: list[tuple[str, str]]) -> None:
        problems = []
        for entry in entries:
            store, _ = self._held[entry]
            try:
                await revoke(store, entry[1], self.run)
            except Exception as exc:  # noqa: BLE001 - every key is attempted, then raised together
                problems.append(str(exc))
                continue
            del self._held[entry]
        if problems:
            raise RuntimeError("; ".join(problems))
