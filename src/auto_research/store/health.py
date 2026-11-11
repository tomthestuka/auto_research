"""The stores' launch preflight items (registered in ``auto_research.preflight``), in run order:

- ``store_config``: the ``stores:`` block validates, the admin secrets are set, and no jail may bind
  a local store's root, state_dir or socket_dir;
- ``store_servers``: every server of a local store ``store_config`` accepts answers (the run's launch
  step started it; this check starts nothing); a remote store's S3 endpoint answers;
- ``store_admin``: the admin key lists the buckets (and, local, the IAM users);
- ``store_buckets``: every declared bucket exists with versioning Enabled;
- ``store_catalog``: the catalog database accepts the admin role and its schema version matches
  (``store.catalog.check_schema``, item S2).

Each raises one error naming every store and what to do.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

from auto_research.store import launch
from auto_research.store.config import DEFAULT_STORE, LocalStore, stores


def _each(check) -> None:
    """Run ``check(store)`` on every declared store, raising once with every problem."""
    problems = []
    for store in stores():
        try:
            check(store)
        except Exception as exc:  # noqa: BLE001 - every store's problem is reported together
            problems.append(f"store {store.name}: {exc}" if not str(exc).startswith(f"store {store.name}")
                            else str(exc))
    if problems:
        raise RuntimeError("; ".join(problems))


def _jail_mounts() -> list[Path]:
    """Every folder the configuration lets a jail bind: ``agent_fs_allow``, ``agent_fs_readonly``,
    ``test_data``, the toolkit roots, the human environments, the externals and the runtime dir (the
    pipeline's environments and every workspace live there). The host ``/tmp`` itself is left out: each
    jail gets a private one in its place (a socket_dir in the temp root is refused by ``launch``)."""
    from auto_research.env import _orch_config, runtime_data_dir

    config = _orch_config()
    entries = [*(config.get("agent_fs_allow") or []), *(config.get("agent_fs_readonly") or []),
               *((config.get("test_data") or {}).values()), *(config.get("human_envs") or {}).values(),
               *(t.get("root") for t in (config.get("toolkits") or []) if isinstance(t, dict)),
               *((e or {}).get("root") for e in (config.get("env_externals") or {}).values()),
               runtime_data_dir()]
    host_tmp = Path("/tmp").resolve()
    out = {Path(str(e)).expanduser().resolve() for e in entries if e}
    return sorted(p for p in out if p != host_tmp)


def _placement_problems(store, mounts: list[Path]) -> list[str]:
    """A local ``store``'s folders a jail may bind (none for a remote store)."""
    problems = []
    if isinstance(store, LocalStore):
        for field in ("root", "state_dir", "socket_dir"):
            path = getattr(store, field).resolve()
            inside = [str(m) for m in mounts if path.is_relative_to(m) or m.is_relative_to(path)]
            if inside:
                problems.append(f"{field} {path} meets the jail mount(s) {', '.join(inside)}; place it outside "
                                "every folder a jail binds (tools reach the store through S3 only)")
    return problems


def _config_problems(store, mounts: list[Path]) -> list[str]:
    """Why ``store`` may not be started: an unset admin variable, or a folder a jail may bind."""
    problems = []
    try:
        store.admin_env.values()
    except RuntimeError as exc:
        problems.append(str(exc))
    return problems + _placement_problems(store, mounts)


def _default_problems(declared) -> list[str]:
    """The default store (``DEFAULT_STORE``) missing from ``declared``."""
    return [] if any(s.name == DEFAULT_STORE for s in declared) else [
        f"no store is named {DEFAULT_STORE!r}, the default store units' keys and the lead's catalog query "
        f"bind to (declared: {', '.join(s.name for s in declared)}); name one store {DEFAULT_STORE!r}"]


def config_defects() -> list[str]:
    """What ``store_config`` refuses that no host fixes by providing the store (S13a, main's ruling):
    no default store, a folder a jail may bind, a toolkit manifest naming an undeclared store. A
    block that does not parse raises ``ValueError``. Unset admin variables are left out: they mean
    the store is absent from this host, which :func:`check_store_config` still refuses."""
    declared = stores()
    mounts = _jail_mounts()
    return (_default_problems(declared)
            + [f"store {s.name}: {p}" for s in declared for p in _placement_problems(s, mounts)]
            + _toolkit_store_names(declared))


def check_store_config() -> None:
    """The block parses; it declares the default store (``DEFAULT_STORE``: the one units' keys and the
    lead's catalog query bind to); each store's admin variables are set; no jail may bind a local
    store's folders (its objects are reached only through the gateway, its state holds the IAM
    secrets)."""
    try:
        declared = stores()
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    mounts = _jail_mounts()
    problems = _default_problems(declared)
    problems += [f"store {s.name}: {p}" for s in declared for p in _config_problems(s, mounts)]
    problems += _toolkit_store_names(declared)
    if problems:
        raise RuntimeError("; ".join(problems))


def _toolkit_store_names(declared) -> list[str]:
    """Every declared toolkit's manifest ``writes``/``reads`` names a declared store (read as data;
    S7, main's ruling B). A toolkit block that does not parse is the toolkit checks' to name."""
    from auto_research.store.ci import undeclared_store_names
    from auto_research.toolkit_contract import toolkits

    try:
        kits = toolkits()
    except Exception:  # noqa: BLE001 — named by the toolkit_* launch checks
        return []
    names = [s.name for s in declared]
    return [p for kit in kits for p in undeclared_store_names(kit.root, kit.packages(kit.root), names)]


def _answers(url: str) -> bool:
    """Whether an HTTP server answers ``url`` (any status: an unsigned request is refused, 403)."""
    try:
        with urllib.request.urlopen(url, timeout=5):
            return True
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False


def refusals(store) -> list[str]:
    """Why ``store`` may not be started (``store_config``'s reasons for it); empty when it may."""
    return _config_problems(store, _jail_mounts())


def check_store_servers() -> None:
    """Every local store's servers answer (the run's launch step starts them before this check,
    ``graph.runtime.start_run_stores``; this check starts nothing); every remote S3 endpoint answers."""
    mounts = _jail_mounts()

    def check(store) -> None:
        if isinstance(store, LocalStore):
            refused = _config_problems(store, mounts)
            if refused:
                raise RuntimeError(f"not started, store_config refuses it: {'; '.join(refused)}")
            down = launch.down(store)
            if down:
                raise RuntimeError(f"not answering: {', '.join(down)}; the run's launch step starts a local "
                                   f"store, so see its store.launch record and {store.state_dir / 'logs'}")
        elif not _answers(store.s3_endpoint):
            raise RuntimeError(f"the S3 endpoint {store.s3_endpoint} does not answer")
    _each(check)


def _check_admin(store) -> None:
    """The admin key works on ``store``: it lists the buckets, and on a local store the IAM users."""
    from botocore.exceptions import ClientError, EndpointConnectionError

    services = ("s3", "iam") if isinstance(store, LocalStore) else ("s3",)
    for service in services:
        client = store.admin_client(service)
        try:
            client.list_buckets() if service == "s3" else client.list_users()
        except EndpointConnectionError as exc:
            raise RuntimeError(f"the {service.upper()} endpoint does not answer ({exc}); see store_servers") from exc
        except ClientError as exc:
            raise RuntimeError(f"the {service.upper()} endpoint refuses the admin key "
                               f"({store.admin_env.s3_access_key}): {exc}; if the key changed, stop the store "
                               "(launch.stop) so it restarts with it") from exc


def _check_buckets(store) -> None:
    """Every bucket of ``store`` exists and keeps versions (status Enabled)."""
    from botocore.exceptions import ClientError

    s3 = store.admin_client("s3")
    problems = []
    for bucket in store.buckets:
        try:
            s3.head_bucket(Bucket=bucket)
        except ClientError as exc:
            problems.append(f"bucket {bucket} is missing ({exc.response['Error'].get('Code')}); create it")
            continue
        status = s3.get_bucket_versioning(Bucket=bucket).get("Status", "never enabled")
        if status != "Enabled":
            problems.append(f"bucket {bucket} versioning is {status}; enable it (PutBucketVersioning "
                            "Status=Enabled)")
    if problems:
        raise RuntimeError("; ".join(problems))


def check_store_admin() -> None:
    """The admin key works: it lists the buckets, and on a local store the IAM users the runtime issues."""
    _each(_check_admin)


def check_store_buckets() -> None:
    """Every declared bucket exists and keeps versions (status Enabled): a receipt names a version id."""
    _each(_check_buckets)


def ready_problems(store) -> list[str]:
    """What keeps ``store`` from serving a run, in the preflight's words, each named by its item:
    servers that do not answer (local), the admin key refused, a bucket missing or not versioned, the
    catalog refusing the admin role or holding another schema. The
    run's launch step asks this before it reconciles, so a store not ready reads as the preflight
    would say it; empty when ready."""
    if isinstance(store, LocalStore) and (down := launch.down(store)):
        return [f"store_servers: not answering: {', '.join(down)}; see {store.state_dir / 'logs'}"]
    for item, check in (("store_admin", _check_admin), ("store_buckets", _check_buckets)):
        try:
            check(store)
        except Exception as exc:  # noqa: BLE001 - the first failing item says what to do
            return [f"{item}: {exc}"]
    # On a thread of its own: the caller may run inside an event loop (the launch step does not, the
    # preflight might).
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        problem = pool.submit(asyncio.run, _catalog_problem(store)).result()
    return [f"store_catalog: {problem}"] if problem is not None else []


async def _catalog_problem(store) -> str | None:
    """Why ``store``'s catalog does not serve: it refuses the admin role, does not answer, or its schema
    is not the one this code reads (``store.catalog.check_schema``); None when it serves."""
    import psycopg

    from auto_research.store.catalog import check_schema

    try:
        dsn = store.admin_dsn()
        try:
            psycopg.connect(dsn, connect_timeout=5).close()
        except psycopg.OperationalError as exc:
            # A failed connection carries no SQLSTATE: the server's FATAL text says why.
            refused = any(t in str(exc) for t in ("password authentication failed", "does not exist",
                                                  "no pg_hba.conf entry"))
            return ("the catalog " + ("refuses the admin role" if refused else "does not answer")
                    + f": {str(exc).strip()}")
        await check_schema(dsn)
    except Exception as exc:  # noqa: BLE001 - reported as the store's problem
        return str(exc)
    return None


async def check_store_catalog() -> None:
    """The catalog database accepts the admin role and holds the schema version this code reads, with
    its roles (``store.catalog.check_schema``, item S2)."""
    problems = []
    for store in stores():
        if (problem := await _catalog_problem(store)) is not None:
            problems.append(f"store {store.name}: {problem}")
    if problems:
        raise RuntimeError("; ".join(problems))
