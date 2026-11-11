"""``python -m auto_research.store status|start|stop|revoke|human-password|clear-flag|move-current|ingest|produce|ci-sweep``
— the human's view of the local stores' servers and the keys runs hold (coordinator rulings A and 1,
2026-11-10), and the human's own acts on the catalog, as its own login.

``status`` prints, per declared store, whether its servers run (host, pids, start time), which of them
do not answer, and the keys the catalog holds live (per run and agent). ``stop`` stops a local store's
servers on this host; a store whose catalog holds live keys (a run may still be using it), or whose
catalog cannot be read, is left running unless ``--force``. Runs start the servers themselves (the launch step in
``graph.runtime.start_run_stores``); servers outlive the runs, so stopping is this command's job.
``revoke --run ID`` revokes every key run ``ID`` holds (its units' and its reconciler's); a run whose
process still runs on this host is left alone unless ``--force``. A run's launch revokes the keys of
dead runs of this host by itself; this is for a run of another host, or one with no record here.
``human-password --store NAME`` sets the password of the human's own catalog login
(``catalog.HUMAN_ROLE``, in ``owner``, made without one by the store's first start), typed at a prompt
or read from ``--password-file``; no ``.env`` and no runtime process holds it.
``clear-flag --store NAME --recipe HASH --flag NAME --reason TEXT`` clears a flag the runtime stamped on
a product (``catalog.clear_flag``), as that login: the history keeps the stamp and the clear; readers,
and the products made from it, stop carrying the flag (unless they derive it: ``unverified`` from a failed
state, ``synthetic`` from a synthetic bucket, said in the output); the next reconciler pass may promote it.
``move-current --store NAME --bucket B --kind K --subject S --recipe HASH --reason TEXT`` moves a
pointer (keyed by bucket, kind and subject) as that login: a human override, held until a product
claimed after it qualifies.
``start [--create-buckets]`` starts a local store's servers (``launch.start``, idempotent) and, with
``--create-buckets``, creates each declared bucket it lacks with versioning on; it then names what keeps
the store from serving a run (``health.ready_problems``).
``ingest --store NAME --mapping FILE`` stores files already on disk as products of the human's ingest
recipe (``store.ingest``), as the human's login; ``--check`` resolves and hashes the files and prints
the plan, writing nothing.
``produce --store NAME --unit UNIT --env ENV [--readonly DIR ...] -- COMMAND ...`` runs one command as a
producer unit, the way a run runs a toolkit tool: a one-shot jailed process in the toolkit environment
``ENV`` (``runner.run_in_environment``), holding keys issued to ``UNIT`` for this job alone
(``keys.issue``, role ``producer``) with the toolkit's commit and the environment's fingerprint beside
them, the folders ``--readonly`` names bound read-only (none may meet a folder the jail binds writable);
the keys are revoked however it ends. The toolkit must be on its authoring branch with a clean tree:
what a product stamps is its commit.
``ci-sweep`` cleans up what the CI pipeline's throwaway stores left behind (``store.ci.sweep_leftovers``):
it ends their leftover servers and removes their folders (``/var/tmp/ar-ci-<uid>/``), and leaves every
store still in use alone (its life lock held). It is the
one way to clean those folders; every throwaway start runs it too.
"""

from __future__ import annotations

import argparse
import os
import sys
import time


def _live_keys(store) -> list[tuple[str, str, str]] | str:
    """``(kind, agent, run)`` of every live credential in ``store``'s catalog, or why it was not read."""
    import psycopg

    try:
        with psycopg.connect(store.admin_dsn(), connect_timeout=5) as conn:
            return conn.execute("SELECT kind, agent, run FROM catalog.credential WHERE revoked_at IS NULL "
                                "ORDER BY issued_at").fetchall()
    except Exception as exc:  # noqa: BLE001 - status reports what it cannot read
        return f"catalog not read ({str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__})"


def _status(store) -> list[str]:
    from auto_research.store import launch
    from auto_research.store.config import LocalStore

    lines = [f"store {store.name} ({store.kind}, S3 {store.s3_endpoint})"]
    if isinstance(store, LocalStore):
        record = launch.status(store)
        if record is None:
            lines.append("  servers: not running")
        else:
            started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.get("started_at", 0)))
            lines.append(f"  servers: running on {record.get('host')} since {started}, pids {record.get('pids')}")
            down = launch.down(store)
            lines.append("  answering: all" if not down else f"  not answering: {', '.join(down)}")
    keys = _live_keys(store)
    if isinstance(keys, str):
        lines.append(f"  live keys: {keys}")
    else:
        lines.append(f"  live keys: {len(keys)}")
        lines += [f"    {kind:8} {agent}  run {run}" for kind, agent, run in keys]
    return lines


def _stop(store, force: bool) -> str:
    from auto_research.store import launch
    from auto_research.store.config import LocalStore

    if not isinstance(store, LocalStore):
        return f"store {store.name}: remote, nothing to stop"
    if launch.status(store) is None:
        return f"store {store.name}: not running"
    keys = _live_keys(store)
    if not force and isinstance(keys, str):
        raise SystemExit(f"store {store.name}: {keys}, so whether a run uses it is unknown; pass --force to stop it")
    if not force and keys:
        runs = sorted({run for _, _, run in keys})
        raise SystemExit(f"store {store.name}: {len(keys)} live key(s) of run(s) {', '.join(runs)}; a run may "
                         "be using it. Stop those runs, or pass --force")
    return f"store {store.name}: " + ("stopped" if launch.stop(store) else "not running on this host")


def _revoke(store, run: str, force: bool) -> str:
    import asyncio

    import socket

    from auto_research.graph.runtime import run_process_alive, run_process_record
    from auto_research.store import keys

    record = run_process_record(run)
    if not force and run_process_alive(run):
        raise SystemExit(f"run {run} still runs on this host (pid {record.get('pid')}); stop it, or pass --force")
    if not force and record is not None and record.get("host") != socket.gethostname():
        raise SystemExit(f"run {run} was recorded on host {record.get('host')} (pid {record.get('pid')}); "
                         "check there that it has ended, then pass --force")
    revoked = asyncio.run(keys.revoke_run(store, run))
    return f"store {store.name}: run {run}: " + (f"revoked {', '.join(revoked)}" if revoked else "no live keys")


def _human_password(path: str | None, *, confirm: bool = False) -> str:
    """The human's catalog password: the first line of ``path`` (a file the human keeps), else typed at
    a prompt (``getpass``). Never read from ``.env`` or the runtime's environment."""
    import getpass
    import os
    import stat

    if path:
        if stat.S_IMODE(os.stat(path).st_mode) & 0o077:
            raise SystemExit(f"{path} is readable by others; make it yours only (chmod 600 {path})")
        with open(path, encoding="utf-8") as handle:
            password = handle.readline().rstrip("\r\n")
    else:
        try:
            password = getpass.getpass("Password of the human's catalog login: ")
            if confirm and getpass.getpass("Again: ") != password:
                raise SystemExit("the two passwords differ")
        except EOFError:
            raise SystemExit("type the password at a terminal, or pass --password-file") from None
    if not password:
        raise SystemExit("give a password (typed, or the first line of --password-file)")
    return password


def _human_dsn(store, password: str) -> str:
    """The human's own login (``catalog.HUMAN_ROLE``) to ``store``'s catalog."""
    from psycopg.conninfo import make_conninfo

    from auto_research.store.catalog import HUMAN_ROLE
    from auto_research.store.config import LocalStore

    if isinstance(store, LocalStore):
        return make_conninfo(host=str(store.socket_dir), port=store.ports.catalog, dbname=store.catalog.database,
                             user=HUMAN_ROLE, password=password)
    return make_conninfo(store.catalog_dsn, user=HUMAN_ROLE, password=password)


def _as_human(store, password_file: str | None, act):
    """Run ``act(conn)`` on an autocommit connection as the human's own login; one line per database error."""
    import asyncio

    import psycopg

    password = _human_password(password_file)

    async def run():
        async with await psycopg.AsyncConnection.connect(_human_dsn(store, password), autocommit=True) as conn:
            return await act(conn)

    try:
        return asyncio.run(run())
    except psycopg.Error as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise SystemExit(f"store {store.name}: {first}") from None


def _set_human_password(store, password_file: str | None) -> str:
    """The human sets its own login's password, through the admin role (a SCRAM verifier only)."""
    import asyncio

    import psycopg

    from auto_research.store import catalog

    password = _human_password(password_file, confirm=True)

    async def run():
        async with await psycopg.AsyncConnection.connect(store.admin_dsn(), autocommit=True) as conn:
            await catalog.set_human_password(conn, password)

    try:
        asyncio.run(run())
    except (psycopg.Error, catalog.CatalogError) as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise SystemExit(f"store {store.name}: {first}") from None
    return f"store {store.name}: the password of {catalog.HUMAN_ROLE} is set"


def _clear_flag(store, recipe: str, flag: str, reason: str, password_file: str | None) -> str:
    from auto_research.store import catalog

    async def clear(conn) -> tuple[dict, list[str]]:
        done = await catalog.clear_flag(conn, recipe, flag, reason)
        # Flags a read also derives from the product itself, which a clear does not change.
        row = await (await conn.execute(
            "SELECT p.state, ARRAY(SELECT DISTINCT o.bucket FROM catalog.object o WHERE o.recipe_hash = p.recipe_hash) "
            "FROM catalog.product p WHERE p.recipe_hash = %s", (recipe,))).fetchone()
        derived = []
        if flag == "unverified" and row and row[0] in ("failed", "nondeterministic"):
            derived.append(f"its state is {row[0]}, so readers still carry unverified")
        if flag == "synthetic" and row and set(row[1]) & set(store.synthetic_buckets):
            derived.append("it lies in a synthetic bucket, so readers still carry synthetic")
        return done, derived

    done, derived = _as_human(store, password_file, clear)
    covered = ", ".join(done.get("receipts") or []) or "none"
    lines = [f"store {store.name}: product {recipe[:12]}: flag {flag} cleared ({reason}); it covered the stamps "
             f"of {covered}; products made from this one stop carrying it, unless another ancestor carries it too"]
    lines += [f"  note: {d}" for d in derived]
    return "\n".join(lines)


def _move_current(store, bucket: str, kind: str, subject: str, recipe: str, reason: str,
                  password_file: str | None) -> str:
    """The human's pointer override, as its own login (it holds until a product claimed after it qualifies)."""
    from auto_research.store import catalog

    async def move(conn) -> dict:
        pointer = await catalog.current(conn, bucket, kind, subject)
        return await catalog.move_current(conn, bucket, kind, subject, recipe,
                                          pointer["seq"] if pointer else 0, reason)

    moved = _as_human(store, password_file, move)
    where = f"({bucket}, {kind}, {subject})"
    if moved["status"] != "moved":
        raise SystemExit(f"store {store.name}: the pointer of {where} moved meanwhile; run it again")
    return f"store {store.name}: {where} now points at {recipe[:12]} ({reason})"


def _start(store, create_buckets: bool) -> str:
    """Start ``store``'s servers (local; idempotent), create its missing buckets with versioning on when
    asked, and name what keeps it from serving a run."""
    from auto_research.store import health, launch
    from auto_research.store.config import LocalStore

    lines = []
    if isinstance(store, LocalStore):
        if refused := health.refusals(store):
            raise SystemExit(f"store {store.name}: {'; '.join(refused)}")
        record = launch.start(store)
        lines.append(f"store {store.name}: serving on {store.s3_endpoint}, pids {record.get('pids')}")
    if create_buckets:
        from botocore.exceptions import ClientError

        s3 = store.admin_client("s3")
        for bucket in store.buckets:
            try:
                s3.head_bucket(Bucket=bucket)
            except ClientError:
                s3.create_bucket(Bucket=bucket)
                lines.append(f"  bucket {bucket} created")
            if s3.get_bucket_versioning(Bucket=bucket).get("Status") != "Enabled":
                s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
                lines.append(f"  bucket {bucket}: versioning enabled")
    problems = health.ready_problems(store)
    lines.append("  ready" if not problems else "  not ready: " + "; ".join(problems))
    return "\n".join(lines)


def _ingest(store, mapping: str, password_file: str | None, check: bool) -> str:
    """The human's ingest (``store.ingest``): the plan alone with ``check``, else each product stored
    as the human's login, the admin key signing the uploads; one JSON line per product."""
    import json

    from auto_research.store import ingest

    try:
        planned = ingest.plan(store, ingest.load_mapping(mapping))
    except ingest.IngestError as exc:
        raise SystemExit(str(exc)) from None
    if check:
        return "\n".join(json.dumps(ingest.describe(p)) for p in planned)
    s3 = ingest.client(store)

    async def act(conn) -> list[str]:
        lines = []
        for p in planned:
            done = await ingest.ingest_one(store, p, conn=conn, s3=s3)
            lines.append(json.dumps({"bucket": p.bucket, "kind": p.kind, "subject": p.subject,
                                     "status": done["status"], "recipe_hash": done["recipe_hash"],
                                     "record": done["record"], "objects": len(done["objects"])}))
        return lines

    try:
        lines = _as_human(store, password_file, act)
    except ingest.IngestError as exc:
        raise SystemExit(f"store {store.name}: {exc}") from None
    lines.append(f"next: python -m auto_research.store.reconcile {store.name}   (verifies the new products)")
    return "\n".join(lines)


def _environment(name: str):
    """The built toolkit environment ``name`` (``envs/<name>/``, built as ``ar-<name>``)."""
    from auto_research.env import env_catalogue

    found = [e for e in env_catalogue() if e.name in (name, f"ar-{name}")]
    if not found:
        raise SystemExit(f"no built environment {name!r}; built: {', '.join(e.name for e in env_catalogue())}")
    if getattr(found[0], "superseded", False):
        raise SystemExit(f"environment {found[0].name} is a build of an earlier definition; rebuild it first")
    return found[0]


def _produce(store, unit: str, env_name: str, readonly: list[str], timeout: int, command: list[str]) -> int:
    """Run ``command`` as producer ``unit`` of ``store`` (see the module text); its exit code. The keys
    are revoked on every way out: the job's end, its timeout, an exception, and SIGTERM or SIGHUP to
    this process (turned into an exit, so the revoke runs)."""
    import asyncio
    import shlex
    import signal
    import subprocess
    from pathlib import Path

    from auto_research.sandbox.jail import policy_from_config
    from auto_research.sandbox.merge_flow import ToolkitBranchUnclean, check_toolkit_branch_clean
    from auto_research.sandbox.runner import run_in_environment
    from auto_research.store import keys
    from auto_research.toolkit_contract import toolkit

    if not command:
        raise SystemExit("produce needs a command after --")
    root = toolkit().root
    try:
        check_toolkit_branch_clean(root, toolkit().authoring_branch)
    except ToolkitBranchUnclean as exc:
        raise SystemExit(f"{exc}\nCommit the toolkit first: a product stamps the toolkit's commit.") from None
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
    environment = _environment(env_name)
    folders = [Path(d).resolve() for d in readonly]
    missing = [str(d) for d in folders if not d.is_dir()]
    if missing:
        raise SystemExit(f"--readonly names no folder: {', '.join(missing)}")
    # A folder the jail binds writable (agent_fs_allow) would stay writable under, or over, the
    # read-only bind: refused, so --readonly always means read-only.
    writable = [Path(r).resolve() for r in policy_from_config().rw]
    overlap = [str(d) for d in folders for w in writable if d.is_relative_to(w) or w.is_relative_to(d)]
    if overlap:
        raise SystemExit(f"--readonly {', '.join(overlap)} meets a folder the jail binds writable (agent_fs_allow); "
                         "name a folder outside it")
    run = f"cli-produce-{time.strftime('%Y%m%d%H%M%S')}-{os.getpid()}"

    def leave(signum, _frame):
        raise SystemExit(128 + signum)

    saved = {sig: signal.signal(sig, leave) for sig in (signal.SIGTERM, signal.SIGHUP)}
    out, err, code = "", "", 1
    try:
        credentials = asyncio.run(keys.issue(store, unit, "producer", run))
        print(f"store {store.name}: {unit} holds producer keys ({credentials.principal}, run {run})",
              file=sys.stderr, flush=True)
        try:
            done = run_in_environment(environment, shlex.join(command), toolkit_root=root,
                                      label=f"store-produce-{unit}", timeout=timeout,
                                      store_env=credentials.env(), readonly=tuple(map(str, folders)))
            out, err, code = done.stdout, done.stderr, done.returncode
        except subprocess.TimeoutExpired as exc:
            def text(value):
                return value.decode("utf-8", "replace") if isinstance(value, bytes) else (value or "")

            out, err, code = text(exc.stdout), text(exc.stderr) + f"\nthe job ran past --timeout {timeout} s\n", 124
    finally:
        sys.stdout.write(out)
        sys.stderr.write(err)
        try:
            # The issue may have failed part-way: revoking the principal is idempotent.
            asyncio.run(keys.revoke(store, unit, run))
            print(f"store {store.name}: the keys of {unit} (run {run}) are revoked", file=sys.stderr)
        finally:
            for sig, handler in saved.items():
                signal.signal(sig, handler)
    return code


def main(argv: list[str] | None = None) -> int:
    from auto_research.store.config import stores

    parser = argparse.ArgumentParser(prog="python -m auto_research.store", description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=("status", "start", "stop", "revoke", "human-password", "clear-flag",
                                           "move-current", "ingest", "produce", "ci-sweep"))
    parser.add_argument("--store", help="one store by name (default: every declared store)")
    parser.add_argument("--run", help="revoke: the run whose keys to revoke")
    parser.add_argument("--force", action="store_true",
                        help="stop: also a store whose catalog holds live keys; revoke: also a run still running")
    parser.add_argument("--recipe", help="clear-flag, move-current: the product's recipe hash")
    parser.add_argument("--flag", choices=("failed", "synthetic", "undeclared_write", "unverified"),
                        help="clear-flag: the flag to clear")
    parser.add_argument("--bucket", help="move-current: the pointer's bucket")
    parser.add_argument("--kind", help="move-current: the pointer's kind")
    parser.add_argument("--subject", help="move-current: the pointer's subject")
    parser.add_argument("--reason", help="clear-flag, move-current: why (kept in the catalog)")
    parser.add_argument("--password-file", help="human-password, clear-flag, move-current, ingest: a file whose first "
                                                "line is the human's catalog password (default: a prompt)")
    parser.add_argument("--create-buckets", action="store_true",
                        help="start: create each declared bucket the store lacks, with versioning on")
    parser.add_argument("--mapping", help="ingest: the YAML file naming each product's bucket, kind, subject, "
                                          "folder and files")
    parser.add_argument("--check", action="store_true", help="ingest: resolve and hash the files, write nothing")
    parser.add_argument("--unit", help="produce: the unit name the job's keys are issued to")
    parser.add_argument("--env", help="produce: the toolkit environment to run in (envs/<name>/)")
    parser.add_argument("--readonly", action="append", default=[],
                        help="produce: a folder the job reads, bound read-only (repeatable)")
    parser.add_argument("--timeout", type=int, default=6 * 3600, help="produce: seconds before the job is ended")
    argv = list(sys.argv[1:] if argv is None else argv)
    # produce: everything after the first "--" is the command, never parsed as options here.
    command = argv[argv.index("--") + 1:] if "--" in argv else []
    args = parser.parse_args(argv[:argv.index("--")] if "--" in argv else argv)
    if command and args.action != "produce":
        parser.error(f"unexpected arguments: {' '.join(command)}")
    if args.action == "ingest" and not (args.store and args.mapping):
        parser.error("ingest needs --store and --mapping")
    if args.action == "produce" and not (args.store and args.unit and args.env and command):
        parser.error("produce needs --store, --unit, --env and -- COMMAND")
    if args.action == "revoke" and not args.run:
        parser.error("revoke needs --run")
    reason = (args.reason or "").strip()
    if args.action == "clear-flag" and not (args.recipe and args.flag and reason and args.store):
        parser.error("clear-flag needs --store, --recipe, --flag and --reason")
    if args.action == "move-current" and not (args.recipe and args.bucket and args.kind and args.subject and reason
                                              and args.store):
        parser.error("move-current needs --store, --bucket, --kind, --subject, --recipe and --reason")
    if args.action == "human-password" and not args.store:
        parser.error("human-password needs --store")
    if args.action == "ci-sweep":  # the CI pipeline's throwaway stores: no declared store involved
        from auto_research.store import ci

        print("\n".join(ci.sweep_leftovers()) or "nothing left behind by the CI pipeline's stores")
        return 0
    declared = [s for s in stores() if args.store in (None, s.name)]
    if not declared:
        raise SystemExit(f"no store named {args.store!r} is declared in `stores:`")
    for store in declared:
        if args.action == "status":
            print("\n".join(_status(store)))
        elif args.action == "stop":
            print(_stop(store, args.force))
        elif args.action == "start":
            print(_start(store, args.create_buckets))
        elif args.action == "ingest":
            print(_ingest(store, args.mapping, args.password_file, args.check))
        elif args.action == "produce":
            return _produce(store, args.unit, args.env, args.readonly, args.timeout, command)
        elif args.action == "human-password":
            print(_set_human_password(store, args.password_file))
        elif args.action == "clear-flag":
            print(_clear_flag(store, args.recipe, args.flag, args.reason, args.password_file))
        elif args.action == "move-current":
            print(_move_current(store, args.bucket, args.kind, args.subject, args.recipe, args.reason,
                                args.password_file))
        else:
            print(_revoke(store, args.run, args.force))
    return 0


if __name__ == "__main__":
    sys.exit(main())
