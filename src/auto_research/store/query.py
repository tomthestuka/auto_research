"""A unit's read-only query of a store's catalog (step 0b, item S9).

The runtime runs it for a unit that holds a reader-class login to the store (``store.keys``): the
products the store holds by bucket, kind, subject and state, the "current" pointer per (bucket, kind,
subject), and
the object versions one product holds, each as the record a tool argument or a brief names
(``{store, bucket, key, version_id}``). It knows no layout: kind, subject, bucket and key are opaque
strings the toolkit chooses. The answer is navigation, never evidence (CLAUDE.md §7).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Mapping

import psycopg
from psycopg.rows import dict_row

#: What a query can ask for.
VIEWS = ("products", "current", "objects")
#: A product's states (``catalog.product.state``).
STATES = ("claimed", "committed", "verified", "failed", "nondeterministic")
#: Rows answered by default, and at most.
DEFAULT_LIMIT, MAX_LIMIT = 50, 200
#: The arguments a query takes.
ARGUMENTS = ("store", "view", "bucket", "kind", "subject", "state", "recipe_hash", "limit")
#: A recipe hash or a prefix of it: at least the 8 hex digits a product's folder carries.
_HASH_PREFIX = re.compile(r"^[0-9a-f]{8,64}$")
#: Each session is read-only and bounded, whatever the login could do.
_SESSION_OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=10000"
_CONNECT_TIMEOUT_S = 10


class QueryError(ValueError):
    """A query the catalog cannot answer as asked; its message tells the model what to write."""


def check(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """The query ``arguments`` ask for, normalised: ``view`` (default ``products``), the filters
    given, ``limit`` (default :data:`DEFAULT_LIMIT`, at most :data:`MAX_LIMIT`). Raises
    :class:`QueryError` naming what to write instead."""
    unknown = sorted(set(arguments) - set(ARGUMENTS))
    if unknown:
        raise QueryError(f"catalog_query takes {', '.join(ARGUMENTS)}; it was given {', '.join(unknown)}")
    view = arguments.get("view") or "products"
    if view not in VIEWS:
        raise QueryError(f"view is one of {', '.join(VIEWS)}; it reads {view!r}")
    out: dict[str, Any] = {"view": view}
    for name in ("store", "bucket", "kind", "subject"):  # opaque strings, matched exactly as written
        value = arguments.get(name)
        if value is not None:
            if not isinstance(value, str) or not value:
                raise QueryError(f"{name} is a non-empty string; it reads {value!r}")
            out[name] = value
    state = arguments.get("state")
    if state is not None:
        if state not in STATES:
            raise QueryError(f"state is one of {', '.join(STATES)}; it reads {state!r}")
        out["state"] = state
    recipe = arguments.get("recipe_hash")
    if recipe is not None:
        if not isinstance(recipe, str) or not _HASH_PREFIX.match(recipe.strip().lower()):
            raise QueryError("recipe_hash is a product's recipe hash, or its first 8 or more hex digits "
                             f"(the short hash in its folder name); it reads {recipe!r}")
        out["recipe_hash"] = recipe.strip().lower()
    limit = arguments.get("limit")
    limit = DEFAULT_LIMIT if limit is None else limit
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise QueryError(f"limit is a whole number from 1 to {MAX_LIMIT}; it reads {limit!r}")
    out["limit"] = limit
    if view == "objects" and "recipe_hash" not in out:
        raise QueryError("view objects names the product: give its recipe_hash (from view products or current)")
    if view == "objects" and {"bucket", "kind", "subject", "state"} & set(out):
        raise QueryError("view objects names one product by its recipe_hash only")
    if view == "current" and ("state" in out or "recipe_hash" in out):
        raise QueryError("view current filters by bucket, kind and subject only")
    return out


async def query(conn: psycopg.AsyncConnection, arguments: Mapping[str, Any], *, store: str) -> dict[str, Any]:
    """Answer one query (``arguments`` as :func:`check` returns them) on ``conn``, a connection
    to ``store``'s catalog. The answer names the view, the rows shown, how many matched, and for
    ``objects`` the product they belong to; times are ISO 8601 strings."""
    view = arguments["view"]
    if view == "products":
        return await _products(conn, arguments, store)
    if view == "current":
        return await _current(conn, arguments, store)
    return await _objects(conn, arguments, store)


async def run(dsn: str, arguments: Mapping[str, Any], *, store: str) -> dict[str, Any]:
    """:func:`query` on a session of its own over ``dsn`` (a reader-class login, ``Credentials.dsn``),
    read-only and with a statement timeout."""
    async with await psycopg.AsyncConnection.connect(
        dsn, autocommit=True, options=_SESSION_OPTIONS, connect_timeout=_CONNECT_TIMEOUT_S,
    ) as conn:
        return await query(conn, arguments, store=store)


#: What the products and objects views say of a product: its recipe (inputs apart), state, flags
#: (``catalog.live_flag``: stamped and not cleared) and whether it is current for its (bucket, kind,
#: subject).
_PRODUCT_COLUMNS = """p.recipe_hash, p.bucket, p.kind, p.subject, p.state, p.generation,
               p.recipe->>'tool' AS tool, p.recipe->>'algorithm_version' AS algorithm_version,
               p.recipe->'settings' AS settings, p.claimed_at, p.committed_at, p.verified_at, p.reason,
               c.recipe_hash IS NOT NULL AS current,
               ARRAY(SELECT DISTINCT f.flag FROM catalog.live_flag f
                     WHERE f.recipe_hash = p.recipe_hash ORDER BY f.flag) AS flags"""
_PRODUCT_FROM = """catalog.product p
        LEFT JOIN catalog.current c
          ON c.bucket = p.bucket AND c.kind = p.kind AND c.subject = p.subject AND c.recipe_hash = p.recipe_hash"""


def _filters(arguments: Mapping[str, Any], table: str, names: tuple[str, ...]) -> tuple[str, dict[str, Any]]:
    clauses = [f"{table}.{name} = %({name})s" for name in names if name in arguments]
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", {n: arguments[n] for n in names if n in arguments}


async def _products(conn: psycopg.AsyncConnection, arguments: Mapping[str, Any], store: str) -> dict[str, Any]:
    where, params = _filters(arguments, "p", ("bucket", "kind", "subject", "state"))
    if "recipe_hash" in arguments:
        where += (" AND " if where else " WHERE ") + "p.recipe_hash LIKE %(prefix)s"
        params["prefix"] = arguments["recipe_hash"] + "%"
    rows = await _rows(conn, f"""
        SELECT {_PRODUCT_COLUMNS},
               jsonb_array_length(p.recipe->'inputs') AS inputs,
               (SELECT count(*) FROM catalog.object o WHERE o.recipe_hash = p.recipe_hash) AS objects,
               count(*) OVER () AS matched
        FROM {_PRODUCT_FROM}
        {where}
        ORDER BY p.claimed_at DESC, p.recipe_hash
        LIMIT %(limit)s""", {**params, "limit": arguments["limit"]})
    return _answer(store, "products", rows)


async def _current(conn: psycopg.AsyncConnection, arguments: Mapping[str, Any], store: str) -> dict[str, Any]:
    where, params = _filters(arguments, "c", ("bucket", "kind", "subject"))
    rows = await _rows(conn, f"""
        SELECT c.bucket, c.kind, c.subject, c.recipe_hash, c.seq, c.moved_at, c.reason, count(*) OVER () AS matched
        FROM catalog.current c{where}
        ORDER BY c.bucket, c.kind, c.subject
        LIMIT %(limit)s""", {**params, "limit": arguments["limit"]})
    return _answer(store, "current", rows)


async def _objects(conn: psycopg.AsyncConnection, arguments: Mapping[str, Any], store: str) -> dict[str, Any]:
    found = await _rows(conn, f"""
        SELECT {_PRODUCT_COLUMNS}, p.recipe->'inputs' AS inputs
        FROM {_PRODUCT_FROM}
        WHERE p.recipe_hash LIKE %(prefix)s ORDER BY p.recipe_hash LIMIT 6""",
                        {"prefix": arguments["recipe_hash"] + "%"})
    if not found:
        raise QueryError(f"no product's recipe hash starts with {arguments['recipe_hash']}; "
                         "list the products to find it")
    if len(found) > 1:
        raise QueryError(f"{arguments['recipe_hash']} starts more than one product's recipe hash "
                         f"({', '.join(r['recipe_hash'][:16] for r in found)}…); give more of it")
    product = _plain(found[0])
    rows = await _rows(conn, """
        SELECT store, bucket, key, version_id, sha256, size, count(*) OVER () AS matched
        FROM catalog.object WHERE recipe_hash = %(hash)s
        ORDER BY id LIMIT %(limit)s""", {"hash": product["recipe_hash"], "limit": arguments["limit"]})
    return {**_answer(store, "objects", rows), "product": product}


def _answer(store: str, view: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    matched = int(rows[0]["matched"]) if rows else 0
    shown = [_plain({k: v for k, v in row.items() if k != "matched"}) for row in rows]
    return {"store": store, "view": view, "matched": matched, "shown": len(shown), "rows": shown}


def _plain(row: Mapping[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}


async def _rows(conn: psycopg.AsyncConnection, statement: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(statement, params)
        return await cur.fetchall()


__all__ = ["ARGUMENTS", "DEFAULT_LIMIT", "MAX_LIMIT", "STATES", "VIEWS", "QueryError", "check", "query", "run"]
