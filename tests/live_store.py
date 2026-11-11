"""The data store as a live cell meets it, through production's own functions (step 0b).

A cell that drives a role outside ``run_research`` takes the run's store launch step through the
seam ``run_research`` takes it through (``graph.runtime.RunLaunch``): the process records its run,
binds the toolkit contract and the configured ``stores:`` block, starts and reconciles each local
store, hands the run's keys to its stop channel (so a stop's cleanup revokes them, as in a run), and
revokes every key it issued when the cell ends, the store left running for the next run (servers
outlive runs).
Inside it the cell issues each worker's keys with ``graph.runtime.issue_unit_keys`` and hands them to
``attach_worker_runtime``, and wires a lead with ``graph.runtime.wire_lead``, as ``run_research``
does. A store that is not ready skips the cell with the store's own refusal (:func:`require_store`).

The configured store is the real one (``/xray02/fzhou_db``): the launch step refuses until the
migration (plan item S11) has made it. A cell that needs a real input looks its record up with
:func:`product_record`, which skips the cell, with the reason, while the store holds none.

A coding-pipeline cell authors on a toolkit clone at :data:`STORE_BASE_REF`, the toolkit commit
that carries the data store (``storelib``, records, ``writes``): :func:`store_base_ref` skips the
cell until that commit is pinned here.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

#: The toolkit commit the coding-pipeline cells clone: the first one carrying the data store
#: (Phase 3 step 0b, item S8). None until the human commits the toolkit's store work.
STORE_BASE_REF: str | None = None


def store_base_ref() -> str:
    """:data:`STORE_BASE_REF`, or a skip naming what the cell waits on."""
    if STORE_BASE_REF is None:
        pytest.skip("waits on the toolkit commit that carries the data store (item S8): pin it as "
                    "tests/live_store.STORE_BASE_REF")
    return STORE_BASE_REF


def _config_ready() -> None:
    """The configured stores, read as the preflight's ``store_config`` item reads them (main's
    rulings): a configuration defect (``store.health.config_defects``: a block that does not parse,
    no default store, a folder a jail mounts, a manifest naming an undeclared store) FAILS the cell,
    checked first, whether or not the store is here; a store absent from this host (its admin
    variables unset, ``AdminEnv.values``; its root missing, or its state_dir and the folder that
    would hold it) skips the cell with that reason; then ``store_config`` itself, whose remaining
    refusals fail."""
    from auto_research.store import health
    from auto_research.store.config import LocalStore, stores

    defects = health.config_defects()
    if defects:
        pytest.fail(f"the store configuration is defective (store_config): {'; '.join(defects)}")
    absent = []
    for store in stores():
        try:
            store.admin_env.values()
        except RuntimeError as exc:
            absent.append(f"store {store.name}: {exc}")
        if isinstance(store, LocalStore):
            if not store.root.is_dir():
                absent.append(f"store {store.name}: root {store.root} does not exist")
            if not store.state_dir.is_dir() and not store.state_dir.parent.is_dir():  # launch makes it
                absent.append(f"store {store.name}: state_dir {store.state_dir} cannot be made here")
    if absent:
        pytest.skip(f"the data store is absent from this host: {'; '.join(absent)}")
    health.check_store_config()


def require_store() -> None:
    """Skip the cell, with the store's own refusal, when the configured store is absent or its servers
    cannot serve (main's rulings: absence is a precondition; a configuration defect fails). For the
    cells that go through ``run_research``, before they call it (no model call yet): the defect and
    absence checks (:func:`_config_ready`), then the run's launch step itself on a run of its own
    (``RunLaunch``: the servers start, idempotently, and are checked ready, the store reconciled),
    which raises ``StoreNotReady`` with the store's words when its servers do not start or answer.
    Every other failure stays a failure."""
    import asyncio
    import uuid

    from auto_research.graph.runtime import RunLaunch, StoreNotReady

    _config_ready()

    async def probe() -> None:
        async with RunLaunch(f"store-check-{uuid.uuid4().hex[:8]}", None):
            pass

    try:
        asyncio.run(probe())
    except StoreNotReady as exc:
        pytest.skip(f"the data store is not ready (the run's launch step): {exc}")


@contextlib.asynccontextmanager
async def run_stores(run_id: str, monitor: Any, control: Any | None = None) -> AsyncIterator[Any]:
    """The run's store launch step through production's own seam (``graph.runtime.RunLaunch``, the
    one ``run_research`` takes); yields the run's ``store.keys.RunKeys``, every key of which is
    revoked, and the contract and stores released, when the block ends, however it ends.
    ``control``: the cell's ``RunControl``, given the keys as ``run_research`` gives its own
    (``RunLaunch.hold``). An absent store, or one whose servers cannot serve (``StoreNotReady`` from the
    launch step), skips the cell with its own refusal before the body runs (:func:`_config_ready`
    first); a configuration defect and every other failure, of the launch or of the body, fail it."""
    from auto_research.graph.runtime import RunLaunch, StoreNotReady

    _config_ready()
    launch = RunLaunch(run_id, monitor)
    try:
        await launch.__aenter__()
    except StoreNotReady as exc:
        pytest.skip(f"the data store is not ready (the run's launch step): {exc}")
    try:
        if control is not None:
            launch.hold(control)
        yield launch.keys
    finally:
        await launch.__aexit__(None, None, None)


async def worker_keys(keys: Any, units: Sequence[str], monitor: Any) -> dict[str, Any]:
    """``attach_worker_runtime``'s store arguments for ``units``, as ``run_research`` passes them:
    each worker's own keys (``credentials``) and the runtime's reconciler login (``reconciler``)."""
    from auto_research.graph.runtime import issue_unit_keys, run_reconciler

    credentials = await issue_unit_keys(keys, list(units), monitor)
    return {"credentials": credentials,
            "reconciler": next((run_reconciler(keys, c.store) for c in credentials.values()), None)}


async def product_record(bucket: str, subject: str, kind: str | None = None) -> dict[str, str]:
    """The record (``{store, bucket, key, version_id}``, its ``recipe.json``'s) of the verified product,
    with no live flag, of ``subject`` in ``bucket`` of the run's default store (of ``kind`` when
    given): the current one, else the newest. Read with production's catalog query (``store.query``) on the store's admin
    login, inside :func:`run_stores`. Skips the cell while the store holds none."""
    from auto_research.store import query
    from auto_research.store.config import DEFAULT_STORE, store
    from auto_research.store.reconcile import RECIPE_FILE

    db = store(DEFAULT_STORE)
    asked = {"view": "products", "bucket": bucket, "subject": subject, "state": "verified",
             **({"kind": kind} if kind else {})}
    rows = [row for row in (await query.run(db.admin_dsn(), query.check(asked), store=db.name))["rows"]
            if not row["flags"]]
    if not rows:
        pytest.skip(f"store {db.name} holds no verified, unflagged product of {subject} in {bucket}"
                    + (f" of kind {kind}" if kind else ""))
    product = next((row for row in rows if row["current"]), rows[0])
    objects = (await query.run(db.admin_dsn(), query.check(
        {"view": "objects", "recipe_hash": product["recipe_hash"], "limit": query.MAX_LIMIT}),
        store=db.name))["rows"]
    recipe = next((o for o in objects if o["key"].rsplit("/", 1)[-1] == RECIPE_FILE), None)
    if recipe is None:
        pytest.fail(f"product {product['recipe_hash'][:16]} of {subject} lists no {RECIPE_FILE} object")
    return {name: recipe[name] for name in ("store", "bucket", "key", "version_id")}


#: A record-shaped brief input for a cell whose coding session is stubbed or stopped before the gate
#: (it never reaches a store): the store's form, with values that name no object.
STAND_IN_RECORD = {"store": "db", "bucket": "ixpe", "key": "raw/01002901/stand-in/recipe.json",
                   "version_id": "stand-in"}
