"""The store catalog: Postgres schema, roles and the runtime's own calls.

Bytes and version ids live in S3; every mutable fact about them lives here (``catalog.sql``):
products named by recipe hash with their claim state, the objects each product wrote, the
"current" pointer per (bucket, kind, subject) with its append-only history, the credentials issued per
agent and run, and an append-only event log. Agents never write a table: a producer calls
``catalog.claim`` / ``catalog.commit`` / ``catalog.fail`` (SQL, from the toolkit's helper), the
runtime's reconciler calls ``verify`` / ``adopt_orphan`` / ``move_current`` (wrapped below); each is a
SECURITY DEFINER function owned by the human's ``owner`` role that names its caller by
``session_user``.

This module holds what the runtime does with the catalog: install the schema (:func:`apply_schema`),
check it at launch (:func:`check_schema`), issue and end each agent's login role
(:func:`issue_role`, :func:`end_role`), record S3 keys (:func:`record_credential`,
:func:`revoke_credential`), and the reconciler's calls and reads. Every function taking a
connection expects one in autocommit mode.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

#: The schema version this code reads and writes; :func:`check_schema` refuses any other.
SCHEMA_VERSION = 1
SCHEMA_SQL = Path(__file__).with_name("catalog.sql")
#: The classes a login role may join. ``owner`` is the human's and is never issued.
ROLE_CLASSES = ("reader", "producer", "reconciler")
CATALOG_ROLES = ("owner", *ROLE_CLASSES)
_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


#: The human's own login (coordinator ruling 11-10): a LOGIN role in ``owner``, never a superuser, made
#: without a password by the store's first start; the human sets its password (``set_human_password``,
#: ``python -m auto_research.store human-password``), which no ``.env`` and no runtime process holds.
#: ``clear_flag`` and human pointer overrides run as it.
HUMAN_ROLE = "ar_human"


class CatalogError(RuntimeError):
    """The catalog is missing, at another schema version, or a role request was refused."""


async def apply_schema(dsn: str) -> None:
    """Install the catalog in ``dsn``'s database, or confirm it is already at :data:`SCHEMA_VERSION`.

    ``dsn`` connects as a superuser (the cluster's bootstrap role): it creates the four roles if
    missing and the schema owned by ``owner``, in one transaction, then runs :func:`check_schema`'s
    role checks, so roles found in the cluster that can log in, or logins holding a class without a
    credential, refuse the install. One catalog per cluster: the roles are cluster-wide. A catalog at
    another version is refused; no older version exists to upgrade from.
    """
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        async with conn.transaction():
            found = await _installed_version(conn)
            if found is not None and found != SCHEMA_VERSION:
                raise CatalogError(f"catalog schema is version {found}; this code installs version "
                                   f"{SCHEMA_VERSION} and holds no upgrade from {found}")
            if found is None:
                await conn.execute(SCHEMA_SQL.read_text())
                await conn.execute("INSERT INTO catalog.schema_version (version) VALUES (%s)", (SCHEMA_VERSION,))
            cur = await conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (HUMAN_ROLE,))
            if await cur.fetchone() is None:  # no password: it logs in once the human sets one
                await conn.execute(sql.SQL("CREATE ROLE {} LOGIN INHERIT NOSUPERUSER NOCREATEROLE NOCREATEDB "
                                           "NOREPLICATION NOBYPASSRLS IN ROLE owner").format(sql.Identifier(HUMAN_ROLE)))
            if found is not None:
                return
            if problems := await _role_problems(conn):
                raise CatalogError("catalog roles: " + "; ".join(problems))


async def check_schema(dsn: str) -> None:
    """Preflight: the catalog at ``dsn`` is at :data:`SCHEMA_VERSION`, its four roles exist and cannot
    log in, and every login holding reader, producer or reconciler has a live credential from this
    catalog. Raises :class:`CatalogError` naming what is wrong.
    """
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        found = await _installed_version(conn)
        if found is None:
            raise CatalogError("no catalog schema in this database; install it with "
                               "auto_research.store.catalog.apply_schema")
        if found != SCHEMA_VERSION:
            raise CatalogError(f"catalog schema is version {found}; this code reads version {SCHEMA_VERSION}")
        if problems := await _role_problems(conn):
            raise CatalogError("catalog roles: " + "; ".join(problems))


async def _installed_version(conn: psycopg.AsyncConnection) -> int | None:
    cur = await conn.execute("SELECT to_regclass('catalog.schema_version') IS NOT NULL")
    if not (await cur.fetchone())[0]:
        return None
    cur = await conn.execute("SELECT max(version) FROM catalog.schema_version")
    return (await cur.fetchone())[0]


async def _role_problems(conn: psycopg.AsyncConnection) -> list[str]:
    cur = await conn.execute("SELECT rolname, rolcanlogin OR rolsuper FROM pg_roles WHERE rolname = ANY(%s)",
                             (list(CATALOG_ROLES),))
    roles = dict(await cur.fetchall())
    problems = [f"{r} is missing" for r in CATALOG_ROLES if r not in roles]
    problems += [f"{r} can log in or is a superuser; it is a NOLOGIN group role" for r, bad in roles.items() if bad]
    cur = await conn.execute(
        "SELECT m.rolname, g.rolname FROM pg_auth_members a JOIN pg_roles m ON m.oid = a.member "
        "JOIN pg_roles g ON g.oid = a.roleid WHERE g.rolname = ANY(%s) AND m.rolcanlogin AND NOT EXISTS "
        "(SELECT FROM catalog.credential c WHERE c.kind = 'postgres' AND c.key_id = m.rolname "
        "AND c.revoked_at IS NULL) ORDER BY 1, 2", (list(ROLE_CLASSES),))
    problems += [f"login {m} holds {g} without a live credential from this catalog" for m, g in await cur.fetchall()]
    cur = await conn.execute(
        "SELECT r.rolcanlogin AND NOT r.rolsuper AND r.rolinherit AND NOT r.rolcreaterole AND NOT r.rolcreatedb "
        "AND NOT r.rolreplication AND NOT r.rolbypassrls, "
        "EXISTS (SELECT FROM pg_auth_members a JOIN pg_roles g ON g.oid = a.roleid "
        "        WHERE a.member = r.oid AND g.rolname = 'owner'), "
        "EXISTS (SELECT FROM pg_auth_members a JOIN pg_roles g ON g.oid = a.roleid "
        "        WHERE a.member = r.oid AND g.rolname = ANY(%s)) "
        "FROM pg_roles r WHERE r.rolname = %s", (list(ROLE_CLASSES), HUMAN_ROLE))
    human = await cur.fetchone()
    if human is None:
        problems.append(f"the human's login {HUMAN_ROLE} is missing")
    elif not (human[0] and human[1]) or human[2]:
        problems.append(f"the human's login {HUMAN_ROLE} must be a LOGIN, INHERIT member of owner and of no "
                        "class, with no superuser, createrole, createdb, replication or bypassrls attribute")
    cur = await conn.execute(
        "SELECT r.rolname FROM pg_auth_members a JOIN pg_roles r ON r.oid = a.member JOIN pg_roles g ON g.oid = a.roleid "
        "WHERE g.rolname = 'owner' AND r.rolcanlogin AND r.rolname <> %s ORDER BY 1", (HUMAN_ROLE,))
    problems += [f"login {m} is in owner; the human acts as {HUMAN_ROLE} only" for (m,) in await cur.fetchall()]
    return problems


async def set_human_password(conn: psycopg.AsyncConnection, password: str) -> None:
    """Set the human's login password (a SCRAM verifier hashed here, never the password, reaches the
    server). ``conn``: a superuser connection, the human's own act through the CLI."""
    if not password:
        raise CatalogError("give a password")
    verifier = conn.pgconn.encrypt_password(password.encode(), HUMAN_ROLE.encode(), b"scram-sha-256").decode()
    await conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(HUMAN_ROLE), sql.Literal(verifier)))


# ---------------------------------------------------------------- credentials (the key adapter)


async def issue_role(conn: psycopg.AsyncConnection, *, name: str, role_class: str, run: str,
                     password: str, valid_until: datetime) -> None:
    """Give agent ``name`` a login role in ``role_class`` for ``run``, valid until ``valid_until``.

    ``conn`` is a superuser connection to the catalog's database. Agents hold ``reader`` or
    ``producer``; ``reconciler`` is the runtime's own login. A role this catalog issued before (an
    earlier run) is re-enabled with the new password, class and expiry, and its settings reset; a
    role it never issued is refused, so no human or system role is taken over. Records a
    ``postgres`` credential (revoking the role's earlier one, if still live).
    """
    if role_class not in ROLE_CLASSES:
        raise CatalogError(f"role class {role_class!r} is not one of {', '.join(ROLE_CLASSES)}")
    if not _ROLE_NAME.match(name) or name in CATALOG_ROLES or name.startswith("pg_"):
        raise CatalogError(f"agent role name {name!r} is lower-case letters, digits and _, "
                           f"and none of {', '.join(CATALOG_ROLES)}")
    if valid_until.tzinfo is None:
        raise CatalogError("valid_until carries a time zone")
    role = sql.Identifier(name)
    # hashed by libpq here, so the password never reaches the server (or its statement log) in clear
    secret = conn.pgconn.encrypt_password(password.encode(), name.encode(), b"scram-sha-256").decode()
    attrs = sql.SQL("LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT "
                    "PASSWORD {} VALID UNTIL {}").format(sql.Literal(secret), sql.Literal(valid_until.isoformat()))
    async with conn.transaction():
        cur = await conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
        exists = await cur.fetchone() is not None
        if exists and not await _issued(conn, name):
            raise CatalogError(f"role {name!r} exists and was not issued by this catalog; choose another name")
        if exists:
            await conn.execute(sql.SQL("ALTER ROLE {} ").format(role) + attrs)
            await conn.execute(sql.SQL("ALTER ROLE {} RESET ALL").format(role))
            for other in ROLE_CLASSES:  # an earlier run's class; revoking a non-member only warns
                if other != role_class:
                    await conn.execute(sql.SQL("REVOKE {} FROM {}").format(sql.Identifier(other), role))
        else:
            await conn.execute(sql.SQL("CREATE ROLE {} ").format(role) + attrs)
        await conn.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(role_class), role))
        await _revoke(conn, "postgres", name)
        await _record(conn, "postgres", name, name, run)


async def end_role(conn: psycopg.AsyncConnection, *, name: str) -> None:
    """End agent ``name``'s login: NOLOGIN and its credential revoked (committed first, so no new
    session can open), then its open sessions terminated; a session left open raises.

    ``conn`` is a superuser connection in autocommit mode.
    """
    if not conn.autocommit:
        raise CatalogError("end_role needs an autocommit connection, so NOLOGIN commits before the sessions end")
    async with conn.transaction():
        if not await _issued(conn, name):
            raise CatalogError(f"role {name!r} was not issued by this catalog")
        await conn.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(name)))
        await _revoke(conn, "postgres", name)
    await conn.execute("SELECT pg_terminate_backend(pid, 5000) FROM pg_stat_activity WHERE usename = %s", (name,))
    cur = await conn.execute("SELECT count(*) FROM pg_stat_activity WHERE usename = %s", (name,))
    if left := (await cur.fetchone())[0]:
        raise CatalogError(f"role {name!r} is NOLOGIN but {left} of its sessions did not end")


async def record_credential(conn: psycopg.AsyncConnection, *, key_id: str, agent: str, run: str) -> None:
    """Record that ``agent`` holds S3 access key ``key_id`` for ``run`` (one live row per key)."""
    async with conn.transaction():
        await _record(conn, "s3", key_id, agent, run)


async def revoke_credential(conn: psycopg.AsyncConnection, *, key_id: str) -> None:
    """Mark S3 access key ``key_id`` revoked; a key with no live row is refused."""
    async with conn.transaction():
        if not await _revoke(conn, "s3", key_id):
            raise CatalogError(f"no live s3 credential {key_id!r}")


async def _issued(conn: psycopg.AsyncConnection, name: str) -> bool:
    cur = await conn.execute("SELECT 1 FROM catalog.credential WHERE kind = 'postgres' AND key_id = %s", (name,))
    return await cur.fetchone() is not None


async def _record(conn: psycopg.AsyncConnection, kind: str, key_id: str, agent: str, run: str) -> None:
    await conn.execute("INSERT INTO catalog.credential (kind, key_id, agent, run) VALUES (%s, %s, %s, %s)",
                       (kind, key_id, agent, run))
    await conn.execute("INSERT INTO catalog.event (action, detail) VALUES ('credential.issue', %s)",
                       (Jsonb({"kind": kind, "key_id": key_id, "agent": agent, "run": run}),))


async def _revoke(conn: psycopg.AsyncConnection, kind: str, key_id: str) -> bool:
    cur = await conn.execute("UPDATE catalog.credential SET revoked_at = now() "
                             "WHERE kind = %s AND key_id = %s AND revoked_at IS NULL", (kind, key_id))
    if cur.rowcount:
        await conn.execute("INSERT INTO catalog.event (action, detail) VALUES ('credential.revoke', %s)",
                           (Jsonb({"kind": kind, "key_id": key_id}),))
    return bool(cur.rowcount)


# ---------------------------------------------------------------- the reconciler's calls


async def verify(conn: psycopg.AsyncConnection, recipe_hash: str, outcome: str = "verified",
                 reason: str | None = None) -> dict[str, Any]:
    """``catalog.verify``: the verdict on a committed product (verified, failed, nondeterministic)."""
    return await _call(conn, "SELECT catalog.verify(%s, %s, %s)", (recipe_hash, outcome, reason))


async def adopt_orphan(conn: psycopg.AsyncConnection, recipe_hash: str, objects: list[dict[str, Any]], *,
                       toolkit_commit: str, env_fingerprint: str, producer: str, reason: str,
                       generation: int) -> dict[str, Any]:
    """``catalog.adopt_orphan``: commit objects a producer stored but never committed, refused unless
    the claim is still at ``generation`` (the production that stored them)."""
    return await _call(conn, "SELECT catalog.adopt_orphan(%s, %s, %s, %s, %s, %s, %s)",
                       (recipe_hash, Jsonb(objects), toolkit_commit, env_fingerprint, producer, reason, generation))


async def move_current(conn: psycopg.AsyncConnection, bucket: str, kind: str, subject: str, recipe_hash: str,
                       expected_seq: int, reason: str) -> dict[str, Any]:
    """``catalog.move_current``: point ``(bucket, kind, subject)`` at a verified product of that bucket;
    ``{"status": "moved" | "mismatch", "current": <pointer row> | None}``."""
    return await _call(conn, "SELECT catalog.move_current(%s, %s, %s, %s, %s, %s)",
                       (bucket, kind, subject, recipe_hash, expected_seq, reason))


async def stamp_flags(conn: psycopg.AsyncConnection, recipe_hash: str, flags: list[str], receipt: str) -> int:
    """``catalog.stamp_flags``: the flags of call receipt ``receipt`` on a product it committed; how
    many were new for (product, receipt)."""
    return await _call(conn, "SELECT catalog.stamp_flags(%s, %s, %s)", (recipe_hash, list(flags), receipt))


async def product_flags(conn: psycopg.AsyncConnection, recipe_hash: str) -> list[dict[str, Any]]:
    """Every row of product ``recipe_hash``'s flag history (stamps and clears), first to last."""
    return await _rows(conn, "SELECT * FROM catalog.product_flag WHERE recipe_hash = %s ORDER BY id", (recipe_hash,))


async def live_flags(conn: psycopg.AsyncConnection, recipe_hash: str) -> set[str]:
    """The flags that count on product ``recipe_hash``: those whose latest row is a stamp (``catalog.live_flag``)."""
    return {row["flag"] for row in await _rows(conn, "SELECT flag FROM catalog.live_flag WHERE recipe_hash = %s",
                                               (recipe_hash,))}


async def clear_flag(conn: psycopg.AsyncConnection, recipe_hash: str, flag: str, reason: str) -> dict[str, Any]:
    """``catalog.clear_flag``: the human clears a live flag, with the reason (the owner's only);
    ``{"flag", "receipts"}``: the receipts whose stamps the clear covers."""
    return await _call(conn, "SELECT catalog.clear_flag(%s, %s, %s)", (recipe_hash, flag, reason))


async def _call(conn: psycopg.AsyncConnection, query: str, params: tuple) -> dict[str, Any]:
    async with conn.transaction():
        cur = await conn.execute(query, params)
        return (await cur.fetchone())[0]


# ---------------------------------------------------------------- reads


async def product(conn: psycopg.AsyncConnection, recipe_hash: str) -> dict[str, Any] | None:
    """The product row named ``recipe_hash``, or None."""
    rows = await _rows(conn, "SELECT * FROM catalog.product WHERE recipe_hash = %s", (recipe_hash,))
    return rows[0] if rows else None


async def products(conn: psycopg.AsyncConnection, state: str) -> list[dict[str, Any]]:
    """Every product in ``state``, oldest claim first."""
    return await _rows(conn, "SELECT * FROM catalog.product WHERE state = %s ORDER BY claimed_at", (state,))


async def objects(conn: psycopg.AsyncConnection, recipe_hash: str) -> list[dict[str, Any]]:
    """The objects a product wrote, in the order recorded."""
    return await _rows(conn, "SELECT * FROM catalog.object WHERE recipe_hash = %s ORDER BY id", (recipe_hash,))


async def current(conn: psycopg.AsyncConnection, bucket: str, kind: str, subject: str) -> dict[str, Any] | None:
    """The pointer row for ``(bucket, kind, subject)`` (its ``seq`` is the next move's ``expected_seq``;
    its ``recipe_hash`` is None after every product it pointed at was demoted), or None."""
    rows = await _rows(conn, "SELECT * FROM catalog.current WHERE bucket = %s AND kind = %s AND subject = %s",
                       (bucket, kind, subject))
    return rows[0] if rows else None


async def current_history(conn: psycopg.AsyncConnection, bucket: str, kind: str, subject: str) -> list[dict[str, Any]]:
    """Every move of ``(bucket, kind, subject)``'s pointer, first to last."""
    return await _rows(conn, "SELECT * FROM catalog.current_history WHERE bucket = %s AND kind = %s AND subject = %s "
                             "ORDER BY seq", (bucket, kind, subject))


async def _rows(conn: psycopg.AsyncConnection, query: str, params: tuple) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()
