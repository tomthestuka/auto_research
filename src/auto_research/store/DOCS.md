# `store/` — the data stores

## Purpose
The data a run reads and writes beyond its own workspace: observation archives and the products tools
make from them. A store is **bytes behind the S3 contract plus a Postgres catalog** (Phase 3 step 0b;
plan `tmp_phase3/0b/PLAN_0b_formal.md`, rulings `tmp_phase3/0b/PLAN.md` §2a). This package declares the
stores (`config`), starts and stops a local store's servers (`launch`), and checks every store at launch
(`health`). The catalog's schema and calls are `catalog` (item S2); the reconciler is `reconcile` (item S6);
the CI pipeline's throwaway store is `ci` (item S7). The human's ingest of files on disk is `ingest` (item S11).

## Architecture
```
orchestration.yaml stores: ──► config.stores()  (bound once per process, like the toolkit contract)
                                   │
           ┌───────────────────────┴───────────────────────┐
     kind: local                                      kind: remote
 launch.start(store), the run's launch step            an S3 endpoint + a catalog DSN that already run
   versitygw iam   127.0.0.1:<iam>  + socket_dir/iam.sock     (nothing to start)
   versitygw posix 127.0.0.1:<s3>   root, --versioning-dir state_dir/versions,
                   --access-log state_dir/logs/access.log, --iam-standalone-policy-cache-ttl 0
   postgres        127.0.0.1:<catalog> + socket_dir, data state_dir/postgres, scram auth
           └───────────────────────┬───────────────────────┘
                     health: store_config · store_servers · store_admin · store_buckets · store_catalog
```
- **One server set per state_dir.** Every server process inherits an exclusive `flock` on
  `state_dir/server.lock` and holds it for its life, so a free lock means no server of the store runs
  anywhere: NFS carries the lock to the server unless mounted `nolock` or `local_lock=flock|all`, which
  `start` refuses for state_dir (cross-host behaviour on `/xray02` not yet probed). A server process is
  known by holding that file open (`holders`, from `/proc/*/fd`), never by a recorded pid, so a recycled
  pid is never signalled. `state_dir/servers.json` (written atomically after the set answers) records
  host, pids and settings (folders, ports, database, admin user, admin key id).
- **A started set is confirmed to be its own** (`_confirm_own`, S7 follow-up, main's ruling D): a port
  free when checked can be bound by another process first (seen under concurrent starts), whose server
  then answers the health check. Before the record is written, every started versitygw process must be
  alive and the store's root key must work on its S3 and IAM endpoints; otherwise what was started is
  stopped and `start` raises, naming the ports.
  Residual: two configured stores sharing one admin key are told apart only by the losing server's
  exit (it exits within 0.1 s on a taken port; the check runs after Postgres starts).
- **`start` is idempotent and never kills a busy set.** Lock held by this host's processes with the
  record's settings: wait up to `READY_TIMEOUT_S` for every server to answer, then return the record or
  refuse (processes alive but silent are left alone). Held with no record (a start killed before writing
  it) or with a recorded server gone: stop the holders and start afresh. Held by no process of this host:
  another host's set, refused. Other settings (a rotated admin key included): refused until `stop`. Ports
  something else already answers on: refused. `launch.lock` serialises start, stop and status.
- **The servers outlive the starting process** (their own session): concurrent runs share them, and
  stopping is explicit (`stop`). Their environment holds PATH, a C locale and the root key only, never
  this process's API keys; only versitygw gets the root key (`ROOT_ACCESS_KEY_ID`/`ROOT_SECRET_ACCESS_KEY`,
  by environment), Postgres never. The catalog password reaches `initdb` on its stdin. No secret appears on
  a command line or in a file of ours. The admin variables are read from the process environment after
  loading the project `.env` (no override).
- **Placement.** `root` and `state_dir` must take `user.` xattrs (versitygw keeps object and version
  metadata there; probe P1: `/nfsdata1` refuses them, `/xray02` takes them), checked by setting and
  removing one on each folder's own inode. The IAM socket path is at most 107 bytes and absolute (probe:
  a relative one breaks start-up, the gateway chdir's to its root); `socket_dir` is mode 0700, outside
  the temp root (a jail may bind host `/tmp`) and outside every jail mount (an abstract socket would be
  reachable from a net-sharing jail). No folder a jail may bind (`agent_fs_allow`, `agent_fs_readonly`,
  `test_data`, toolkit roots, `human_envs`, `env_externals` roots, the runtime dir) meets `root`,
  `state_dir` or `socket_dir` (`store_config`; `store_servers` starts no store it refuses): tools reach
  the store through S3 only. Folders are compared after resolving symlinks.
- **Revocation lag 0** (`--iam-standalone-policy-cache-ttl 0`, probe P5): a key or policy change applies
  on the next request.
- **Postgres** is initialised once (`initdb -U <admin_user> --auth=scram-sha-256 -E UTF8 --no-locale`),
  started by `pg_ctl` on its Unix socket in `socket_dir` and on 127.0.0.1; every fresh start creates the
  catalog database when absent and installs or confirms its schema (`catalog.apply_schema`, idempotent,
  as the admin superuser, on a thread of its own since `start` may run inside the preflight's event loop).
  The admin role stays the cluster's superuser and joins no agent role; one catalog per cluster (roles
  are cluster-wide).
- **Adapters.** Server-specific code is two runtime adapters (portability rule): key issuance and read
  records. `adapters` names them per kind (`versitygw_iam`/`access_log` local, `aws_sts`/`none` remote)
  for items S4 and S5.
- **Per-agent keys** (`keys`, item S4; human rulings 2026-11-10: each agent is its own S3 user and
  Postgres role for the run, the role is its policy, key lifetime = the run, revocation only for a dead
  agent):
  ```
  keys.issue(store, unit, role_class, run)              keys.revoke(store, unit, run)
    IAM user <principal> + inline policy "ar-role"        access keys deleted, catalog marks them revoked
    access key ──► catalog.record_credential             inline policy and user deleted
    Postgres login <principal> in role_class             login NOLOGIN, open sessions terminated
      (catalog.issue_role, random password,                (catalog.end_role)
       VALID UNTIL issue + ROLE_VALID_FOR)
    ──► Credentials.env(): AWS_* + PG*, for the unit's jailed tool processes only
  ```
  One name (`principal(run, unit)`, `ar_<run>_<unit>_<hash8>`) is both the IAM user and the Postgres
  login, and the `agent` of both credential rows, so a receipt reader (S5) maps an access key to its
  unit. Role templates, inline identity policies over the store's buckets (bucket names in a user policy
  work on versitygw; bare user names in a bucket policy do not, probe Q1):

  | class | allows | on |
  |---|---|---|
  | `reader` | `ListBucket`, `ListBucketVersions`, `GetObjectVersion`, `GetObjectVersionTagging` | the buckets, their keys |
  | `producer` | reader + `PutObject`, `AbortMultipartUpload`, `ListMultipartUploadParts` (no tagging: mutable facts live in the catalog) | every key of the buckets |
  | `reconciler` (the runtime's own login; its S3 key signs the reconciler's pass, `reconcile(s3=)`) | as `reader` | |
  | all | **deny** `DeleteObject`, `DeleteObjectVersion`, `PutBucketVersioning`, `PutBucketPolicy`, `DeleteBucketPolicy`, `PutLifecycleConfiguration`, `DeleteBucket` | the buckets, their keys |

  No `s3:GetObject`: an unpinned read is refused, so every read names a version (ruling (e)). Writes go to
  any key of the store's buckets (human ruling: products live at `<bucket>/<kind>/<subject>/<short
  hash>/`, no separate prefix); create-only is the toolkit helper's `If-None-Match:*`, not the policy's
  (ruling option A: a policy requiring it blocks multipart). Which role a unit holds: the store's `roles`
  (`roles.producer` patterns, default `analyst_worker_*`; every other unit `reader`, the lead included)
  until unit definitions name it (Phase 3 step 3).
- **Where the keys go.** `graph.runtime` issues them (`RunKeys`) and hands each unit's `env()` to its own
  jailed tool processes only: its REPLs (`PersistentSandbox(store_env=)`, pool REPLs included) and a
  one-shot jailed process that is given them (`runner.run_in_environment(store_env=)`, its jail home
  and `/tmp` then the keys' own; no production caller passes keys yet). Keys are issued to the analyst workers,
  the units with jailed tool processes today, and to the analyst lead, a `reader` by the default
  `roles` (S9 ruling 5): the runtime runs its catalog queries (`query`, below) on its login to the
  default store and hands its keys to no process. They travel in a mode-0600 file in the
  jail's private `/tmp` (`runner.write_store_env`), read and removed at the process's start, never on a
  command line (`/proc/*/cmdline` is world-readable) or through the launcher's clean start. Beside them
  each keyed process receives `AR_TOOLKIT_COMMIT` and `AR_ENV_FINGERPRINT`, the commit and environment
  state its receipts are stamped with (`runner.keyed_env`), which the toolkit's store helper requires. The shared
  shell server (`bash_exec`, one per run for every worker) gets none: all store I/O goes through toolkit
  tools (ruling R2/R3). No key reaches the orchestrator's environment, a prompt or the ledger
  (`store.credentials.issue` records the role, principal and key id only).
- **Run lifecycle** (coordinator rulings B, 1, 4). `graph.runtime.start_run_stores`, before the
  preflight, per store: starts it when local and `store_config` accepts it; requires it ready
  (`health.ready_problems`: servers, admin key, buckets and versioning, worded as the preflight items
  word them); revokes the keys of dead runs of this host (`sweep_dead_runs`: a run is dead when its
  runtime process, recorded in `ledgers/<run>/runtime.json` as host, pid and the pid's start time, is
  gone; a run of another host or with no record here is never touched); then reconciles the store
  (`reconcile.reconcile`) as the run's own reconciler login, its S3 key signing the pass. The
  preflight's `store_servers` only checks. Units' keys are issued as they are
  attached; the run's teardown revokes every key last before the channel (leaf to root), also on a
  stop, a failed setup or a failed launch, and once more in the run's outermost `finally` (idempotent),
  so a teardown cut short by an outer cancel or a timed-out stop step leaves none. A re-issue of the same
  (run, unit) (a resumed run) deletes the earlier keys and ends the earlier login's open sessions. A unit that dies is revoked alone (`RunKeys.revoke`); no path
  ends a unit before its run today (a failed turn keeps its seat). `python -m auto_research.store
  status|stop` is the human's view (ruling A): servers outlive runs.

## Interfaces / Contracts
- `config.stores(config=None)` → the declared stores; without `config` the process's bound block (bound
  from `orchestration.yaml` on first use), with `config` that dict's block, never bound. Raises
  `ValueError` when the block is absent, empty or invalid. `store(name="db", config=None)`, `bind(config)`,
  `reset()`.
- `LocalStore` / `RemoteStore` (frozen pydantic, `extra="forbid"`, discriminated by `kind`): `.s3_endpoint`,
  `.region`, `.admin_s3()` → `(key id, secret)`, `.admin_dsn(dbname=None)` → libpq conninfo with the admin
  password, `.admin_client(service="s3"|"iam")` → boto3 client signed with the admin key, `.adapters`.
  Local also: `.iam_endpoint`, `.iam_socket`, `.catalog_socket`, `.access_log`.
- `launch.start(store)` → the running set's record (`{host, settings, started_at, pids}`), None for a
  remote store; raises `RuntimeError` naming the refusal. `launch.stop(store)` → whether a set ran on
  this host (all its holders stopped). `launch.status(store)` → the record while a server holds the
  state_dir, else None. `launch.down(store)` → what does not answer now (IAM, gateway, Postgres).
  `launch.holders(store)` → pids of this user's processes holding `server.lock`.
- `health.check_store_config`, `check_store_servers`, `check_store_admin`, `check_store_buckets`, async
  `check_store_catalog`: preflight items registered as `store_config` … `store_catalog`, each raising one
  `RuntimeError` naming every store's problem and what to do.
- `keys.issue(store, unit, role_class, run)` (async) → `Credentials`; `keys.revoke(store, unit, run)`
  (async), idempotent; `keys.RunKeys(run)`: `.issue(store, unit, role_class=None)` (default
  `store.role_of(unit)`), `.held()`, `.revoke(unit)`, `.revoke_all()` (last issued first, idempotent; every
  key attempted, failures raised together), `.pending()` (anything left to revoke). A `RunKeys` holds a
  unit from before its issue starts, so an issue that fails or is cancelled part-way is revoked again at
  the run's end. A remote store raises `NotImplementedError` (the `aws_sts` adapter is not built). An
  issue that fails part-way revokes what it made; a rollback that fails too is noted on the raised
  error. A revoke marks every live S3 key the catalog records for the principal revoked (also keys an
  earlier revoke deleted while the catalog did not answer).
- `keys.revoke_principal(store, name)`, `keys.live_runs(store)` → `{run: [principal, ...]}` of live keys,
  `keys.revoke_run(store, run)` → the principals revoked (every one attempted, failures raised together).
- `python -m auto_research.store status|start|stop|revoke|human-password|clear-flag|move-current|ingest|produce|ci-sweep [options]` (`ci-sweep`: see "Life and leftovers" under CI)
  (`__main__.main(argv)`; `start`, `ingest`, `produce`: `## Migration and ingest` below). The human's own acts run as its own login (`catalog.HUMAN_ROLE`, `ar_human`),
  whose password is typed at a prompt (`getpass`) or read from `--password-file`, never from `.env`:
  `human-password --store NAME` sets it (through the admin role, a SCRAM verifier only; a password file
  must be its owner's only (mode 600) and its line ending is dropped); `clear-flag
  --store --recipe --flag --reason` clears a live flag (`catalog.clear_flag`); `move-current --store
  --bucket --kind --subject --recipe --reason` is a pointer override. `revoke --run ID` revokes every key of run `ID`; without `--force` it refuses
  a run whose process still runs on this host and a run recorded on another host (naming host and pid),
  and goes ahead for a run with no record here.
- Issues and revokes of one principal take turns on a Postgres advisory lock of its name (two launches
  sweeping one dead run collided in `ALTER ROLE`); a key or catalog row another revoke removed first
  passes.
- Invariant: no secret value is held in config, logged, or put on a command line.

## Catalog
The catalog (`catalog.sql`, `catalog.py`): the store's mutable facts in Postgres.

### Purpose
The catalog holds every mutable fact about the store's objects in one Postgres schema, `catalog`:
- products, named by recipe hash, with their claim state;
- the object versions each product wrote;
- the "current" pointer per (bucket, kind, subject), with its append-only history;
- which agent and run hold which key;
- an append-only event log.

Bytes and version ids live in S3. Agents write the catalog only through SECURITY DEFINER functions, which name their caller by `session_user`.

### Architecture
```
agent tool (toolkit helper, psycopg, its own login role)
   └─ SELECT catalog.claim(recipe) ─► claimed │ in_progress │ reuse │ failed │ nondeterministic
      PUT to the recipe-named key (If-None-Match:*), then
      SELECT catalog.commit(hash, objects, toolkit_commit, env_fingerprint)   or   catalog.fail(hash, reason[, true])
runtime reconciler (login in `reconciler`), through catalog.py
   └─ verify(hash, outcome, reason) · adopt_orphan(...) · move_current(bucket, kind, subject, hash, expected_seq, reason)
human (bootstrap superuser, or a login in `owner`): owns the schema; rolls back with move_current
```
- **Roles** are cluster-wide NOLOGIN groups:
  - `owner` owns the schema, every table and every function;
  - `reader` has SELECT on every table;
  - `producer` = reader + EXECUTE on claim / commit / fail;
  - `reconciler` = reader + EXECUTE on verify / adopt_orphan / move_current / flag / record_check / stamp_flags.

  No class has INSERT, UPDATE or DELETE on a table. EXECUTE is revoked from PUBLIC on every catalog function, and on any function `owner` creates later (default privileges).
- **One catalog per cluster.** The roles are cluster-wide, so `check_schema` treats a login holding a class with no live credential in this catalog as a fault.
- **Product state machine** (`product.state`):

  | From | Event | To |
  |---|---|---|
  | — | claim | `claimed` (lease, wall clock) |
  | `claimed` with a live lease | the same owner claims again | `claimed` (renew: same generation and claimed_at) |
  | `claimed` with an expired lease | the same owner claims again | `claimed` (restart; generation + 1) |
  | `claimed` with an expired lease | another agent claims | `claimed` (takeover; generation + 1) |
  | `claimed` | its owner commits | `committed` |
  | `claimed` | its owner fails it | `failed` or `nondeterministic` |
  | `failed` with no objects | claim | `claimed` (retry; generation + 1) |
  | `committed` or `verified` | the reconciler's verify | `verified`, `failed` or `nondeterministic` |
  | `claimed` with an expired lease | adopt_orphan | `committed` |

  - **Terminal states:** `failed` with objects and `nondeterministic`. Their objects hold the recipe's create-once keys. Claim answers with the state and its reason, and the human decides.
  - **A commit after the lease expired** is accepted as long as no other agent took the claim over.
  - **A producer's own `fail` is final** (ruling 11-10). Nothing it stored is adopted over it; a later claim retries in a new generation.
  - **Generation** (`product.generation`, ruling 11-10). It names the production: 1 for the first claim, and + 1 for each takeover, retry, or restart by its owner after expiry; renewing a live lease keeps it and `claimed_at`. `claim` answers it, and the helper (S8) writes the production into `<short hash>.<n>/` (n omitted for 1). A new production therefore never meets an abandoned one's create-once keys; the abandoned folder stays and the reconciler flags it.
- **"Current"** is keyed by (bucket, kind, subject): two buckets may use one subject string for different things. It always points at a verified product of its bucket, or at nothing.
  - A product's `bucket` is the one bucket its objects lie in, set when they are recorded (commit, adopt_orphan). Objects naming two buckets (or two stores) are refused (22023): one bucket of one store per product.
  - `move_current` reads the product `FOR SHARE` and compares-and-swaps on `seq`. `expected_seq` = 0 means "no pointer yet". On a mismatch it moves nothing and returns the winner.
  - When `verify` demotes the current product, the pointer moves in the same transaction back to the latest product in its history that is still verified, or to NULL. Each such move is a history row and an event.
  - The human rolls back with the same call.
- **Product identity** is `catalog.recipe_hash(recipe)`, the sha256 of the recipe's jsonb text. jsonb fixes key order and spacing; it does not equate `2` with `2.0`, so the helper normalises numbers.
  - A recipe holds exactly kind, subject, tool, algorithm_version, settings and inputs.
  - Each input is `{store, bucket, key, version_id}`, kept in the tool's order.
  - The toolkit commit and the environment fingerprint are provenance, given at commit. They are never part of identity.
- **Append-only:** object, current_history, event, object_check and product_flag refuse UPDATE, DELETE and TRUNCATE by trigger, for every role.
- **Locks:**
  - claim, commit and fail lock the product `FOR UPDATE`.
  - verify locks it `FOR NO KEY UPDATE`, which blocks `move_current`'s `FOR SHARE` read of the same product. It does not block a history row's foreign-key check, so the two cannot deadlock.
  - A claim inside an open transaction holds the row until that transaction ends. Callers use autocommit.

### Interfaces / Contracts
**SQL: the producer.** Callable by members of `producer`; the toolkit's helper (S8) calls these.

| Function | Returns, and refusals |
|---|---|
| `catalog.claim(recipe jsonb, lease interval DEFAULT '1 hour') → jsonb` | `{status, recipe_hash, …}`, with status one of: `claimed` (owner, lease_until, generation); `in_progress` (owner, lease_until, generation); `reuse` (state, owner, objects[]); `failed` (reason); `nondeterministic` (reason). Refuses a malformed recipe, or a lease outside (0, 1 day], with SQLSTATE 22023. |
| `catalog.commit(hash, objects jsonb, toolkit_commit, env_fingerprint) → jsonb` | `{status: committed, recipe_hash, objects}`. `objects` is a non-empty list of `{store, bucket, key, version_id, sha256 (64 lower hex), size (integer bytes)}`. Refusals: no claim, or not claimed (22023); the claim is held by another agent (42501); empty provenance or a bad object (22023); a version already recorded (23505). |
| `catalog.fail(hash, reason, nondeterministic boolean DEFAULT false) → jsonb` | `{status, recipe_hash}`. The caller must hold the claim (42501 otherwise), and a reason is required (22023). |
| `catalog.recipe_hash(recipe jsonb) → text` | Callable by `reader`. |

**SQL: the reconciler.** Callable by `reconciler` (and the owner); wrapped in Python below.

| Function | Returns, and refusals |
|---|---|
| `catalog.verify(hash, outcome DEFAULT 'verified', reason DEFAULT NULL) → {status, recipe_hash}` | Only for a product that is committed or verified. Outcomes other than `verified` need a reason. |
| `catalog.adopt_orphan(hash, objects, toolkit_commit, env_fingerprint, producer, reason, generation) → {status: committed, …}` | Only for an expired claim (a producer's `fail` is final), at that generation (checked under the row lock). `producer` must equal the claim's owner. |
| `catalog.move_current(bucket, kind, subject, hash, expected_seq, reason) → {status: moved \| mismatch, current: row \| null}` | Only for a verified product of that (bucket, kind, subject). A reason is required. Onto a product carrying a live flag (`live_flag`) only the human's own login moves it (`_as_human`); the reconciler and the runtime's superuser are refused (22023). `moved_by` is the authenticated login. |
| `catalog.flag(action, hash, detail jsonb) → boolean` | Logs the reconciler's finding as event `flag.<action>` (S6). Idempotent: an identical flag (action, hash, detail) already logged is not logged again, and the call returns false. Refuses an action that is not lower-case words joined by dots, or a detail that is not an object (22023). |
| `catalog.record_check(store, bucket, key, version_id, size, mtime, sha256) → void` | Records that the reconciler hashed this version at this size and LastModified (`object_check`, S6). |
| `catalog.stamp_flags(hash, flags text[], receipt) → integer` | Stamps a call receipt's flags (`failed`, `synthetic`, `undeclared_write`, `unverified`) on a product that call made (`product_flag`, S5): locks the product `FOR UPDATE`, one row per new (product, flag, receipt) and one `stamp_flags` event; a current product is demoted (`_demote`, as `verify` does). Returns how many were new. Refuses an unknown product, an empty or unknown flag list, an empty receipt (22023). |
| `catalog.clear_flag(hash, flag, reason) → {flag, receipts}` | The human's own login only (`_as_human`: the login that authenticated, `system_user`, is a non-superuser member of owner and is the session user, so `SET SESSION AUTHORIZATION` by the runtime's superuser is refused; no class holds EXECUTE, so the reconciler, producer and reader logins are refused too; 42501); the clear row records the authenticated login: appends a `cleared` row with the reason and a `clear_flag` event naming every receipt whose stamp it covers (each stamp since the last clear). Refuses a blank reason, an unknown product, and a flag not live on the product (22023). Leaves the pointer alone: the next pass may promote the product. |

**Tables** (SELECT for `reader`):
- `product` (… `generation`)
- `object` (store, bucket, key, version_id, sha256, size, recipe_hash, created_by, created_at)
- `current`
- `current_history` (both with `human_override`: a move by the human's own login, never a demotion)
- `credential` (kind `s3` | `postgres`, key_id, agent, run, issued_at, revoked_at; at most one live row per key)
- `event` (at, actor = session_user, action, recipe_hash, detail; indexed on (action, recipe_hash))
- `object_check` (store, bucket, key, version_id, size, mtime, sha256, checked_by, checked_at): the reconciler's hashes; the latest row per version is the baseline of the cheap check
- `product_flag` (recipe_hash, flag, action `stamp` | `cleared`, receipt `<run>/<handle>` (a stamp), reason (a clear), stamped_by, stamped_at; unique per (product, flag, receipt)): the history of the provenance marks a product carries from the calls that made it (S5) and the human's clears
- view `live_flag` (recipe_hash, flag, receipt, stamped_by, stamped_at): per (product, flag), the latest row when it is a stamp: the flags that count, for readers and for promotion
- `schema_version`

**Python** (`auto_research.store.catalog`). Every connection argument is a `psycopg.AsyncConnection` in autocommit mode.
- `apply_schema(dsn)` installs the catalog, or confirms it is at `SCHEMA_VERSION`. The dsn is a superuser connection.
- `check_schema(dsn)` is the preflight check:
  - the schema is at `SCHEMA_VERSION`;
  - the four roles exist, are NOLOGIN and are not superusers;
  - every login in reader, producer or reconciler has a live postgres credential;
  - the human's login `HUMAN_ROLE` exists as a LOGIN, INHERIT member of owner and of no class, with no
    superuser, createrole, createdb, replication or bypassrls attribute (`apply_schema` creates it,
    without a password, at the store's first start, and on an existing catalog that lacks it);
  - no other login is in owner.
- `set_human_password(conn, password)` (superuser connection; a SCRAM verifier hashed client-side).

  It raises `CatalogError`.
- The key adapter (S4) uses a superuser connection:
  - `issue_role(conn, *, name, role_class, run, password, valid_until)`: the role is LOGIN, in that class, with a SCRAM verifier hashed by libpq and VALID UNTIL. A role issued in an earlier run is re-enabled, and its settings and other classes are reset. A role this catalog never issued is refused.
  - `end_role(conn, *, name)`: commits NOLOGIN and the revoke, then terminates the role's sessions, and raises if any remain.
  - `record_credential(conn, *, key_id, agent, run)` and `revoke_credential(conn, *, key_id)`: S3 access keys.
- The reconciler (S6), as a reconciler login: `verify`, `adopt_orphan`, `move_current`; it calls `catalog.flag` itself.
- Reads: `product`, `products(state)`, `objects(recipe_hash)`, `current(bucket, kind, subject)`, `current_history(bucket, kind, subject)`; `move_current(conn, bucket, kind, subject, recipe_hash, expected_seq, reason)`.

### Dependencies
- `psycopg` 3 (with `psycopg.sql`, `rows.dict_row`, `types.json.Jsonb`).
- Postgres 17 from conda-forge, started by `store/launch.py` (S1).
- `catalog.sql` is read beside the module. A non-editable install needs it declared as package data (`[tool.setuptools.package-data] auto_research = ["store/*.sql"]`).
- Used by: S1 (`check_schema` in preflight, `apply_schema` at first start), S4 (roles and credentials), S6 (reconciler), S8 (the SQL functions, from the toolkit helper).

### Dictionary-level reference
| Symbol | Meaning |
|---|---|
| `SCHEMA_VERSION` | The version this code installs and reads (1). |
| `SCHEMA_SQL` | Path of `catalog.sql`. |
| `ROLE_CLASSES` | The classes a login may join: `("reader", "producer", "reconciler")`. |
| `CATALOG_ROLES` | `ROLE_CLASSES` plus `owner`. |
| `CatalogError` | The catalog is missing, at another version, its roles are wrong, or a role request was refused. |
| `apply_schema`, `check_schema` | See Interfaces. |
| `issue_role`, `end_role`, `record_credential`, `revoke_credential` | See Interfaces. |
| `verify`, `adopt_orphan`, `move_current` | Each runs one SQL call in its own transaction and returns the function's jsonb as a dict. |
| `product`, `products`, `objects`, `current`, `current_history` | Rows as dicts, read through `dict_row`. |

### Residuals (accepted limits)
- **Committed keys are not tied to the recipe.** Nothing in the catalog checks that an object's key lies under the recipe's `<kind>/<subject>/<short hash>/` folder. The producer policy allows writes on every key of the store's buckets, and create-only is the toolkit helper's `If-None-Match:*`, not the policy's (ruling option A); the helper, the CI pipeline's `layer_b.returns.store` and the reconciler bound this.
- **Input versions are not checked against recorded objects.** An input naming a version that no object row holds, `"latest"` for instance, is accepted. The helper always resolves a version id.
- **A Postgres login lives at most `keys.ROLE_VALID_FOR` (7 days, VALID UNTIL; accepted, coordinator
  ruling 6).** A run longer than that loses new catalog sessions (a REPL restart included); its open
  sessions go on. The run's end revokes earlier.
- **A dead run's Postgres session that outlives `end_role`'s 5 s terminate is not revisited**: the login
  is NOLOGIN and its credential row revoked first, so the sweep no longer lists it (the jail's
  `--die-with-parent` normally ends the tool processes with their runtime).
- **Keys of a run with no liveness record here are never swept**: a run of another host or runtime dir,
  a deleted ledger, the reconciler CLI's `cli-<pid>` logins (S6; those expire within the hour). S3 keys
  have no expiry; `status` lists them and `revoke --run` ends them.
- **A sweep and a resume of the same run can overlap**: liveness is checked again before each
  principal is revoked, and a resume records its process before it issues anything, so the window is
  the few milliseconds of one principal's revoke.
- **VALID UNTIL binds only password authentication.** Under trust or peer authentication, `end_role` is the real expiry.
- **An agent can change its own role's password and settings.** These are reset at the next issue.
- **`fail(..., nondeterministic := true)` is terminal for its recipe.** Only the owner reverses it.

## Catalog query (item S9)
`query.py`: a unit's read-only query of a store's catalog, run by the runtime as the unit's own
reader-class login (`keys.issue(..., "reader")`; the lead's `catalog_query` tool,
`agents.react.execute_catalog_query`). Domain-neutral: kind, subject, bucket and key are the
toolkit's strings.
```
check(arguments) ─► {view, store?, bucket?, kind?, subject?, state?, recipe_hash?, limit}   (QueryError: what to write)
run(login.dsn(), asked, store=name)   one session: default_transaction_read_only, statement_timeout 10 s
  products  catalog.product: tool, algorithm version, settings, input count, state, flags
            (product_flag), current mark, object count; newest claim first
  current   catalog.current per (bucket, kind, subject)
  objects   one product (recipe hash or a prefix of >= 8 hex, unique), in full with its recipe inputs,
            flags and current mark → its object rows
→ {store, view, matched, shown, rows[, product]}   times as ISO 8601 strings
```
- `VIEWS = ("products", "current", "objects")`; `STATES` (the product states); `ARGUMENTS`;
  `DEFAULT_LIMIT = 50`, `MAX_LIMIT = 200`.
- `check(arguments) -> dict` — the normalised query; raises `QueryError` for an unknown argument, a
  view or state outside its list, an empty string, a recipe hash that is not 8–64 hex digits (any
  case, lowered), a limit outside 1–200 (null is the default), `objects` without a recipe hash or with
  another filter, `current` with a state or hash filter. Bucket, kind and subject match exactly as written.
  A product has its bucket once its objects are recorded, so a bucket filter leaves out claimed products
  and failed ones without objects.
- `async query(conn, asked, *, store) -> dict`, `async run(dsn, asked, *, store) -> dict` — the answer;
  `objects` raises `QueryError` when no product, or more than one, starts with the hash given.
- An object row (`store, bucket, key, version_id`, plus `sha256`, `size`) is the record a `record`
  argument and a brief's `validation_data` (`schemas.ValidationDataset`) name.
- The answer is the receipt of a harness read (producer `catalog_query`): navigation, never evidence.
- Residuals: a product's flags are its own live ones (`catalog.live_flag`); its ancestors' flags reach a
  result through the receipts of the calls that read it. The lead's `reader` issue makes an S3 user
  and access key it never uses (`keys.issue` makes both; the runtime uses the Postgres login only);
  revoked with the run's keys. Only the default store (`DEFAULT_STORE`) is issued; a configuration
  without a store named `db` is refused by `store_config` (ruling 6: never a silent absence). The answer is bounded by rows (at
  most 200), not by characters; a product's settings are shown whole.

## Records (receipts from the store's records, item S5)
`records.py`: what one unit's toolkit call read and wrote in a store, from the store's own records,
never from the tool (plan `PLAN_0b_formal.md` S5; research `s3/write_events/SYNTHESIS.md`).
```
StoreRecords(agent=<principal>, run, writable=writable_area([...]))   one per unit (graph.runtime, S4)
  bracket(declared, given):  [unit lock: one call of a unit at a time]
    before  ListObjectVersions under each writable Area (admin client) · access-log size per store
    ...the call runs...
    after   ListObjectVersions again → new versions / delete markers / removed versions
            access log since the offset → lines by the unit's keys (catalog.credential: agent, run)
              GetObject ?versionId=…  → a pinned read          PutObject/Copy/CompleteMPU/Delete(s) →
                                                                which listed changes are this call's
            every new version and every read hashed by the runtime (GET by version id, SHA-256)
  → CallIO(reads: [InputFile basis object], writes: [StoreObject], flags, notes)
```
- **Attribution.** With an access log, a new version is the call's only when a successful
  PUT-like line (Put, Copy, CompleteMultipartUpload) by one of the unit's keys names its key; a delete
  marker or a removed version only when a `DeleteObject` line names it, or a `DeleteObjects` line
  (which names no key) is in its bucket: a shared area holds other units' writes too. When another
  key also wrote that key in the window, or the key gained more versions than the unit's lines, the
  versions are not attributed and the call is `unverified`. A line with no readable status counts
  as succeeded; a log line that is neither a request nor the gateway's `log starts` header (a key
  holding a newline splits its line) marks the call `unverified`. A store whose read record is
  `none` (remote) gives every listed change to the call, and its reads are the call's pinned
  `record` arguments. The unit's keys come from the catalog on every call with log lines; a failed
  query falls back to the keys an earlier query found; none known marks the call `unverified`.
- **Flags.** A write in a store the tool's manifest does not name in `writes` (`declared` False), a
  removed version, or a mutating log line the listing does not show → `undeclared_write`. A listing,
  log, catalog or hash that fails, a read naming no version, or bytes whose store SHA-256 disagrees →
  `unverified`. A read in a `synthetic_buckets` bucket → `synthetic`. A write over the key's latest
  version names it (`StoreObject.overwrote`).
- **Hashes** are the runtime's, cached per process by (store, bucket, key, version id, size, last
  modified): an edit in the store's folder keeps the version id and changes size or time (probe P9),
  so it is hashed again; the client validates the store's checksum on the GET, and a stored
  full-object SHA-256 is compared with ours.
- **Flags across runs** (coordinator rulings 11-10). After a call's receipt is minted, the flags the
  call earned itself (`CallReceipts.own_flags`: `failed`, its own store records' `undeclared_write`,
  `unverified` and direct `synthetic`) are stamped on every product the call made; a flag it carried
  (from its reads, `CallIO.carried`, or its parents) is stamped only where the product's recipe inputs
  do not yield it through ancestry (`_derivable`: a read that is no recipe input, a parent outside the
  catalog), so a flag derivable upstream stays derived and a clear there reaches every descendant. They
  go (`stamp_flags` → `catalog.stamp_flags`,
  receipt `<run>/<handle>`): those recording its writes (catalog `object` rows) and every product the
  unit's catalog login claimed, committed or failed in the bracket's window (`catalog.event`), so a
  product stored but never committed (adopted later) and writes the bracket could not attribute are
  stamped too. A stamp that fails is retried with the next call's. Stamped through the
  runtime's reconciler login (`reconciler=`, `graph.runtime.run_reconciler`; the admin role when none,
  a test or the CLI). Every read looks its version up (`_lineage_flags`: catalog `object` → `product`,
  then each recipe input's producing product, transitively) and carries onto the reading receipt the
  flags stamped on any of them, `synthetic` when any of them wrote or read in a synthetic bucket, and
  `unverified` when one is `failed` or `nondeterministic` (the reconciler's check failed it). A lookup
  that fails marks the read `unverified`. The unit's keys are read through the same login.
- **Before a call.** `current_version` pins a record without a version (`provenance.inputs.CurrentVersion`);
  `version_problem` refuses a named version the store does not hold (HEAD; ruling d);
  `store_location_problem` refuses a `path` argument inside a local store's root, state or socket
  folder (ruling a). A store that cannot be listed before a call refuses it (`StoreRecordError`);
  after the call nothing raises: what is lost marks the record `unverified`.
- **Log parsing.** The gateway writes AWS's server-access-log format with the key unescaped (spaces
  allowed) and the request URI escaped: `parse_access_line` reads the object from the URI and accepts
  it only where the key field says the same; the window starts at a line boundary and ends at the last
  complete line (a line being written is another request's).
- **Residuals.** A process that outlives its call writes into the next call's window (the reconciler's
  sweep is the check); listing cost grows with the writable area (whole buckets for a producer); a
  rotated log during a call marks it `unverified`; the hash cache is per process, so each run hashes
  a version it reads again once; an edit in the store's folder that keeps both size and the
  one-second modification time hits the cache (probe P9's edits change both); the summary line names
  at most 12 versions (the receipt names all); a stamp still failing when the run ends is lost (its
  note was shown); a store without a read record gives every listed change to the call, so a flagged
  call stamps a concurrent product of another unit too (over-flagging; only local stores today); a
  wrong stamp (a call that committed a good product and failed after it) stays until the human clears
  it (`clear-flag`); a clear is not undone by a retry of the same receipt's stamp (one row per (product,
  flag, receipt)), only by a stamp from another receipt; a clear covers every receipt's stamp of that
  flag (the output and the event name them); a clear of `unverified` or `synthetic` does not reach
  readers that derive the flag from a failed state or a synthetic bucket (said in the output); receipts
  already minted keep what they carried, so within the run that minted them a descendant linked in-run
  still carries a flag cleared since; a flag carried from a non-catalog parent (a `$ref`) is stamped
  on the product, so a clear upstream does not reach it; the runtime's admin role is the cluster's superuser and could
  still act as the human (it can set the human's password and then log in as it): the human login and
  the authenticated-login checks separate the record from the runtime's own acts, not the power.

## Reconciler
`reconcile.py` (item S6): the runtime's pass that keeps a store's catalog true to its bytes.

### Purpose
- It verifies what producers committed.
- It adopts what they stored but never committed.
- It finds objects that changed or vanished outside the gateway (the human ruling on out-of-band edits).
- It moves each (bucket, kind, subject)'s `current` pointer forward automatically (the human ruling on "current").

The pass knows no layout. Buckets, kinds, subjects and keys are opaque strings the toolkit chooses.

### Architecture
```
reconcile(store, run=…, conn=reconciler login)            the runtime: each run's launch step (S4); the CLI
  1 sweep    ListObjectVersions (admin key) − object rows → HEAD each → x-amz-meta-recipe-hash
               no product / no metadata           → flag orphan
               live claim                         → pending
               expired claim                      → adopt_orphan the objects a complete recipe.json lists (+ it)
               failed (the producer's fail: final) → flag
               committed/verified, recorded key    → other bytes: verify(nondeterministic)   same bytes: flag
  2 verify   committed: runtime GET + SHA-256 of every object (recorded in object_check), then
               metadata · the store's full-object SHA-256 (HEAD, ChecksumMode) as a second check
             verified: the cheap check (listed · size · size+LastModified of the last object_check;
               a disagreement is hashed again and recorded)
               → verify(verified | failed, reason); a failed current product moves its pointer back (catalog)
  3 promote  per (bucket, kind, subject), in the declared buckets: newest qualifying (claimed_at) → move_current(CAS on seq), forward only
check_store_integrity()   preflight store_integrity: the cheap check only, admin credentials, no writes
```
- **Login.** The runtime's launch step passes its own `reconciler` login (S4 `keys.issue(store, "reconciler", "reconciler")`).
  - Without one, `reconciler_session` issues `ar_reconciler_<run>` through `catalog.issue_role` and ends it with `end_role`.
  - Sessions of one login are serialised within a process.
  - The S3 side signs with the reconciler's own keys when the runtime passes them (`s3=`, S4's `keys.issue(store, "reconciler", "reconciler")`, a reader-class policy: list and pinned `GetObjectVersion`; least privilege, main's ruling). The admin key signs only for the CLI and the preflight item. boto3's checksum handling is off: the pass compares and hashes the bytes itself.
- **Snapshot discipline.** The DB clock is read before the listing.
  - A row missing from the listing but written after the listing began is skipped. It is never judged "missing", so a producer committing during the pass is left for the next pass.
  - In the sweep, a committed product's rows are read again before a version is called an orphan.
  - A verdict refused because the product changed meanwhile (another reconciler, the human) is flagged as `verdict_refused`; it is never raised.
- **Every product is hashed once by the runtime** (ruling 11-10), at its first verify, so an edit before that verify is found whatever its size and mtime. The hash, with the size and LastModified listed, is recorded in `object_check`.
- **Edits on disk after that.** An edit outside the gateway keeps its version id and stored checksum, but changes the size and LastModified (probe P9).
  - The cheap check compares the listing with the last `object_check` row of the version. Any difference is hashed again and recorded, so a touched file with the same bytes is hashed once. A second version at a recorded key is judged the same way (hashed once).
  - A size or hash that disagrees with the row is `edited` when the version changed after its row was written or after its last hash, and `mismatch` otherwise (the producer's row named other bytes).
- **Adoption needs the completeness marker** (ruling 11-10). The helper writes `recipe.json` (`RECIPE_FILE`) last in a production's folder (`RecipeFile`: recipe_hash, generation, recipe, objects `[{key, version_id, sha256, size}]` in the file's own bucket). An expired claim is adopted only when:
  - one of its unrecorded versions is such a file for this recipe, at the claim's generation (checked again by `adopt_orphan` under the row lock), by the claim's owner, carrying `toolkit-commit`, `env-fingerprint` and the sha256 of its own bytes;
  - every object the file lists is among the unrecorded versions, with the listed size and sha256 and the same provenance.

  The listed objects and the file itself are adopted. Every other version of the recipe is flagged, including an abandoned generation's folder. A refused adoption is flagged only when the product did not get committed meanwhile (two reconcilers, or the producer's own late commit).
- **Qualifying for "current"** (ruling 11-10, `_Qualifier`). The product must:
  - be verified;
  - have its recipe's tool be a manifest key of a declared toolkit (`toolkit_contract.toolkits()`, read as data by `ToolkitRegistry`);
  - have its `toolkit_commit` be reachable from that toolkit's authoring branch (a missing branch is flagged `unqualified` once per pass) (`git merge-base --is-ancestor <commit> refs/heads/<authoring_branch>`, run by application code as `merge_flow` does). The runtime never looks at the human's trusted branch (ruling 11-10);
  - not be synthetic: no object of it, and no recipe input of it or of any ancestor product, lies in a store's `synthetic_buckets`;
  - carry none of `BLOCKING_FLAGS` (every receipt flag) live (`live_flag`: stamped, not cleared), itself or any ancestor product (the flags the runtime stamped from the calls that made them, S5), and have no ancestor `failed` or `nondeterministic` (`_Qualifier.flagged`). A stamp landing after the qualifier read the flags is caught by `move_current`'s refusal; one landing after the promotion demotes the pointer (`stamp_flags`).

  "Failed" is excluded because only verified products are candidates.
- **Promotion rules** (approved by main, 11-10):
  - "Newest" is the product's `claimed_at`, the start of its production. It stays stable under adoption, unlike `committed_at`.
  - A pointer the human's own login (`catalog.HUMAN_ROLE`, `ar_human`) moved with `move_current` is a human override: `current.human_override`, set from `catalog._as_human()` (the authenticated login, never an impersonation). It holds until a product claimed after that move qualifies. A move by the admin role or any runtime login, and every demotion (`_demote`, whoever's session ran it), is the runtime's and holds nothing (main's ruling 11-10).
  - The pointer only moves forward: candidates are claimed after the current product (and after a human's move). A current product newer than every qualifying one stays, even when this run's toolkits do not qualify it, so runs with different toolkits never flip pointers back; the query is bounded by it.
  - On a CAS mismatch, or a refusal because the chosen product was demoted meanwhile, the pass re-reads the pointer and decides again, at most `PROMOTE_ATTEMPTS` (3) times; then it flags `promotion_contended`.
  - Every move is logged by `move_current` with its reason.
- **Flags** are the event log's `flag.<action>` rows (`catalog.flag`, idempotent) plus `Report.flags`. The actions are:
  - `orphan`;
  - `nondeterministic`;
  - `missing`;
  - `edited`;
  - `mismatch`;
  - `unreachable` (a HEAD or GET that failed: no verdict);
  - `unchecked` (a row in a bucket the store does not declare: no verdict);
  - `adopt_refused`;
  - `verdict_refused`;
  - `promotion_contended`;
  - `unqualified` (a toolkit's authoring branch is missing: nothing it made can become current).

### Interfaces / Contracts
- `async reconcile(store, *, run, conn=None, s3=None) -> Report`. One pass, as described above.
  - `conn` is an autocommit connection as a `reconciler` login.
  - `s3` is the reconciler's own store keys (an object with `access_key_id` and `secret_access_key`, S4's `keys.Credentials`); without it the admin key signs.
  - Raises when a bucket cannot be listed: nothing is concluded from a partial listing.
  - S4 calls it at launch, before preflight (coordinator ruling Q2).
- `reconciler_session(store, *, run)` is an async context manager that yields that connection, issued and then ended.
- `async integrity_problems(store) -> list[str]` and `async check_store_integrity()`: the preflight item `store_integrity`, registered after `store_catalog`.
  - Read-only, with the admin S3 key and the admin catalog role.
  - It covers the rows of committed and verified products in declared buckets.
  - It raises one `RuntimeError` naming each object, and says that a reconcile pass marks the product failed.
- `main(argv)`: `python -m auto_research.store.reconcile [store …]` runs one pass per store and prints the report as JSON (for the human).
- The immutable object metadata the toolkit's helper (S8) writes on every object (`x-amz-meta-*`):
  - `recipe-hash`;
  - `sha256` (64 lower-case hex);
  - `producer` (the agent's catalog login, the claim's owner);
  - `toolkit-commit`;
  - `env-fingerprint`.

  Adoption needs all five, and verify compares the first two when present.
- `RECIPE_FILE = "recipe.json"`, `RecipeFile`, `RecipeObject` (pydantic, `extra="forbid"`): the completeness marker's schema, which S8's helper mirrors (the toolkit imports nothing from `auto_research`):
  ```json
  {"recipe_hash": "<64 hex>", "generation": 1, "recipe": {"kind": "…", "subject": "…", "tool": "…",
   "algorithm_version": "…", "settings": {}, "inputs": []},
   "objects": [{"key": "<kind>/<subject>/<short hash>[.<n>]/out.dat", "version_id": "…", "sha256": "<64 hex>", "size": 123}]}
  ```
  Write it last, with the same five metadata entries as every object; commit it with the others. One bucket per product: the listed objects lie in the recipe file's bucket. Single PUTs carry `ChecksumAlgorithm="SHA256"`, so the store's own checksum is available for the second check (a CRC32 or multipart object has none, and is checked by the runtime's hash alone).
- **The toolkit's side (item S8).** The configured toolkit's helper is `storelib` (a support package of
  research_toolkit, outside its package glob): `produce` (claim, create-only uploads with the five
  entries, `recipe.json` last, commit or fail), `stage_in` (pinned GETs, a `recipe.json` record staging
  its whole product), `resolve`, `remote`/`open_remote`. It mirrors `META_*`, `RECIPE_FILE` and the
  `RecipeFile`/`RecipeObject` fields in `storelib/contract.py`; `tests/test_toolkit_store.py` checks the
  two are equal (read from the toolkit's source as data, never imported) and runs the toolkit's store
  tests against `ci.throwaway`, then reconciles what they stored. A product's record, as tools pass it,
  is the record of its `recipe.json`.

### Dictionary-level reference
| Symbol | Meaning |
|---|---|
| `META_RECIPE_HASH`, `META_SHA256`, `META_PRODUCER`, `META_TOOLKIT_COMMIT`, `META_ENV_FINGERPRINT` | The metadata names above, without the `x-amz-meta-` prefix (boto3's `Metadata` keys). |
| `PROMOTE_ATTEMPTS` | 3: decisions per (bucket, kind, subject) before `promotion_contended`. |
| `RECIPE_FILE`, `RECIPE_FILE_MAX` | `"recipe.json"`; 16 MiB, the largest recipe file read. |
| `RecipeFile`, `RecipeObject` | The recipe file's schema (above). |
| `SESSION_LIFETIME` | 1 h: how long a self-issued reconciler login may open new sessions. |
| `Finding(action, recipe_hash, detail, logged)` | One flag; `logged` is whether this pass wrote it to the event log first. |
| `Report(store, verified, failed, nondeterministic, adopted, pending, promoted, flags)` | One pass. `failed` and `nondeterministic` map hash → reason. `promoted` holds `{kind, subject, recipe_hash, previous, seq, reason}`. `.as_dict()` gives the form the runtime records. |
| `reconcile`, `reconciler_session`, `integrity_problems`, `check_store_integrity`, `main` | See Interfaces. |

### Residuals (accepted limits)
- **An edit after the first verify that keeps both the size and the LastModified recorded at the last hash is not seen.** LastModified has 1 s precision, so this means an edit in the same second as the version's last write, or one that restores the old mtime (`cp -p`, `rsync -t`). A `GET` through boto3's checksum validation (the toolkit helper's read) still refuses it.
- **Every unrecorded version is read back (HEAD) at every pass.** Abandoned folders and stray uploads stay, so this grows with them; their flags are written once.
- **A producer still uploading after its lease expired** has its versions flagged as orphans ("no recipe.json") until it commits; the flags stay as history.
- **Synthetic ancestry is followed in this store's catalog only.** An input stored in another store, with its own catalog, is synthetic only when its own bucket is a synthetic one.
- **A toolkit commit reachable from the authoring branch is not thereby reviewed.** Merge to the trusted branch stays the human's.
- **Flags are never cleared.** A finding resolved later stays in the event log as history.

## CI (the CI pipeline's throwaway store, item S7)
`ci.py`: a fresh local store for one CI test run, so the gate's network-denied test run judges a store
tool without ever reaching the real store (the hermetic rule: a CI run sees the declared data sets and
its throwaway store, nothing else).
```
gate process (host)                                    the CI test run (bwrap --unshare-net)
 throwaway(mirror)                                      sees the jail folder only (bound read-only)
   launch.start: versitygw IAM + gateway + Postgres, 127.0.0.1, temporary folders, fresh admin secrets
   buckets: the mirror's, versioning on; the catalog schema applied (launch)
   keys.issue: ci_producer (producer), ci_reader (reader)
   copy in: the brief's validation records, from the real store (admin key), before the run
   jail folder/ (/var/tmp/ar-ci-<uid>/<id>-j; the store's own socket_dir, <id>-s, with its IAM socket, stays out)
     s3.sock          host relay ──► gateway 127.0.0.1:<s3>   ◄── relay 127.0.0.1:<s3> in the jail (plugin ar_store)
     .s.PGSQL.<port>  a hard link to Postgres's socket          ◄── PGHOST=<jail folder>
     calls.sock       StoreRecords.bracket per watched call   ◄── the gate's return recorder (begin, end)
     seed.sock        a data set's file uploaded as a product  ◄── TOOLKIT_STORE_SEED (a toolkit test)
```
- **When.** The gate's runner starts one per pytest run (run 1, run 2, each regression run; never the
  live collection) whose packages include one declaring a store tool (`store_tools`: a manifest
  `writes` or `reads`, or a `record` argument; ruling C: a regression run of a package without one gets none),
  when the configuration declares a store (`mirror_of`: the default store, else the first). Nothing
  carries over between runs, so the determinism compare sees two fresh stores.
- **What the test run gets** (the toolkit's tools read the same names in a real run): the producer's
  `keys.ENV_NAMES` (`PGHOST` is the jail folder: the jail has no network), `TOOLKIT_STORE` (the
  store's name, the one records give), the reader's `TOOLKIT_STORE_READER_{AWS_ACCESS_KEY_ID,
  AWS_SECRET_ACCESS_KEY, PGUSER, PGPASSWORD}`, `TOOLKIT_STORE_SEED`, and, when the brief names
  validation data, `TOOLKIT_STORE_VALIDATION`. The servers' state (IAM,
  versions, access log, catalog cluster) and the admin secrets stay on the host; the admin secrets
  are held by the throwaway's own `AdminEnv` (`_HeldSecrets`), never in this process's environment,
  which an author harness or another jail could inherit.
- **The bracket.** The recorder brackets every call of every store tool of the tree (`ENV_TOOLS`:
  `{key: [module, fn]}`; an unchanged one only when a collected test imported its module), watched or
  not, and judges the watched ones. A store tool called while a bracket is open, from any thread,
  joins it (its writes are that call's). One connection per call: `{"call", "tool"}` opens
  `StoreRecords(agent=<producer>, run).bracket(declared=<the tool's writes>)` (answered once the store
  is listed); the next line closes it, and `CIStore.calls` gains what the store recorded: each version
  written (`StoreObject` fields), its metadata and, for a `recipe.json`, its text and
  `catalog.recipe_hash` of its recipe, the producer login, the call's pinned reads with the product
  each belongs to, and each written recipe hash's catalog row (state, owner, recipe inputs with their
  products; ruling A). `sandbox/returns.py` judges it (`layer_b.returns.store`).
- **The sweep** (ruling E). After each run `CIStore.sweep()` lists every version and delete marker in
  the store; one no bracketed call wrote and the pipeline did not seed or copy in (a test's own write,
  a tool's thread or process writing after its call returned) fails the run (`layer_b.returns.store`,
  "written outside a call").
- **Validation data** (main's ruling after S9). The brief's `validation_data` records (`{store, bucket,
  key, version_id}`) are copied from the real store with its admin key, outside every jail, into each
  run's store before the run: the same bucket and key, the same metadata, a new version id; a
  `recipe.json` record brings every object it lists, and is rewritten to name the new versions and
  committed and verified in the run's catalog. `TOOLKIT_STORE_VALIDATION` holds the records as the
  run's store holds them (a JSON list, in the brief's order). The bytes are fetched once per gate
  runner (a `cache` folder) and counted against `SEED_MAX_BYTES`. `validation_problems(mirror,
  records) -> (missing, unreachable)` is the gate's precheck: a record of another store, a bucket the
  store lacks, or an object version (or one its recipe file lists) the real store does not hold fails
  `layer_b.validation_data` before any test runs (as does a copy the brief causes to fail, too large or
  a recipe the run's catalog refuses: `ValidationRefused`); a real store that does not answer is a fault outside
  the change (`infrastructure.real_store`, ruling B). Records apply only to a run that has a store (the tree declares a store tool).
- **Seeding** (the one way a test stages store inputs, main's ruling 2). A test sends one JSON line,
  either `{"data": <data set>, "path": <file in its folder>, "bucket": <bucket>}` (answered
  `{"record": <the file's record>}`), `{"data", "paths": [<file>, ...], "bucket"}` (those files as one
  product, answered `{"record": <its recipe.json's record>, "objects": [...]}`), or `{"kind", "subject", "bucket", "files": {<name>: <base64
  bytes>}}` (synthetic bytes, stored as a product of that kind and subject, `settings` naming each
  file's sha256; answered `{"record": <its recipe.json's record>, "objects": [<each file's record>]}`),
  at most `SEED_LINE_MAX` (64 MiB); a refusal is `{"error"}` naming the action. Names are checked
  before anything is claimed (`_check_names`: plain path parts, no file called `recipe.json` or lying
  under another, keys within 1024 bytes; kind and subject one plain word); a store failure after the
  claim fails the claim, so the same request can be sent again. Seeded objects are recorded as the
  pipeline's (outside every call window: the sweep passes them). A synthetic seed's product is stamped
  `synthetic` in the run's catalog (`catalog.stamp_flags`, main's ruling A), so every read of it carries
  `synthetic` whatever its bucket; a data-set seed is not flagged. The file is uploaded with
  the admin key as a verified product of kind `test_data` (`SEED_KIND`), subject the data set, at
  `test_data/<set>/<short hash>/<path>` beside its `recipe.json` (create-only, the five metadata
  entries, committed and verified in the catalog); asked again in the same run, the same record. A data
  set the configuration does not provide (`test_data`), a path outside its folder, an unknown bucket,
  or more than `SEED_MAX_BYTES` (4 GiB) per run is refused with the reason.
- **Interfaces.** `throwaway(mirror, *, data_sets=None, writes=None, validation=(), cache=None,
  tools=None)` (context manager) → `CIStore` (`.env`, `.socket_dir` the jail folder, `.calls`, `.store`
  the `LocalStore`, `.records` its `StoreRecords`, `.sweep()`); `validation_problems(mirror, records)
  -> (missing, unreachable)`, `ENV_VALIDATION`, `ENV_TOOLS`; `CIStore.start()`, `.stop()` (idempotent:
  servers stopped, folders removed); `store_tools(root) -> StoreTools(packages, writes, entries)`; `mirror_of(config) -> store | None`;
  `PLUGIN_NAME`, `PLUGIN_SOURCE` (the in-jail relay, standard library); `ENV_STORE`, `ENV_SEED`,
  `READER_PREFIX`, `ENV_CALLS`, `ENV_S3_SOCKET`, `PRODUCER`, `READER`, `SEED_KIND`, `SEED_TOOL`,
  `SEED_MAX_BYTES`, `PARENT_ROOT`, `LIFE_LOCK`, `SWEEP_GRACE_S`; `ci_parent()`, `sweep_leftovers()`;
  `SeedRefused`.
- **Manifest store names** (main's ruling B). `undeclared_store_names(root, packages, declared)`: a
  `writes`/`reads` entry naming a store the configuration does not declare; refused by the gate's
  `structural.argument_kinds` (the touched packages) and by the launch item `store_config` (every declared
  toolkit's manifests, read as data; `health._toolkit_store_names`).
- **Life and leftovers** (S7 follow-up, after a store's folder vanished under a running full suite,
  2026-11-11: the cause stayed unconfirmed). Every throwaway store of a user lives in one fixed folder,
  `/var/tmp/ar-ci-<uid>/` (`ci_parent()`, mode 0700, the same whatever a process's `TMPDIR`): `<id>/` its
  work folder (root, state, its life lock `in-use`), `<id>-s` its socket folder, `<id>-j` the folder the
  jail binds. A store makes its work folder as `.new-…`, locks `in-use` (an exclusive `flock` the kernel
  lets go when its process ends, however it ends), and only then names it `<id>`, so a named folder's
  lock is always held while its store lives. `sweep_leftovers()` ends the servers (the processes
  holding a store's `server.lock`, also when its folder was removed; never a process that only names a
  store's path, nor the gate process starting a store, which holds its life lock too) of every store whose lock is free or whose work folder is gone, then removes those
  stores' folders, half-made `.new-…` folders whose lock is free, and socket folders with no work folder
  after `SWEEP_GRACE_S` (120 s). Every throwaway start runs it first, ending leftover servers only
  (`remove_folders=False`, main's ruling A: a removal can be gigabytes); **`python -m auto_research.store
  ci-sweep` is the one way to remove these folders**; the launch's preflight item `store_ci_leftovers`
  (`check_ci_leftovers`, a `PreflightWarning`, never a failure) names how many are left (`leftover_folders()`),
  their size and that command. Accepted residuals (main's rulings B, C): a start
  paused for more than 120 s before it takes its lock (Ctrl-Z, a debugger) loses its half-made folder
  and fails; a child forked without exec from a gate process would keep its dead store marked in use. A store's `stop` also
  ends its servers by the pids `launch.start` recorded (checked against each process's start time, so a
  reused pid is never signalled), so a store whose folder was removed under it leaves no server running.
  A start that `launch.start` refuses because a chosen port was taken meanwhile (below) is retried on
  fresh ports.
- **Residuals.** A nested manifest tool that only reads is not detected (ruling 3 is enforced through the
  products a call stores; main's ruling C, accepted). Ports are taken free and released before the servers bind them (three attempts on any start
  failure). A watched call that starts while another is bracketed waits for it (one bracket at a time
  per run; a watched call made from a second thread while the first waits for it stalls until the
  recorder's 600 s wait ends, then fails the check). A write a leftover thread or process makes after
  its call returned is caught by the sweep, unless a later call of the same run is bracketed when it
  lands (then it is that call's). Copied validation recipes keep the real store's version ids in their
  `inputs`. **One store per CI run** (ruling D, accepted residual): the configured default store, else
  the first; a tool writing a second store, or a validation record from one, cannot pass the CI
  pipeline.

## Migration and ingest (item S11)
The human's two ways to fill a store from data already on disk (human rulings 2026-11-11: the old tree
enters a fresh store through the gateway, non-destructively; archive downloads become products exactly as
the toolkit's own tool makes them; the human's derived files become products of the human's recipe).
Domain-neutral: the astronomy (which files, which tool) lives in the human's mapping file and job script,
outside `src/` (S11's are in `tmp_phase3/0b/migration/`).
```
start [--create-buckets]   launch.start (local, idempotent) · each declared bucket created, versioning Enabled
                           · health.ready_problems ("ready" or what the preflight would say)

ingest --mapping FILE      ingest.plan: each product's files (paths or globs below its folder), hashed
  (as ar_human: password      recipe {kind, subject, tool TOOL, "1", settings {<its own settings>, folder,
                                     files {path: sha256}}, inputs []}
   typed or --password-file) ingest.ingest_one: every file hashed again → catalog.claim (as ar_human)
                              → PUT each create-only (If-None-Match:*, ChecksumSHA256, the five metadata
                                entries; admin key signs) at <kind>/<subject>/<short hash>[.<n>]/<path>;
                                above 5 GiB in parts of 256 MiB, each part's SHA-256 checked by the store
                              → recipe.json last (reconcile.RecipeFile) → catalog.commit; any failure → catalog.fail
         --check           the plan only (no password, nothing written)

produce --unit U --env E [--readonly DIR ...] [--timeout S] -- COMMAND ...
  the toolkit on its authoring branch and clean (merge_flow.check_toolkit_branch_clean), else refused
  keys.issue(store, U, "producer", run cli-produce-<time>-<pid>)
  runner.run_in_environment(env E, COMMAND, store_env=its keys, readonly=DIRs): jailed one-shot, the
     toolkit commit (AR_TOOLKIT_COMMIT) and the environment fingerprint (AR_ENV_FINGERPRINT) beside the keys
  output printed (a timeout's partial output too, exit 124) · keys.revoke on every way out
```
- **An ingested product** is verified by the reconciler like any other and never made `current` by it (its
  tool, `ingest.TOOL`, is no manifest key); a tool reads it by its record (`data_catalog`, `catalog_query`
  list it); the human may point `current` at it (`move-current`). Its provenance fields (toolkit commit,
  environment) read `ingest.PROVENANCE`. The settings name every file with its SHA-256, so other bytes
  under the same names make another product; the same files again are answered `reused`. A product's own
  `settings` (text only: a YAML `1.0` or `yes` is refused, never coerced) are facts the human records about the files (S11: the versions a kept
  archive download carries) and are part of its identity; `folder` and `files` are the ingest's own names.
  Refused before anything is claimed: a bucket the store lacks, a pattern matching no regular file, a file
  outside its folder (a link included), a file named `recipe.json`, the same files listed twice, a setting
  named `folder` or `files`, a file changed since the plan. A file above `SINGLE_PUT_MAX` goes up in parts,
  each with its SHA-256, the whole file's SHA-256 compared before the upload completes (create-only); an
  interrupt stops it at the next part. The ingest's own S3 client (`client(store)`, the admin key) waits
  `READ_TIMEOUT_S` per request and never retries: the gateway assembles a large upload's parts during its
  completion (33 s for 9.7 GB on `/xray02`, measured), and a retried completion is answered without its
  version id; an answer without one, or a 412, is read back from the key (same bytes: that version).
- **A producer job** is how a run runs a toolkit tool, for a command the human names (S11: the migration
  driver running `heasarc_utils.acquire` with its download replaced by a copy of the old tree). Its keys
  live for the job: revoked at its end, on its timeout, an exception, and SIGTERM or SIGHUP to the CLI.
  A `--readonly` folder that meets a folder the jail binds writable (`agent_fs_allow`) is refused.
- Interfaces (`store/ingest.py`): `TOOL = "auto_research.store.ingest"`, `ALGORITHM_VERSION = "1"`,
  `PROVENANCE = "human-ingest"`, `SHORT_HASH = 8`, `SINGLE_PUT_MAX` (5 GiB), `PART_SIZE` (256 MiB),
  `RESERVED_SETTINGS`, `READ_TIMEOUT_S` (3600); `client(store)`; `IngestError`; `IngestProduct(bucket, kind,
  subject, folder, files, settings={})` (settings `{name: text}`), `IngestMapping(products)` (pydantic, `extra="forbid"`; kind and subject
  one plain word, folder absolute, files relative); `load_mapping(path)`; `Planned(bucket, kind, subject,
  folder, files {path: (file, sha256, size)}, recipe)`; `plan(store, mapping) -> [Planned]`; async
  `ingest_one(store, planned, *, conn, s3) -> {status: stored | reused, recipe_hash, record, objects}`
  (`conn`: an autocommit connection as the login the product is recorded under); `describe(planned)`.
- **Residuals.** A CLI job's run records no runtime process, so a CLI killed by SIGKILL leaves its keys
  until `revoke --run cli-produce-…` (the run id is printed when the keys are issued; the Postgres login
  expires after `keys.ROLE_VALID_FOR`). `produce` checks no relation between `--env` and the environment
  the command's package names (the runbook names it). Output is printed when the job ends, not streamed.
  Two concurrent ingests of one mapping as `ar_human` share one claim: the second one raises though the
  product is stored (run it again: `reused`). A multipart upload left open by a SIGKILLed ingest keeps
  its parts in the gateway's temporary area; the reconciler lists versions, not open uploads
  (`ListMultipartUploads` shows them; `AbortMultipartUpload` removes them).

## Dependencies
- External: `versitygw` (1.11.0, pinned), `postgresql` (`initdb`, `pg_ctl`, `postgres`), `psycopg` 3,
  `boto3`/`botocore`, `pyyaml`, `pydantic` (all conda-forge, `environment.yml`); `moto` for client tests.
- Internal: `config_paths.orchestration_yaml`, `env._orch_config`, `env.runtime_data_dir`, `env._ENV_FILE`,
  `store.catalog.apply_schema` (`launch`) and `check_schema` (`health`), item S2; `keys` uses
  `catalog.issue_role`/`end_role`/`record_credential`/`revoke_credential`. Used by:
  `preflight.build_default_preflight`; `graph.runtime` (launch step, unit keys, teardown;
  `run_control.RunControl.credentials`); `sandbox.runner`/`persistent` (the keys' environment); receipts
  (`records`, used by `agents.react.SandboxToolProvider` and `graph.runtime`), the reconciler (S6), the CI
  pipeline's throwaway store (`ci`, S7: uses `launch`, `keys`, `records`, `reconcile`'s recipe file and
  metadata names, `sandbox.registry.manifest_literal`, `provenance.inputs.declared_kinds`; used by
  `sandbox.gate`). `records` uses `provenance.inputs.NoSuchRecord` and `schemas.artifacts`.
  `reconcile` (S6) uses `catalog`, `config.stores`, `toolkit_contract.toolkits`, `sandbox.registry.ToolkitRegistry`
  and `git`; it is called by `graph.runtime.start_run_stores` and registered as preflight `store_integrity`.

## Dictionary-level reference
- `config.DEFAULT_STORE = "db"` — the store a unit uses unless it names another (ruling 2026-11-10).
- `config.LOCAL_REGION = "us-east-1"`; `config.SOCKET_PATH_MAX = 107`.
- `config.AdminEnv(s3_access_key, s3_secret_key, catalog_password)` — environment variable NAMES;
  `.values()` → `{field: secret}`, raising naming every unset variable.
- `config.Ports(s3, iam, catalog)` — three distinct unprivileged (≥ 1024) 127.0.0.1 ports.
- `config.CatalogSettings(database="db", admin_user="ar_admin")` — lowercase Postgres names.
- `config.LocalStore(name, kind="local", root, state_dir, socket_dir, ports, buckets, catalog, admin_env)` —
  absolute paths, none inside another; sockets ≤ 107 bytes; buckets 3–63 of `[a-z0-9-]`, unique.
- `config.RemoteStore(name, kind="remote", endpoint, region="us-east-1", buckets, catalog_dsn, admin_env)` —
  `catalog_dsn` names host, dbname and user and no password.
- `config.StoreRoles(producer=("analyst_worker_*",))` — the store's `roles:` key; a
  store's `.role_of(unit)` → `"producer"` when a pattern matches (`fnmatchcase`), else `"reader"`.
- `_Store.synthetic_buckets` (default `()`, a subset of `buckets`) — buckets holding synthetic data; a
  result that read there is marked `synthetic`.
- `records.StoreRecords(*, agent, run, writable=(), stores=None)` — `.current_version(store, bucket, key)`,
  `.version_problem(argument, record) -> str | None`, `async with .bracket(declared=(), given=()) as io`;
  `.client(store)` (the admin S3 client), `.stores`, `.writable`.
- `records.Area(store, bucket, prefix="")`; `records.writable_area(store_names, stores=None) -> [Area]`
  (every bucket of each store, whole).
- `records.CallIO(reads, writes, flags, notes)`, `.summary()` (the `[store] wrote … · read …` line).
- `records.LogLine(requester, bucket, op, key, version_id, status, parsed)`; `records.parse_access_line(line)`;
  `records.WRITE_OPS`, `records.READ_OPS`; `records.StoreRecordError`.
- `records.store_location_problem(arguments, names, cwd, stores=None) -> str | None`.
- `records.StoreRecords(..., reconciler=None)`; `.stamp_flags(writes, flags, receipt, window=None) -> [note]`;
  `CallIO.window` (the bracket's open and close, UTC).
- `catalog.stamp_flags(conn, recipe_hash, flags, receipt) -> int`; `catalog.product_flags(conn, recipe_hash)` (the
  history); `catalog.live_flags(conn, recipe_hash) -> set[str]`; `catalog.clear_flag(conn, recipe_hash, flag, reason)`.
- `reconcile.BLOCKING_FLAGS = {"failed", "synthetic", "undeclared_write", "unverified"}`.
- `config.stores`, `store`, `bind`, `reset` — above.
- `config.admin_env_names(config=None) -> frozenset[str]` — every variable naming a declared store's admin
  secret (none without a `stores:` block); `config.without_admin_secrets(env) -> dict` — `env` without them.
  No process acting for a model holds one: the jail launcher unsets each (`jail.write_launcher`), both
  author harnesses blank them in the env their SDK overlays, the LaTeX compile runs without them (S5,
  2026-11-10). Two stores may share no name, folder or port.
- `launch.READY_TIMEOUT_S = 30.0` — a server's start-up and SIGTERM budget; `launch.HEALTH_PATH = "/health"`.
- `launch.start`, `stop`, `status`, `down`, `holders` — above.
- `health.config_defects() -> list[str]` — what `store_config` refuses that no host fixes by
  providing the store: no default store, a folder a jail may bind, a toolkit manifest naming an
  undeclared store (a block that does not parse raises `ValueError`); unset admin variables are left
  out (an absent store). The live cells fail on a defect and skip an absent store
  (`tests/live_store`, S13a).
- `health.check_store_config()` — the block parses and declares the default store (`DEFAULT_STORE`,
  named in the refusal with the names declared), admin variables set, no folder a jail may bind meets
  root, state_dir or socket_dir (the host `/tmp` itself excepted: each jail gets a private one).
- `health.refusals(store)` — why `store` may not be started (`store_config`'s reasons for it); the
  launch step starts only a store with none.
- `health.ready_problems(store)` — what keeps a store from serving a run, each prefixed by the preflight
  item that would say it (`store_servers`, `store_admin`, `store_buckets`, `store_catalog`: the admin role
  refused, no answer, or another schema, `catalog.check_schema`); the launch step asks before it sweeps,
  issues or reconciles. `check_store_admin`/`check_store_buckets`/`check_store_catalog` run the same
  per-store checks.
- `health.check_store_servers()` — every local store `store_config` accepts answers on all three servers
  (the launch step started it; this check starts nothing, ruling B); a remote S3 endpoint must answer
  HTTP.
- `keys.POLICY_NAME = "ar-role"`; `keys.ROLE_VALID_FOR = 7 days` (the Postgres VALID UNTIL backstop; the
  run's end revokes first); `keys.ENV_NAMES` (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
  `AWS_ENDPOINT_URL_S3`, `AWS_REGION`, `PGHOST`=127.0.0.1, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`,
  and `TOOLKIT_STORE` = `keys.STORE_NAME_VARIABLE`, the store's name, which a tool writes into its records
  and which the CI pipeline's throwaway store sets too, `ci.ENV_STORE`);
  `keys.ROLE_CLASSES = ("reader", "producer", "reconciler")`.
- `keys.Credentials` (frozen; secrets hidden from `repr`): `store, unit, run, role_class, principal,
  access_key_id, secret_access_key, pg_password, s3_endpoint, region, pg_port, pg_database`; `.env()`,
  `.dsn()` (libpq conninfo over 127.0.0.1).
- `keys.principal(run, unit)` — the one name, `[a-z_][a-z0-9_]{0,62}`; `keys.policy(store, role_class)` —
  the inline policy document.
- `health.check_store_admin()` — the admin key lists buckets (and, local, IAM users); an endpoint that
  does not answer is named apart from one that refuses the key.
- `health.check_store_buckets()` — every bucket exists with versioning `Enabled`.
- `health.check_store_catalog()` (async) — the admin role connects to the catalog database (a refused
  role and a server that does not answer are named apart) and `catalog.check_schema(dsn)` passes.

## Changelog
- 2026-11-10 — Phase 3 step 0b S1: package created. `config` (the `stores:` block: `LocalStore`,
  `RemoteStore`, bound once per process), `launch` (versitygw IAM + gateway + Postgres per local store,
  one server set per state_dir by an inherited flock, idempotent start, explicit stop), `health` (preflight
  `store_config`, `store_servers`, `store_admin`, `store_buckets`, `store_catalog`). Tests:
  `tests/test_store_config.py`, `tests/test_store_servers.py` (real servers in temp folders).
- 2026-11-10 — S1 review fixes: server processes are known by holding `server.lock` (`holders`), so a
  start killed before its record is found and restarted and `stop` ends every holder; a busy set is
  waited for, never restarted; `store_servers` starts no store `store_config` refuses; the jail-mount
  check covers toolkit roots, `human_envs`, `env_externals` and the runtime dir; `socket_dir` outside the
  temp root; NFS `nolock`/`local_lock` refused for state_dir; Postgres gets no S3 key, `initdb` its password
  on stdin; the settings include the admin user and key id; messages tell an unreachable server from a
  refused credential; ports ≥ 1024, folders compared through symlinks; `.env` loaded for the admin
  variables; a malformed block names the file.
- 2026-11-10 — S1 + S2 wiring: a fresh `launch.start` applies the catalog schema (`catalog.apply_schema`); `## Catalog` placeholder for S2's section.
- 2026-11-10: S2. The catalog was built: `catalog.sql` (schema v1, four roles, SECURITY DEFINER claim / commit / fail / verify / adopt_orphan / move_current, append-only triggers) and `catalog.py` (apply/check, role and credential helpers for the key adapter, reconciler wrappers and reads). `tests/test_store_catalog.py` has 33 tests on a throwaway Postgres, including forced-interleaving races. After a stateless adversarial review:
  - a failed product with objects is terminal;
  - demoting the current product moves the pointer back;
  - move_current reads the product `FOR SHARE`;
  - adopt_orphan requires an expired claim and the claim's producer;
  - leases run on `clock_timestamp`;
  - the four roles are checked to be NOLOGIN, and each login in a class to hold a live credential;
  - end_role commits NOLOGIN before terminating sessions;
  - re-issue resets a role's settings;
  - TRUNCATE is refused on the append-only tables.
- 2026-11-10: S2 follow-up (coordinator ruling). A recipe input is `{store, bucket, key, version_id}`, the same shape as object rows and the `record` argument (it was `version`).
- 2026-11-10 — (main) S2's catalog section merged under `## Catalog`.
- 2026-11-10 — S4: per-agent keys and the run's store lifecycle. `keys` (versitygw IAM key adapter: one S3
  user + inline role policy + access key and one Postgres login per unit per run; `RunKeys`; `aws_sts` a
  refusing stub); `__main__` (`status|stop`, ruling A; `stop` refuses while live keys are recorded unless
  `--force`); `config` gains `roles` (`StoreRoles`, `role_of`); `health.check_store_servers` only checks
  (ruling B) and `health.refusals` serves the launch step. Tests: `tests/test_store_keys.py` (throwaway
  store: each role refused/allowed by the store, revocation ends open sessions, re-issue, a failed issue
  leaves nothing, keys reach a jailed REPL (human and pipeline environment) and a one-shot call and are
  used there, never the shell server, the launch step, `status|stop`); `tests/test_store_servers.py`
  adapted (store_servers no longer starts a store).
- 2026-11-10 — Phase 3 step 0b S5: `records.py` (receipts from the store's records: the per-call
  version-listing bracket, the access-log reader, the runtime's hash cache, record pins, the named-
  version check, the store-folder path refusal); `_Store.synthetic_buckets` (the synthetic namespace,
  replacing the `synth…` folder regex; both config files). Tests: `tests/test_store_records.py` (a real
  throwaway store).
- 2026-11-10 — S6: the reconciler (`reconcile.py`; `## Reconciler`). `reconcile(store, run=, conn=)` sweeps orphans (adopt / pending / nondeterministic / flag), verifies committed products (HEAD + stored SHA-256, runtime hash for multipart or a later LastModified) and re-checks verified ones cheaply (missing, size, mtime → hash), then moves `current` forward to the newest qualifying product (claimed_at; human override held; CAS re-decided at most 3 times). `catalog.sql` gains `catalog.flag` (SECURITY DEFINER, reconciler; idempotent) and the index `event_action`; SCHEMA_VERSION stays 1 (nothing deployed). Preflight `store_integrity` (checks only). After a stateless adversarial review:
  - a row written during the pass is never judged missing;
  - one nondeterministic verdict per product;
  - a refused verdict is flagged, not raised;
  - promotion is forward only;
  - rows in undeclared buckets get no verdict (`unchecked`);
  - a size disagreement is labelled `edited` or `mismatch` by LastModified;
  - the toolkit commit must exist in the toolkit's repository;
  - synthetic products (by bucket, through ancestry) never qualify;
  - a refused promotion decides again;
  - CLI logins are per process;
  - session locks are held per event loop.

  Tests: `tests/test_store_reconcile.py` (26, on a throwaway store).
- 2026-11-10 — S4 stateless review fixes: keys revoked again in `run_research`'s outermost `finally` and
  the stop step given the reset's 30 s budget; `RunKeys` holds a unit from before its issue (a failed or
  cancelled issue is revoked at the end, `pending()`); a failed rollback is noted on the error; revoke
  marks the principal's live catalog keys (not only those it deleted now); a re-issue ends the earlier
  login's sessions; a one-shot call carrying keys gets a jail home and `/tmp` of its own
  (`runner._keyed_label`); `status|stop`: `stop` refuses when the catalog cannot be read; DOCS name the
  workers as the units holding keys today. Tests added for each.
- 2026-11-10 — S5 stateless review fixes: a bulk delete attributes only markers and removals; a key
  another key also wrote in the window, or with more versions than the unit's lines, is `unverified`
  and unattributed; lines with no status count as succeeded and unreadable lines mark the call
  `unverified`; the unit's keys fall back to the last known set when the catalog fails, and none known
  is `unverified`; a read in a synthetic bucket is marked even when it cannot be hashed; a key written
  twice in one call chains `overwrote`; the summary is capped. Tests: the store-records tests use keys
  issued by `store.keys.issue` and `graph.runtime._store_records` (a wider key only where a role's
  policy would refuse the action), plus bulk delete, same-key, unreadable-line and writer-mismatch
  cases.
- 2026-11-10 — S6 follow-up (coordinator rulings 1-5 on S6's open points):
  - every committed product is hashed by the runtime at its first verify; the store's checksum and the metadata are a second check;
  - `catalog.object_check` + `catalog.record_check` (reconciler): the cheap check's baseline is the last hash's size and LastModified, so a touched-but-identical object is hashed once;
  - adoption requires a complete `recipe.json` written last (`RECIPE_FILE`, `RecipeFile` exported for S8), and adopt_orphan accepts only an expired claim (a producer's `fail` is final);
  - `product.generation`, answered by `claim` (+ 1 per takeover or retry), so each production writes its own folder `<short hash>.<n>/`, and an abandoned one stays, flagged;
  - a qualifying toolkit commit must be reachable from the toolkit's authoring branch.

  SCHEMA_VERSION stays 1. Tests: `tests/test_store_reconcile.py` (33); `tests/test_store_catalog.py` (generation, adopt rule, object_check append-only); `tests/test_store_keys.py`'s launch-step test declares the fixture toolkit instead of patching `_registered_tools`.
- 2026-11-10: S6 follow-up, after its stateless review, plus main's least-privilege ruling.
  - **Generation and claim time:**
    - a restart by the owner after its lease expired is a new generation (`claim.restart`);
    - a renewal keeps `claimed_at`;
    - `adopt_orphan` takes the generation and checks it under the row lock (`catalog.adopt_orphan(..., generation=)`).
  - **Re-hashing:** a second version at a recorded key is judged through `object_check` (hashed once).
  - **Flag lookup:** `catalog.flag`'s lookup uses the `(action, recipe_hash)` index.
  - **Promotion:** the promotion query is bounded by the current product's `claimed_at`.
  - **Adoption refusals:** a refused adoption leaves no flag when the product got committed meanwhile.
  - **Missing branch:** a missing authoring branch is flagged `unqualified`.
  - **Least privilege:** `reconcile(..., s3=)` signs with the reconciler's own reader-class keys (main's ruling); the admin key signs only for the CLI and preflight.
  - Tests: `tests/test_store_reconcile.py` (40); `tests/test_store_catalog.py` (35).
- 2026-11-10 — S4 follow-up (coordinator rulings 1–4): the launch step revokes the keys of dead runs of
  this host (`graph.runtime.sweep_dead_runs`, liveness `ledgers/<run>/runtime.json`), and
  `python -m auto_research.store revoke --run ID` serves the rest; `keys.revoke_principal`, `live_runs`,
  `revoke_run`; the reconciler's own S3 key signs its pass (`reconcile(s3=)`); the producer template drops
  `PutObjectTagging`; the launch step requires `health.ready_problems` empty before it reconciles (a
  missing bucket reads as `store_buckets` words it); the 7-day login life is a recorded residual. Tests in
  `tests/test_store_keys.py`.
- 2026-11-10 — S4 follow-up review fixes: liveness is known only from a well-formed record of this host
  (an unreadable start time, a malformed or corrupt record read as unknown, never dead, and never block a
  launch); the sweep checks liveness again before each principal (a resume's fresh keys stay); a
  per-principal advisory lock and tolerant deletes let two launches sweep one dead run; `revoke --run`
  refuses a run recorded on another host without `--force`. Residuals recorded above.
- 2026-11-10 — S4 follow-up: `tests/test_store_keys.py::test_the_launch_step_starts_reconciles_and_issues_each_units_keys` reads the gateway's access log: the reconciler's `ListObjectVersions` requests carry its own access key, never the admin's.
- 2026-11-10 — S4 follow-up (coordinator): `health.ready_problems` also checks the catalog (`_catalog_problem`, shared with `check_store_catalog`), so a missing schema stops the launch step in `store_catalog`'s words before any key. Test: `test_a_catalog_without_its_schema_fails_the_launch_step_before_any_key`.
- 2026-11-10 — S5 follow-up (coordinator ruling: flags survive across runs through the catalog):
  `catalog.sql` gains the append-only `product_flag` table and `catalog.stamp_flags` (SECURITY
  DEFINER, reconciler and owner; SCHEMA_VERSION stays 1, nothing deployed); `catalog.py`
  `stamp_flags`, `product_flags`; `records.StoreRecords(reconciler=)` stamps a flagged call's
  committed products and carries a read's lineage flags (`_lineage_flags`); the reconciler's promotion
  skips a product carrying `BLOCKING_FLAGS`. Tests: `tests/test_store_records.py` (a failed call's
  product read in a later run, an ancestor's flag reaching a reader, the function's grants and
  refusals), `tests/test_store_reconcile.py` (a flagged product is never promoted).
- 2026-11-10 — S4 follow-up: keyed tool processes also receive `AR_TOOLKIT_COMMIT` and `AR_ENV_FINGERPRINT` (sandbox DOCS).
- 2026-11-10 — S5 follow-up review fixes: a stamp covers every product the unit's login claimed,
  committed or failed in the call's window (a product stored but never committed, then adopted, and
  writes the bracket could not attribute); a failed stamp is retried with the next call's; a stamp on
  the current product demotes it (`catalog._demote`, shared with `verify`, which now also skips flagged
  products when moving back); `move_current` refuses a reconciler login a flagged product (`stamp_flags`
  locks the product, so the two serialise); the qualifier checks flags and failed states over the
  ancestor walk and blocks `synthetic` stamps too; the "carries" note names the product read. Tests:
  claim-only stamping, the retry, `product_flag` append-only for the owner, a late stamp, an
  ancestor's flag in promotion.
- 2026-11-10 — S9: `query.py`, the catalog query a unit's reader login runs (products by kind, subject, state or recipe-hash prefix; the current pointer; one product's objects as records), read-only and time-bounded per session; the lead holds a `reader` login for it (`graph.runtime.issue_unit_keys(role_class="reader")`). Tests: `tests/test_store_query.py` (a throwaway store).
- 2026-11-10 — S9 review fixes: kind and subject match as written (no trimming); a null limit is the default; `objects` takes no other filter and shows its product in full (recipe inputs, flags, current mark); the products list shows an input count instead of the inputs. Residuals recorded above.
- 2026-11-10 — S7: `ci.py`, the CI pipeline's throwaway store (`## CI`): one fresh local store per CI
  pytest run (servers by `launch.start`, keys by `keys.issue`, buckets mirrored, versioning on), reached
  from the network-denied jail through a socket folder bound read-only (an S3 relay, Postgres, the
  per-call bracket by `records.StoreRecords`, data-set seeding as verified products). Tests:
  `tests/test_gate_store.py`.
- 2026-11-10 — S8: the toolkit side of the product contract (research_toolkit `storelib`; the paragraph
  under the reconciler's interfaces). Tests: `tests/test_toolkit_store.py` (contract parity; the
  toolkit's store tests against `ci.throwaway`, then a reconciler pass verifies what they stored).
- 2026-11-10 — S5 follow-up 2 (ruling: an owner-only clear): `product_flag` gains `action` (`stamp` |
  `cleared`) and `reason`; view `live_flag` (a flag counts while its latest row is a stamp), read by
  `move_current`, `_demote`, `StoreRecords._lineage_flags`, `_Qualifier.flagged` and `query`'s product
  columns; `catalog.clear_flag` (the human's only); `catalog.live_flags`, `catalog.clear_flag` in
  Python; `python -m auto_research.store clear-flag`. Tests: a reconciler clear refused, a non-live clear
  refused, readers stop carrying a cleared flag (`tests/test_store_records.py`); a cleared product is
  promoted (`tests/test_store_reconcile.py`).
- 2026-11-10 — S9 follow-up (main rulings 5-6): `StoreRoles` default producer = `analyst_worker_*` only (both orchestration configs' `roles` too): the lead is a reader; `check_store_config` refuses a `stores:` block with no default store, and `graph.runtime.issue_unit_keys` raises for a run bound without one. Tests: `tests/test_store_servers.py::test_a_configuration_without_the_default_store_is_refused_never_silently_storeless`, `tests/test_store_keys.py` (the lead's role).
- 2026-11-10 — S5 follow-up 2 review fixes: a stamp row holds no reason and a clear no receipt, and a
  blank reason is refused (CHECK, `clear_flag`, CLI); `clear_flag` refuses an unknown product and returns
  and logs the receipts it covers; the CLI reports any database error as one line, names the covered
  receipts, a flag readers still derive, and that descendants keep their own stamps. Tests: producer
  refused, unknown product, blank reason, two receipts covered by one clear, a retried stamp stays
  cleared, a new receipt's stamp is live again.
- 2026-11-10 — S5 follow-up 3 (main rulings): a call stamps only the flags it earned itself
  (`CallIO.carried` kept apart; `receipt_call(carried=)`, `CallReceipts.own_flags`), so carried flags
  stay derived through ancestry and a clear upstream reaches the descendants; the human's own login
  `ar_human` (`catalog.HUMAN_ROLE`, made without a password by `apply_schema`, checked by
  `check_schema`; `set_human_password`), `clear_flag` refuses a superuser session; CLI `human-password`,
  `clear-flag` and `move-current` run as that login (password typed or from `--password-file`). Tests:
  carried flags not stamped and cleared upstream, the human login (no password yet refused, the admin
  refused, `stamped_by`/`moved_by` = `ar_human`), a human override.
- 2026-11-10 — S7 additions (main) and stateless-review fixes: the brief's validation records are copied
  in from the real store before each run (`validation`, `cache`, `ENV_VALIDATION`, `validation_problems`;
  a recipe record brings its objects and becomes a verified product); the jail binds a folder of its own
  (relay, bracket, seed sockets and a hard link to Postgres's socket), so the IAM socket stays out; a start
  failure is retried on new ports whatever its message; validation records are copied once per run each,
  and a recipe whose claim is refused raises.
- 2026-11-10 — S5 follow-up 3 review fixes: the human is the login that authenticated (`_authenticated`,
  `_as_human`: `system_user`), so a superuser's `SET SESSION AUTHORIZATION ar_human` neither clears nor
  forges the record; `move_current` lets only the human move onto a flagged product and records the
  authenticated mover; a carried flag the product's recipe inputs do not yield is stamped (`_derivable`);
  `apply_schema` makes `ar_human` on an existing catalog; `check_schema` refuses another login in owner
  and any elevated attribute of `ar_human`; the CLI refuses a password file others can read, drops a
  CRLF ending, exits cleanly without a terminal and on a database error. Open for main (not S5's files):
  the author harnesses inherit `os.environ`, holding the store's admin secrets loaded from `.env`.
- 2026-11-10 — S5 harness-env (main assignment, review finding): `config.admin_env_names`,
  `config.without_admin_secrets`. The author harnesses' SDKs start their CLI from this process's
  environment overlaid with the options' env, so both name every admin secret there with an empty value
  (`sandbox/coder/claude_sdk._without_store_secrets`, `codex_sdk.CodexBackend._build_client`); the coder
  env's fallback (`merge_flow`) drops them. The REPL, the shell server, environment builds and the CI
  test run already start from allow-lists. Review fixes: the jail launcher passes `--unsetenv` for each
  (the Claude SDK's version check spawns the jailed CLI with this process's whole environment); the Claude
  harness blanks every name, held or not; `report.render.compile_report` runs without them. Residuals:
  `admin_env_names` is empty when no `stores:` block can be read (a production launch refuses that in
  preflight; a test or bench without the block passes the variables `.env` loaded); the CI store's own
  `AR_CI_*` admin secrets (`store.ci`, S7) are in this process's environment while a CI run lasts. Tests:
  `tests/test_coder_env_secrets.py` (the harnesses' env builders, and a real jail launcher printing the
  environment it runs with).
- 2026-11-10 — S7 follow-up (main's rulings A-F): the bracket's facts carry each product's catalog row
  and the call's pinned reads (A); `validation_problems` tells a missing record from an unreachable store
  (B); a store only for runs whose packages declare a store tool (`StoreTools.packages`, C); one store
  mirrored is a recorded residual (D); `CIStore.sweep()` after each run, every store tool's call
  bracketed (`ENV_TOOLS`) (E); the throwaway's admin secrets held by its own `AdminEnv`, out of the
  process environment (S5's finding).
- 2026-11-10 — S8 follow-up (rulings R3/R5): `Credentials.env()` carries the store's name as
  `TOOLKIT_STORE` (`keys.STORE_NAME_VARIABLE`, in `ENV_NAMES`), the one variable the runtime and the CI
  pipeline both set; the toolkit's helper reads the name from it (no fixed name in the toolkit) and
  fails a production whose key already holds other bytes as an ordinary failure (a retry claims a new
  generation).
- 2026-11-10 — S6 (main's R4): "current" is keyed by (bucket, kind, subject), not (kind, subject); two buckets may share a subject string.
  - `catalog.sql`:
    - `product.bucket` is set from its objects, and objects spanning two buckets are refused (one bucket per product);
    - `current` and `current_history` gain `bucket` in their keys;
    - `_demote` and `move_current(bucket, kind, subject, …)` follow.
  - `catalog.py`: `current`, `current_history` and `move_current` take `bucket`.
  - `reconcile`: promotion is grouped by (bucket, kind, subject).
  - `query.py`: a `bucket` filter, a bucket column on products and current, and the current mark joined on bucket.
  - The `move-current` CLI takes `--bucket`, and the `catalog_query` tool schema takes `bucket`.
  - `SCHEMA_VERSION` stays 1.
  - Tests: catalog (pointers per bucket, wrong-bucket refusal, mixed-bucket commit refused), reconcile (two buckets sharing a subject), query (bucket filter), records (CLI).
- 2026-11-10 — S6, the R4 review fixes:
  - the one-bucket check counts (store, bucket);
  - promotion covers only the buckets the store declares;
  - the `catalog_query` bucket description and the DOCS say that a product has its bucket once its objects are recorded;
  - tests: an adopted product carries its bucket and is promoted, and a bucket filter excludes.
- 2026-11-10 — S7 follow-up review fixes: a store tool called from another thread joins the open bracket (no wait on itself); a call's reads of its own writes are no inputs; `ValidationRefused` (the brief's fault) apart from a start failure; `_declare` removes every folder it made; `stop` cancels open connections; an unchanged store tool is bracketed only when a test imported it. Residual: sweep attribution is by time window (a late write landing in a later call's bracket is that call's).
- 2026-11-10 — S6 (main's point 3): only a move by the human's own login (`ar_human`) is a human override. Moves by the admin role or a runtime login never block promotion; the role-membership lookup `_is_reconciler` is gone. Tests: an admin-fallback demotion (`stamp_flags` as admin) does not block re-promotion; an admin move does not hold; an `ar_human` rollback holds, also under a compare-and-swap mismatch.
- 2026-11-10 — S8: the toolkit's `data_catalog` joins the current pointer on (bucket, kind, subject).
  Test: `tests/test_toolkit_store.py::test_two_missions_sharing_a_subject_each_list_their_current_product_once`
  (two buckets' products of one subject, promoted by a real reconciler pass, each listed once as current;
  it fails with the earlier two-key join).
- 2026-11-10 — S6, point 3 after its stateless review:
  - the override is explicit: `current.human_override` and `current_history.human_override` are set by `move_current` from `catalog._as_human()`, and are false for every `_demote`;
  - `_demote` records the authenticated login (`catalog._authenticated()`), never an impersonated `session_user`;
  - the reconciler reads `human_override`;
  - tests: a demotion run by an admin impersonating `ar_human`, or by the human's own session, never holds the pointer.
- **Residuals** (reported to main):
  - a later demotion of the human's chosen product falls back through the history and may land on the product the human rejected;
  - on a remote store authenticated by cert, peer or ident, `system_user` names the external identity, so a human move is not recognised (local stores use SCRAM).
- 2026-11-10 — S7 final follow-up (main's rulings 1-3): the seed channel stores synthetic products a test stages (`{kind, subject, bucket, files}`; `_store_seeded` serves both kinds of seed); a manifest `reads` makes a store package; `SEED_LINE_MAX`.
- 2026-11-10 — S7 final follow-up review fixes: seed names checked before the claim and a failed store fails the claim; the `paths` data-set form (the toolkit fixture's); a reused seed answers in its first order; a request that is no JSON object, or base64 that is not standard, is refused with the action.
- 2026-11-10 — S7 last follow-up (main's rulings A-D): synthetic seeds stamped `synthetic`; `undeclared_store_names` refused by the gate's structural job and by `store_config` (claimed: `health.check_store_config`); a nested read-only tool call recorded as a residual; the bracket's facts carry the call's `carried` flags.
- 2026-11-11 — S12 (docs): the reconciler diagram names its callers as built, each run's launch step and the CLI
  (it said "after commits, run end": no such call exists; a run's new products become current at the next launch).
  The catalog residual on committed keys no longer cites a prefix-scoped write policy or HEASARC-layout raw keys (the
  policy covers every key of the buckets; raw downloads are recipe-named products like any other).
- 2026-11-11 — S11 (migration tooling, dry run only): `ingest.py` (the human's ingest of files on disk as
  products of `ingest.TOOL`, as `ar_human`) and the CLI actions `start [--create-buckets]`, `ingest
  --mapping [--check]`, `produce --unit --env [--readonly] -- COMMAND` (`## Migration and ingest`);
  `sandbox.runner.run_in_environment(readonly=)`. After a stateless review: every file hashed again before
  the claim; globs relative to the folder; the same files in two buckets refused; an interrupt gives the
  claim up; the producer job's keys revoked on SIGTERM/SIGHUP and a timeout (its partial output kept), a
  `--readonly` folder meeting a writable jail root refused, no dirty-toolkit escape. Tests:
  `tests/test_store_ingest.py` (14, a throwaway store; the producer job in a real jail).
- 2026-11-11 — S11 follow-up (human ruling: preserve old, fetch new later): an ingest product carries the
  human's own `settings` (beside `folder` and `files`); a file above 5 GiB goes up in parts with per-part
  SHA-256 (`PART_SIZE`), so a kept archive download (raw, never current) can be ingested whole. Test: a
  three-part file verified by the reconciler, a reserved setting name refused.
- 2026-11-11 — S11 follow-up review fixes (and a failure the probe run found: a 9.7 GB completion took 33 s,
  the admin client's 30 s timeout retried it, and the retry's answer named no version): the ingest's own
  client (`client`, `READ_TIMEOUT_S`, no retry); an answer without a version id or a 412 read back from the
  key, its abort guarded; an interrupt stops a part upload at the next part; settings are text only and
  shown by `--check`; a file going up in parts is not hashed again before the claim (it is compared whole
  during its upload). Tests: other bytes abort the upload with nothing left open, a versionless answer and
  a 412 read back, other bytes at a known key refused, `folder` refused, a float refused.
- 2026-11-11 — S7 follow-up (main's go): a throwaway's life lock (`in-use`, flocked for its life) under `<temp root>/ar-ci/<id>/` and `/var/tmp/ar-ci/<id>-s|-j`; `sweep_leftovers` at every start and as `python -m auto_research.store ci-sweep` (the one way to clean them); `stop` ends the servers by recorded pid and start time when their folder is gone. Tests: `tests/test_gate_store.py` (a killed gate's leftovers swept, a store in use untouched, a removed folder's servers stopped).
- 2026-11-11 — S7 follow-up review fixes: one fixed per-user folder `/var/tmp/ar-ci-<uid>/` for every throwaway folder (a sweep with another `TMPDIR` had ended live stores; a second user was refused the shared name); the sweep ends only processes holding a store's `server.lock` (a `tail` on its log had been killed); a work folder is named only once locked (`.new-…` first); the scan reads a process's start time before and after; the sweep tests use a folder of their own.
- 2026-11-11 — S7 follow-up, found running two suites at once: the sweep judges each server by its own store's life lock at scan time (judged against a folder listing taken earlier, it ended another process's store named meanwhile; test `test_a_sweep_leaves_alone_a_store_named_after_it_listed_the_folder`); a start confirms its servers are its own (`_own_servers`) and retries on fresh ports otherwise.
- 2026-11-11 — S7 follow-up, second stateless review: an exited server (a zombie) counts as gone and is collected, so `stop` takes about a second instead of 15 s; the servers are stopped even when the store's event loop stalls; a failed start attempt ends its servers by pid; the sweep tolerates entries removed meanwhile, a file closing during its scan, and a symlinked parent; it never ends a process holding a store's life lock. Tests: the port race (`_own_servers` starts again on fresh ports), half-made `.new-…` folders and their grace, and every sweep test's teardown requires the sweep to find nothing.
- 2026-11-11 — S7 follow-up (main's rulings A-D): `launch.start` confirms the servers on its ports are its own (`_confirm_own`; test in `tests/test_store_servers.py`), replacing the throwaway's own check; a throwaway start ends leftover servers only (`sweep_leftovers(remove_folders=False)`), folders go with `ci-sweep`; residuals B and C recorded.
- 2026-11-11 — S7 follow-up review: `_confirm_own`'s refusal names the exception and says the root key was not accepted or nothing answered; tests pin each guard (a server exiting on a taken port; servers refusing the store's key).
- 2026-11-11 — S7 follow-up (main's rulings 1, 3): preflight notice `store_ci_leftovers` (`leftover_folders`, `check_ci_leftovers`; the sweep removes exactly what `leftover_folders` lists); the store test fixtures (`make_store`, the reconciler's `env`, `serving_real_store`) remove their socket folders also when a store is refused or never started.
- 2026-11-11 — S13a follow-up 3 (main's ruling 1): `health.config_defects()` splits `store_config`'s refusals a host cannot fix by providing the store from an absent store's (`_placement_problems`, `_default_problems` shared with `check_store_config`, whose behaviour is unchanged).
- 2026-11-11 — S11 real run done: 21 raw observations migrated by `produce`, 01002901 and 01250101 kept as the human's raw products (01250101's first single 3.3 GB PUT was refused by the gateway under load with `XAmzContentSHA256Mismatch`; rerun on a quiet store, generation 2), 5 spectra ingested. Final reconcile: 23 raw + 5 pha verified, 21 current, every object hashed, 10 orphans flagged in 01250101's abandoned generation 1; `fzhou_obsdb` unchanged (1238 entries, size/mtime/ctime/mode compared). No code change.
