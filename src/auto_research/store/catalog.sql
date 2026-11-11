-- The store catalog: every mutable fact about the store's objects (products, claims, "current",
-- credentials, events) in one Postgres schema, `catalog`. Bytes and version ids live in S3.
--
-- Applied by `auto_research.store.catalog.apply_schema` on a superuser connection, in one
-- transaction; that function records the schema version afterwards. Roles are cluster-wide:
--   owner      the human; owns the schema, every table and every function (NOLOGIN group role)
--   reader     SELECT on every table
--   producer   reader + claim / commit / fail
--   reconciler reader + verify / adopt_orphan / move_current / flag / record_check / stamp_flags (the runtime)
-- Agent login roles are members of reader or producer, never of owner. No role but owner holds
-- INSERT / UPDATE / DELETE on a table: every write goes through a SECURITY DEFINER function below,
-- which names its caller by `session_user` and writes an `event` row.

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'owner') THEN CREATE ROLE owner NOLOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'reader') THEN CREATE ROLE reader NOLOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'producer') THEN CREATE ROLE producer NOLOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'reconciler') THEN CREATE ROLE reconciler NOLOGIN; END IF;
END $$;
GRANT reader TO producer;
GRANT reader TO reconciler;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
-- a function owner creates later (a migration) is callable only by the roles it is granted to
ALTER DEFAULT PRIVILEGES FOR ROLE owner REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
CREATE SCHEMA catalog AUTHORIZATION owner;

SET ROLE owner;

CREATE TABLE catalog.schema_version (
  version integer PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);

-- A product is one run of a recipe: tool + algorithm version + settings + input versions, with the
-- (kind, subject) it is a product of. Its key in the store is named by recipe_hash, written once.
-- bucket: the one bucket its objects lie in, set when they are recorded (NULL before).
CREATE TABLE catalog.product (
  recipe_hash text PRIMARY KEY CHECK (recipe_hash ~ '^[0-9a-f]{64}$'),
  kind text NOT NULL,
  subject text NOT NULL,
  bucket text CHECK (bucket <> ''),
  recipe jsonb NOT NULL,
  state text NOT NULL CHECK (state IN ('claimed', 'committed', 'verified', 'failed', 'nondeterministic')),
  owner text NOT NULL,
  lease_until timestamptz,
  toolkit_commit text,
  env_fingerprint text,
  claimed_at timestamptz NOT NULL,
  -- which production of the recipe this is: 1 for the first claim, +1 for each takeover or retry, so
  -- each production writes its own folder and never meets an abandoned one's create-once keys
  generation integer NOT NULL DEFAULT 1 CHECK (generation > 0),
  committed_at timestamptz,
  verified_at timestamptz,
  reason text
);
CREATE INDEX product_kind_subject ON catalog.product (kind, subject);
CREATE INDEX product_bucket_kind_subject ON catalog.product (bucket, kind, subject);
CREATE INDEX product_state ON catalog.product (state);

-- One stored version of one key. Immutable: a changed byte is a new version, so a new row.
CREATE TABLE catalog.object (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  store text NOT NULL,
  bucket text NOT NULL,
  key text NOT NULL,
  version_id text NOT NULL,
  sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  size bigint NOT NULL CHECK (size >= 0),
  recipe_hash text NOT NULL REFERENCES catalog.product,
  created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (store, bucket, key, version_id)
);
CREATE INDEX object_recipe ON catalog.object (recipe_hash);
CREATE INDEX object_sha256 ON catalog.object (sha256);

-- The product a (bucket, kind, subject) reads by default; seq counts its moves (compare-and-swap).
-- Always a verified product of that bucket, or NULL once every product it pointed at was demoted.
-- Keyed by bucket too: two buckets may use one subject string for different things.
CREATE TABLE catalog.current (
  bucket text NOT NULL,
  kind text NOT NULL,
  subject text NOT NULL,
  recipe_hash text REFERENCES catalog.product,
  seq bigint NOT NULL CHECK (seq > 0),
  moved_by text NOT NULL,
  moved_at timestamptz NOT NULL,
  reason text NOT NULL,
  -- the human's own deliberate move (move_current as ar_human, catalog._as_human()); never a demotion
  human_override boolean NOT NULL DEFAULT false,
  PRIMARY KEY (bucket, kind, subject)
);

CREATE TABLE catalog.current_history (
  bucket text NOT NULL,
  kind text NOT NULL,
  subject text NOT NULL,
  seq bigint NOT NULL,
  recipe_hash text REFERENCES catalog.product,
  previous text REFERENCES catalog.product,
  moved_by text NOT NULL,
  moved_at timestamptz NOT NULL,
  reason text NOT NULL,
  human_override boolean NOT NULL DEFAULT false,
  PRIMARY KEY (bucket, kind, subject, seq)
);

-- Which agent and run hold a key: an S3 access key id, or a Postgres login role.
CREATE TABLE catalog.credential (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind text NOT NULL CHECK (kind IN ('s3', 'postgres')),
  key_id text NOT NULL,
  agent text NOT NULL,
  run text NOT NULL,
  issued_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz
);
CREATE UNIQUE INDEX credential_live ON catalog.credential (kind, key_id) WHERE revoked_at IS NULL;

CREATE TABLE catalog.event (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at timestamptz NOT NULL DEFAULT clock_timestamp(),
  actor text NOT NULL DEFAULT session_user,
  action text NOT NULL,
  recipe_hash text,
  detail jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX event_action ON catalog.event (action, recipe_hash);

CREATE FUNCTION catalog.refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'catalog.% is append-only: % refused', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE TRIGGER object_immutable BEFORE UPDATE OR DELETE ON catalog.object
  FOR EACH ROW EXECUTE FUNCTION catalog.refuse_change();
CREATE TRIGGER current_history_append_only BEFORE UPDATE OR DELETE ON catalog.current_history
  FOR EACH ROW EXECUTE FUNCTION catalog.refuse_change();
CREATE TRIGGER event_append_only BEFORE UPDATE OR DELETE ON catalog.event
  FOR EACH ROW EXECUTE FUNCTION catalog.refuse_change();
CREATE TRIGGER object_no_truncate BEFORE TRUNCATE ON catalog.object
  FOR EACH STATEMENT EXECUTE FUNCTION catalog.refuse_change();
CREATE TRIGGER current_history_no_truncate BEFORE TRUNCATE ON catalog.current_history
  FOR EACH STATEMENT EXECUTE FUNCTION catalog.refuse_change();
CREATE TRIGGER event_no_truncate BEFORE TRUNCATE ON catalog.event
  FOR EACH STATEMENT EXECUTE FUNCTION catalog.refuse_change();

-- The reconciler's record of hashing one stored version: its size and LastModified as listed, and the
-- SHA-256 of its bytes. A later pass that lists the same size and LastModified needs no hash.
CREATE TABLE catalog.object_check (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  store text NOT NULL,
  bucket text NOT NULL,
  key text NOT NULL,
  version_id text NOT NULL,
  size bigint NOT NULL CHECK (size >= 0),
  mtime timestamptz NOT NULL,
  sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  checked_by text NOT NULL DEFAULT session_user,
  checked_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX object_check_version ON catalog.object_check (store, bucket, key, version_id, id);
CREATE TRIGGER object_check_append_only BEFORE UPDATE OR DELETE ON catalog.object_check
  FOR EACH ROW EXECUTE FUNCTION catalog.refuse_change();
CREATE TRIGGER object_check_no_truncate BEFORE TRUNCATE ON catalog.object_check
  FOR EACH STATEMENT EXECUTE FUNCTION catalog.refuse_change();

-- The provenance marks a product carries from the run that made it: each flag of the call receipt
-- that committed it (failed, synthetic, undeclared_write, unverified), stamped by the runtime after
-- the receipt is minted (catalog.stamp_flags), so a later run reading the product carries them too.
-- Append-only: a stamp is never taken back; the human clears a flag with a `cleared` row
-- (catalog.clear_flag, the owner's only). A flag counts while its latest row is a stamp (live_flag).
CREATE TABLE catalog.product_flag (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  recipe_hash text NOT NULL REFERENCES catalog.product,
  flag text NOT NULL CHECK (flag IN ('failed', 'synthetic', 'undeclared_write', 'unverified')),
  action text NOT NULL DEFAULT 'stamp' CHECK (action IN ('stamp', 'cleared')),
  -- a stamp names the receipt it came from; a clear names why
  receipt text CHECK (receipt <> ''),
  reason text CHECK (reason <> ''),
  stamped_by text NOT NULL DEFAULT session_user,
  stamped_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK ((action = 'stamp' AND receipt IS NOT NULL AND reason IS NULL)
         OR (action = 'cleared' AND reason IS NOT NULL AND btrim(reason) <> '' AND receipt IS NULL)),
  UNIQUE (recipe_hash, flag, receipt)
);
CREATE INDEX product_flag_latest ON catalog.product_flag (recipe_hash, flag, id);
-- The flags that count: per (product, flag), the latest row, when it is a stamp.
CREATE VIEW catalog.live_flag AS
  SELECT latest.recipe_hash, latest.flag, latest.receipt, latest.stamped_by, latest.stamped_at
    FROM (SELECT DISTINCT ON (recipe_hash, flag) * FROM catalog.product_flag
           ORDER BY recipe_hash, flag, id DESC) latest
   WHERE latest.action = 'stamp';
CREATE TRIGGER product_flag_append_only BEFORE UPDATE OR DELETE ON catalog.product_flag
  FOR EACH ROW EXECUTE FUNCTION catalog.refuse_change();
CREATE TRIGGER product_flag_no_truncate BEFORE TRUNCATE ON catalog.product_flag
  FOR EACH STATEMENT EXECUTE FUNCTION catalog.refuse_change();

-- ---------------------------------------------------------------- helpers (owner only)

-- The login that authenticated this session (system_user: `<method>:<role>`), which `SET SESSION
-- AUTHORIZATION` cannot change; session_user when the method names none.
CREATE FUNCTION catalog._authenticated() RETURNS text LANGUAGE sql STABLE AS $$
  SELECT coalesce(nullif(split_part(system_user, ':', 2), ''), session_user::text)
$$;

-- Whether this session acts as the human: authenticated as a non-superuser login, not impersonated
-- (session_user is the login that authenticated), and a member of owner.
CREATE FUNCTION catalog._as_human() RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT catalog._authenticated() = session_user::text
     AND NOT coalesce((SELECT r.rolsuper FROM pg_roles r WHERE r.rolname = catalog._authenticated()), true)
     AND EXISTS (SELECT FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member
                 JOIN pg_roles g ON g.oid = m.roleid WHERE r.rolname = session_user AND g.rolname = 'owner')
$$;

-- The recipe's identity: sha256 of its jsonb text (jsonb orders keys and spacing one way).
CREATE FUNCTION catalog.recipe_hash(recipe jsonb) RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT encode(sha256(convert_to(recipe::text, 'UTF8')), 'hex')
$$;

-- What is wrong with a recipe, or NULL. Keys: kind, subject, tool, algorithm_version (non-empty
-- strings), settings (an object), inputs (a list of {store, bucket, key, version_id}, in the order
-- the tool takes them). Provenance (toolkit commit, environment) stays out: it never changes a
-- product's identity.
CREATE FUNCTION catalog._recipe_problem(recipe jsonb) RETURNS text LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  k text;
  inp jsonb;
BEGIN
  IF recipe IS NULL OR jsonb_typeof(recipe) <> 'object' THEN RETURN 'a recipe is a JSON object'; END IF;
  IF (SELECT array_agg(x ORDER BY x COLLATE "C") FROM jsonb_object_keys(recipe) x)
     IS DISTINCT FROM ARRAY['algorithm_version', 'inputs', 'kind', 'settings', 'subject', 'tool'] THEN
    RETURN 'a recipe holds exactly kind, subject, tool, algorithm_version, settings, inputs';
  END IF;
  FOREACH k IN ARRAY ARRAY['kind', 'subject', 'tool', 'algorithm_version'] LOOP
    IF jsonb_typeof(recipe -> k) <> 'string' OR btrim(recipe ->> k) = '' THEN
      RETURN format('recipe %s is a non-empty string', k);
    END IF;
  END LOOP;
  IF jsonb_typeof(recipe -> 'settings') <> 'object' THEN RETURN 'recipe settings is an object'; END IF;
  IF jsonb_typeof(recipe -> 'inputs') <> 'array' THEN RETURN 'recipe inputs is a list'; END IF;
  FOR inp IN SELECT * FROM jsonb_array_elements(recipe -> 'inputs') LOOP
    IF jsonb_typeof(inp) <> 'object'
       OR (SELECT array_agg(x ORDER BY x COLLATE "C") FROM jsonb_object_keys(inp) x)
          IS DISTINCT FROM ARRAY['bucket', 'key', 'store', 'version_id']
       OR EXISTS (SELECT FROM jsonb_each(inp) e WHERE jsonb_typeof(e.value) <> 'string' OR btrim(e.value #>> '{}') = '') THEN
      RETURN 'each recipe input is {store, bucket, key, version_id}, non-empty strings';
    END IF;
  END LOOP;
  RETURN NULL;
END $$;

CREATE FUNCTION catalog._log(action text, hash text, detail jsonb) RETURNS void LANGUAGE sql AS $$
  INSERT INTO catalog.event (action, recipe_hash, detail) VALUES (action, hash, detail)
$$;

CREATE FUNCTION catalog._objects(hash text) RETURNS jsonb LANGUAGE sql STABLE AS $$
  SELECT coalesce(jsonb_agg(jsonb_build_object('store', o.store, 'bucket', o.bucket, 'key', o.key,
                                               'version_id', o.version_id, 'sha256', o.sha256, 'size', o.size)
                            ORDER BY o.id), '[]')
  FROM catalog.object o WHERE o.recipe_hash = hash
$$;

-- Records a product's objects: a non-empty list of {store, bucket, key, version_id, sha256, size}.
CREATE FUNCTION catalog._insert_objects(hash text, objects jsonb, creator text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  o jsonb;
BEGIN
  IF objects IS NULL OR jsonb_typeof(objects) <> 'array' OR jsonb_array_length(objects) = 0 THEN
    RAISE EXCEPTION 'objects is a non-empty list of {store, bucket, key, version_id, sha256, size}'
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  -- one bucket (of one store) per product: its current pointer is keyed by that bucket
  IF (SELECT count(DISTINCT (e ->> 'store', e ->> 'bucket')) FROM jsonb_array_elements(objects) e) > 1 THEN
    RAISE EXCEPTION 'a product''s objects lie in one bucket of one store; these name %',
      (SELECT string_agg(DISTINCT (e ->> 'store') || '/' || (e ->> 'bucket'), ', ') FROM jsonb_array_elements(objects) e)
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  FOR o IN SELECT * FROM jsonb_array_elements(objects) LOOP
    IF jsonb_typeof(o) <> 'object'
       OR (SELECT array_agg(x ORDER BY x COLLATE "C") FROM jsonb_object_keys(o) x)
          IS DISTINCT FROM ARRAY['bucket', 'key', 'sha256', 'size', 'store', 'version_id']
       OR EXISTS (SELECT FROM jsonb_each(o) e
                  WHERE e.key <> 'size' AND (jsonb_typeof(e.value) <> 'string' OR btrim(e.value #>> '{}') = ''))
       OR jsonb_typeof(o -> 'size') <> 'number' OR o ->> 'size' !~ '^[0-9]+$'
       OR o ->> 'sha256' !~ '^[0-9a-f]{64}$' THEN
      RAISE EXCEPTION 'object % is {store, bucket, key, version_id, sha256 (64 hex), size (bytes)}', o
        USING ERRCODE = 'invalid_parameter_value';
    END IF;
    BEGIN
      INSERT INTO catalog.object (store, bucket, key, version_id, sha256, size, recipe_hash, created_by)
      VALUES (o ->> 'store', o ->> 'bucket', o ->> 'key', o ->> 'version_id', o ->> 'sha256',
              (o ->> 'size')::bigint, hash, creator);
    EXCEPTION WHEN unique_violation THEN
      RAISE EXCEPTION 'object %/%/% version % is already recorded',
        o ->> 'store', o ->> 'bucket', o ->> 'key', o ->> 'version_id' USING ERRCODE = 'unique_violation';
    END;
  END LOOP;
  UPDATE catalog.product p SET bucket = objects -> 0 ->> 'bucket' WHERE p.recipe_hash = hash;
END $$;

-- ---------------------------------------------------------------- producer

-- Claim a recipe before running it. Returns {status, recipe_hash, ...}:
--   claimed           the caller holds it until lease_until (new, renewed, taken over from an
--                     expired claim, or retried after a failure); generation names the production
--                     (1 first, +1 per takeover, retry, or restart by its owner after expiry; renewing a
--                     live lease keeps it and the claim time), and the helper writes its folder from it
--   in_progress       another agent holds a live claim: owner, lease_until, generation
--   reuse             already committed: objects lists the stored versions to read
--   failed            its stored objects failed verification; reason says why (a human decides)
--   nondeterministic  one recipe gave two different outputs; reason says what was seen
-- A lease runs on the wall clock (clock_timestamp), so a claim made late in a long transaction
-- still gets its whole lease. Call it outside an open transaction (autocommit): a claim inside one
-- holds the row until that transaction ends, and other agents wait on it.
CREATE FUNCTION catalog.claim(recipe jsonb, lease interval DEFAULT interval '1 hour') RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = catalog, pg_temp AS $$
DECLARE
  problem text := catalog._recipe_problem(recipe);
  me text := session_user;
  h text;
  p catalog.product;
  action text;
  lease_end timestamptz := clock_timestamp() + lease;
BEGIN
  IF problem IS NOT NULL THEN
    RAISE EXCEPTION 'claim refused: %', problem USING ERRCODE = 'invalid_parameter_value';
  END IF;
  IF lease IS NULL OR lease <= interval '0' OR lease > interval '1 day' THEN
    RAISE EXCEPTION 'claim refused: a lease lies between 0 and 1 day (renew it by claiming again)'
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  h := catalog.recipe_hash(recipe);
  INSERT INTO catalog.product (recipe_hash, kind, subject, recipe, state, owner, lease_until, claimed_at)
  VALUES (h, recipe ->> 'kind', recipe ->> 'subject', recipe, 'claimed', me, lease_end, clock_timestamp())
  ON CONFLICT (recipe_hash) DO NOTHING;
  IF FOUND THEN
    PERFORM catalog._log('claim', h, jsonb_build_object('lease_until', lease_end));
    RETURN jsonb_build_object('status', 'claimed', 'recipe_hash', h, 'owner', me, 'lease_until', lease_end,
                              'generation', 1);
  END IF;

  SELECT * INTO p FROM catalog.product WHERE product.recipe_hash = h FOR UPDATE;
  IF p.state IN ('committed', 'verified') THEN
    PERFORM catalog._log('claim.reuse', h, jsonb_build_object('state', p.state));
    RETURN jsonb_build_object('status', 'reuse', 'recipe_hash', h, 'state', p.state, 'owner', p.owner,
                              'generation', p.generation, 'objects', catalog._objects(h));
  ELSIF p.state = 'nondeterministic'
        OR (p.state = 'failed' AND EXISTS (SELECT FROM catalog.object o WHERE o.recipe_hash = h)) THEN
    -- its objects hold the recipe's keys (written once), so no retry can store a product there
    PERFORM catalog._log('claim.' || p.state, h, '{}');
    RETURN jsonb_build_object('status', p.state, 'recipe_hash', h, 'reason', p.reason);
  ELSIF p.state = 'claimed' AND p.owner <> me AND p.lease_until > clock_timestamp() THEN
    PERFORM catalog._log('claim.in_progress', h, jsonb_build_object('owner', p.owner));
    RETURN jsonb_build_object('status', 'in_progress', 'recipe_hash', h, 'owner', p.owner,
                              'lease_until', p.lease_until, 'generation', p.generation);
  END IF;

  -- a renewal of a live lease continues the production; anything else starts a new one (a new
  -- generation and claim time): a retry after a fail, a takeover, or its owner restarting after expiry
  action := CASE WHEN p.state = 'failed' THEN 'claim.retry'
                 WHEN p.owner = me AND p.lease_until > clock_timestamp() THEN 'claim.renew'
                 WHEN p.owner = me THEN 'claim.restart'
                 ELSE 'claim.takeover' END;
  UPDATE catalog.product
     SET state = 'claimed', owner = me, lease_until = lease_end, reason = NULL,
         claimed_at = CASE WHEN action = 'claim.renew' THEN p.claimed_at ELSE clock_timestamp() END,
         generation = CASE WHEN action = 'claim.renew' THEN p.generation ELSE p.generation + 1 END
   WHERE product.recipe_hash = h;
  PERFORM catalog._log(action, h, jsonb_build_object('previous_owner', p.owner, 'previous_state', p.state,
                                                     'lease_until', lease_end,
                                                     'generation', (SELECT q.generation FROM catalog.product q
                                                                    WHERE q.recipe_hash = h)));
  RETURN jsonb_build_object('status', 'claimed', 'recipe_hash', h, 'owner', me, 'lease_until', lease_end,
                            'generation', (SELECT q.generation FROM catalog.product q WHERE q.recipe_hash = h));
END $$;

-- Record the objects a claimed recipe produced, in one transaction; the caller holds the claim.
CREATE FUNCTION catalog.commit(hash text, objects jsonb, toolkit_commit text, env_fingerprint text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = catalog, pg_temp AS $$
DECLARE
  me text := session_user;
  p catalog.product;
BEGIN
  SELECT * INTO p FROM catalog.product WHERE product.recipe_hash = hash FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'commit refused: recipe % has no claim; claim it first', hash
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  IF p.state <> 'claimed' THEN
    RAISE EXCEPTION 'commit refused: recipe % is %, not claimed', hash, p.state
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  IF p.owner <> me THEN
    RAISE EXCEPTION 'commit refused: the claim on recipe % is held by %', hash, p.owner
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF coalesce(commit.toolkit_commit, '') = '' OR coalesce(commit.env_fingerprint, '') = '' THEN
    RAISE EXCEPTION 'commit refused: name the toolkit commit and the environment fingerprint'
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  PERFORM catalog._insert_objects(hash, objects, me);
  UPDATE catalog.product
     SET state = 'committed', toolkit_commit = commit.toolkit_commit,
         env_fingerprint = commit.env_fingerprint, committed_at = now(), lease_until = NULL
   WHERE product.recipe_hash = hash;
  PERFORM catalog._log('commit', hash, jsonb_build_object('objects', jsonb_array_length(objects),
                       'toolkit_commit', commit.toolkit_commit, 'env_fingerprint', commit.env_fingerprint));
  RETURN jsonb_build_object('status', 'committed', 'recipe_hash', hash, 'objects', catalog._objects(hash));
END $$;

-- Give up a claim the caller holds; nondeterministic when the store already holds other bytes
-- under this recipe's key.
CREATE FUNCTION catalog.fail(hash text, reason text, nondeterministic boolean DEFAULT false)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = catalog, pg_temp AS $$
DECLARE
  me text := session_user;
  p catalog.product;
  next_state text := CASE WHEN nondeterministic THEN 'nondeterministic' ELSE 'failed' END;
BEGIN
  IF coalesce(fail.reason, '') = '' THEN
    RAISE EXCEPTION 'fail refused: give the reason' USING ERRCODE = 'invalid_parameter_value';
  END IF;
  SELECT * INTO p FROM catalog.product WHERE product.recipe_hash = hash FOR UPDATE;
  IF NOT FOUND OR p.state <> 'claimed' THEN
    RAISE EXCEPTION 'fail refused: recipe % is %, not claimed', hash, coalesce(p.state, 'unknown')
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  IF p.owner <> me THEN
    RAISE EXCEPTION 'fail refused: the claim on recipe % is held by %', hash, p.owner
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  UPDATE catalog.product SET state = next_state, reason = fail.reason, lease_until = NULL
   WHERE product.recipe_hash = hash;
  PERFORM catalog._log('fail', hash, jsonb_build_object('state', next_state, 'reason', fail.reason));
  RETURN jsonb_build_object('status', next_state, 'recipe_hash', hash);
END $$;

-- ---------------------------------------------------------------- reconciler

-- The reconciler's verdict on a committed (or verified) product after reading its objects back:
-- verified, failed (missing or changed bytes) or nondeterministic. A reason goes with any but verified.
-- Demoting the current product moves its pointer, in the same transaction, back to the latest
-- product it pointed at that is still verified (NULL when none is), so current is always verified.
-- Move a (bucket, kind, subject)'s pointer off `hash` when it points there: back to the latest product in its
-- history that is still verified and carries no live flag (catalog.live_flag), or to NULL; one
-- history row and one event. Called by verify (a failed check) and stamp_flags (a blocking flag).
CREATE FUNCTION catalog._demote(hash text, why text) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  c catalog.current;
  back text;
BEGIN
  SELECT * INTO c FROM catalog.current cur WHERE cur.recipe_hash = hash FOR UPDATE;
  IF NOT FOUND THEN
    RETURN;
  END IF;
  SELECT hh.recipe_hash INTO back
    FROM catalog.current_history hh JOIN catalog.product q ON q.recipe_hash = hh.recipe_hash
   WHERE hh.bucket = c.bucket AND hh.kind = c.kind AND hh.subject = c.subject AND q.state = 'verified'
     AND q.recipe_hash <> hash
     AND NOT EXISTS (SELECT FROM catalog.live_flag f WHERE f.recipe_hash = q.recipe_hash)
   ORDER BY hh.seq DESC LIMIT 1;
  UPDATE catalog.current cur
     SET recipe_hash = back, seq = c.seq + 1, moved_by = catalog._authenticated(), moved_at = now(), reason = why,
         human_override = false
   WHERE cur.bucket = c.bucket AND cur.kind = c.kind AND cur.subject = c.subject;
  INSERT INTO catalog.current_history (bucket, kind, subject, seq, recipe_hash, previous, moved_by, moved_at, reason)
  VALUES (c.bucket, c.kind, c.subject, c.seq + 1, back, hash, catalog._authenticated(), now(), why);
  PERFORM catalog._log('move_current', back, jsonb_build_object('bucket', c.bucket, 'kind', c.kind, 'subject', c.subject,
                       'seq', c.seq + 1, 'previous', hash, 'reason', why));
END $$;

CREATE FUNCTION catalog.verify(hash text, outcome text DEFAULT 'verified', reason text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = catalog, pg_temp AS $$
DECLARE
  p catalog.product;
BEGIN
  IF outcome IS NULL OR outcome NOT IN ('verified', 'failed', 'nondeterministic') THEN
    RAISE EXCEPTION 'verify refused: outcome is verified, failed or nondeterministic'
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  IF outcome <> 'verified' AND coalesce(verify.reason, '') = '' THEN
    RAISE EXCEPTION 'verify refused: give the reason for %', outcome USING ERRCODE = 'invalid_parameter_value';
  END IF;
  -- NO KEY UPDATE: blocks move_current's FOR SHARE read of this product, never a history row's key check
  SELECT * INTO p FROM catalog.product WHERE product.recipe_hash = hash FOR NO KEY UPDATE;
  IF NOT FOUND OR p.state NOT IN ('committed', 'verified') THEN
    RAISE EXCEPTION 'verify refused: recipe % is %, not committed', hash, coalesce(p.state, 'unknown')
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  UPDATE catalog.product
     SET state = outcome, reason = verify.reason,
         verified_at = CASE WHEN outcome = 'verified' THEN now() ELSE product.verified_at END
   WHERE product.recipe_hash = hash;
  PERFORM catalog._log('verify', hash, jsonb_build_object('outcome', outcome, 'previous_state', p.state,
                                                          'reason', verify.reason));
  IF outcome <> 'verified' THEN
    PERFORM catalog._demote(hash, format('current product %s is %s: %s', hash, outcome, verify.reason));
  END IF;
  RETURN jsonb_build_object('status', outcome, 'recipe_hash', hash);
END $$;

-- Record objects a producer wrote but never committed (it stopped between the PUT and the commit),
-- read back by the reconciler from the store with their metadata. Only an expired claim adopts: a
-- producer's own fail is final. producer is the agent the claim names.
CREATE FUNCTION catalog.adopt_orphan(hash text, objects jsonb, toolkit_commit text, env_fingerprint text,
                                     producer text, reason text, generation integer)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = catalog, pg_temp AS $$
DECLARE
  p catalog.product;
BEGIN
  IF coalesce(adopt_orphan.reason, '') = '' OR coalesce(producer, '') = ''
     OR coalesce(adopt_orphan.toolkit_commit, '') = '' OR coalesce(adopt_orphan.env_fingerprint, '') = '' THEN
    RAISE EXCEPTION 'adopt_orphan refused: name the producer, toolkit commit, environment fingerprint and reason'
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  SELECT * INTO p FROM catalog.product WHERE product.recipe_hash = hash FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'adopt_orphan refused: recipe % has no product row', hash
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  IF NOT (p.state = 'claimed' AND p.lease_until <= clock_timestamp()) THEN
    RAISE EXCEPTION 'adopt_orphan refused: recipe % is % (lease until %); only an expired claim adopts',
      hash, p.state, p.lease_until USING ERRCODE = 'invalid_parameter_value';
  END IF;
  IF p.owner <> producer THEN
    RAISE EXCEPTION 'adopt_orphan refused: the claim on recipe % names %, not %', hash, p.owner, producer
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  IF adopt_orphan.generation IS DISTINCT FROM p.generation THEN
    RAISE EXCEPTION 'adopt_orphan refused: the claim on recipe % is at generation %, not %', hash, p.generation,
      adopt_orphan.generation USING ERRCODE = 'invalid_parameter_value';
  END IF;
  PERFORM catalog._insert_objects(hash, objects, producer);
  UPDATE catalog.product
     SET state = 'committed', owner = producer, toolkit_commit = adopt_orphan.toolkit_commit,
         env_fingerprint = adopt_orphan.env_fingerprint, committed_at = now(), lease_until = NULL,
         reason = adopt_orphan.reason
   WHERE product.recipe_hash = hash;
  PERFORM catalog._log('adopt_orphan', hash, jsonb_build_object('producer', producer, 'previous_owner', p.owner,
                       'previous_state', p.state, 'objects', jsonb_array_length(objects),
                       'reason', adopt_orphan.reason));
  RETURN jsonb_build_object('status', 'committed', 'recipe_hash', hash, 'objects', catalog._objects(hash));
END $$;

-- Point (bucket, kind, subject) at a verified product of that bucket. expected_seq is the seq the caller read (0: no
-- pointer yet). On a mismatch nothing moves and the winner comes back:
-- {status: mismatch, current: {...} | null}; on success {status: moved, current: {...}}.
CREATE FUNCTION catalog.move_current(bucket text, kind text, subject text, hash text, expected_seq bigint,
                                     reason text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = catalog, pg_temp AS $$
DECLARE
  me text := catalog._authenticated();  -- the login that authenticated, never an impersonation
  p catalog.product;
  c catalog.current;
  previous text;
  moved boolean := false;
  human boolean := catalog._as_human();
BEGIN
  IF coalesce(move_current.reason, '') = '' THEN
    RAISE EXCEPTION 'move_current refused: give the reason' USING ERRCODE = 'invalid_parameter_value';
  END IF;
  -- FOR SHARE: a verify demoting this product finishes first, or waits until this move commits
  SELECT * INTO p FROM catalog.product WHERE product.recipe_hash = hash FOR SHARE;
  IF NOT FOUND OR p.state <> 'verified' THEN
    RAISE EXCEPTION 'move_current refused: recipe % is %; only a verified product becomes current',
      hash, coalesce(p.state, 'unknown') USING ERRCODE = 'invalid_parameter_value';
  END IF;
  IF p.bucket IS DISTINCT FROM move_current.bucket OR p.kind <> move_current.kind
     OR p.subject <> move_current.subject THEN
    RAISE EXCEPTION 'move_current refused: recipe % is a product of (%, %, %), not (%, %, %)',
      hash, p.bucket, p.kind, p.subject, move_current.bucket, move_current.kind, move_current.subject
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  -- Only the human's own login moves a pointer onto a product carrying a live flag (catalog.live_flag):
  -- never the reconciler, nor the runtime's superuser admin. A stamp landing meanwhile has finished
  -- (stamp_flags locks the product FOR UPDATE).
  IF EXISTS (SELECT FROM catalog.live_flag f WHERE f.recipe_hash = hash) AND NOT catalog._as_human() THEN
    RAISE EXCEPTION 'move_current refused: recipe % carries flags from the call that made it; only the human''s own login moves onto it', hash
      USING ERRCODE = 'invalid_parameter_value';
  END IF;

  IF expected_seq = 0 THEN
    BEGIN
      INSERT INTO catalog.current (bucket, kind, subject, recipe_hash, seq, moved_by, moved_at, reason, human_override)
      VALUES (move_current.bucket, move_current.kind, move_current.subject, hash, 1, me, now(), move_current.reason,
              human);
      moved := true;
    EXCEPTION WHEN unique_violation THEN
      moved := false;  -- another mover created the pointer first
    END;
  ELSE
    SELECT * INTO c FROM catalog.current cur
     WHERE cur.bucket = move_current.bucket AND cur.kind = move_current.kind
       AND cur.subject = move_current.subject FOR UPDATE;
    IF FOUND AND c.seq = expected_seq THEN
      previous := c.recipe_hash;
      UPDATE catalog.current cur
         SET recipe_hash = hash, seq = expected_seq + 1, moved_by = me, moved_at = now(),
             reason = move_current.reason, human_override = human
       WHERE cur.bucket = move_current.bucket AND cur.kind = move_current.kind
         AND cur.subject = move_current.subject;
      moved := true;
    END IF;
  END IF;

  SELECT * INTO c FROM catalog.current cur
   WHERE cur.bucket = move_current.bucket AND cur.kind = move_current.kind AND cur.subject = move_current.subject;
  IF NOT moved THEN
    PERFORM catalog._log('move_current.mismatch', hash, jsonb_build_object('bucket', move_current.bucket,
                         'kind', move_current.kind,
                         'subject', move_current.subject, 'expected_seq', expected_seq,
                         'winner_seq', c.seq, 'winner', c.recipe_hash));
    RETURN jsonb_build_object('status', 'mismatch', 'current', CASE WHEN c.seq IS NULL THEN NULL ELSE to_jsonb(c) END);
  END IF;
  INSERT INTO catalog.current_history (bucket, kind, subject, seq, recipe_hash, previous, moved_by, moved_at, reason,
                                       human_override)
  VALUES (move_current.bucket, move_current.kind, move_current.subject, c.seq, hash, previous, me, now(),
          move_current.reason, human);
  PERFORM catalog._log('move_current', hash, jsonb_build_object('bucket', move_current.bucket, 'kind', move_current.kind,
                       'subject', move_current.subject, 'seq', c.seq, 'previous', previous,
                       'reason', move_current.reason));
  RETURN jsonb_build_object('status', 'moved', 'current', to_jsonb(c));
END $$;

-- The reconciler's finding about the store (an object no row records, a recorded object missing or
-- changed on disk, a promotion it could not make): one event, action 'flag.<action>'. Idempotent: an
-- identical flag (action, recipe hash, detail) already logged is not logged again; returns whether
-- this call logged it.
CREATE FUNCTION catalog.flag(action text, hash text, detail jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = catalog, pg_temp AS $$
DECLARE
  name text := 'flag.' || flag.action;
BEGIN
  IF coalesce(flag.action, '') !~ '^[a-z_]+(\.[a-z_]+)*$' THEN
    RAISE EXCEPTION 'flag refused: action is lower-case words joined by dots' USING ERRCODE = 'invalid_parameter_value';
  END IF;
  IF detail IS NULL OR jsonb_typeof(detail) <> 'object' THEN
    RAISE EXCEPTION 'flag refused: detail is a JSON object' USING ERRCODE = 'invalid_parameter_value';
  END IF;
  -- serialises identical flags, so two reconcilers log one
  PERFORM pg_advisory_xact_lock(hashtext(name || coalesce(hash, '') || detail::text));
  IF EXISTS (SELECT FROM catalog.event e WHERE e.action = name
             AND (e.recipe_hash = hash OR (hash IS NULL AND e.recipe_hash IS NULL))
             AND e.detail = flag.detail) THEN
    RETURN false;
  END IF;
  PERFORM catalog._log(name, hash, detail);
  RETURN true;
END $$;

-- The reconciler's record of hashing one stored version (catalog.object_check).
CREATE FUNCTION catalog.record_check(store text, bucket text, key text, version_id text, size bigint,
                                     mtime timestamptz, sha256 text) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = catalog, pg_temp AS $$
  INSERT INTO catalog.object_check (store, bucket, key, version_id, size, mtime, sha256)
  VALUES ($1, $2, $3, $4, $5, $6, $7)
$$;

-- Stamp a call receipt's flags on a product that call claimed or committed (catalog.product_flag): one
-- row per flag new for (product, receipt) and one event, and the pointer taken off the product when it
-- is current (every stamped flag blocks promotion); returns how many were new. Refuses an unknown
-- product, an empty or unknown flag list and an empty receipt handle.
CREATE FUNCTION catalog.stamp_flags(hash text, flags text[], receipt text) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = catalog, pg_temp AS $$
#variable_conflict use_column
DECLARE
  added integer;
BEGIN
  -- FOR UPDATE: a move_current of this product (FOR SHARE) finishes first, or waits and sees the stamp
  PERFORM FROM catalog.product p WHERE p.recipe_hash = hash FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'stamp_flags refused: no product %', hash USING ERRCODE = 'invalid_parameter_value';
  END IF;
  IF coalesce(cardinality(flags), 0) = 0 OR EXISTS (
       SELECT FROM unnest(flags) f WHERE f IS NULL
         OR f NOT IN ('failed', 'synthetic', 'undeclared_write', 'unverified')) THEN
    RAISE EXCEPTION 'stamp_flags refused: flags are one or more of failed, synthetic, undeclared_write, unverified'
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  IF coalesce(receipt, '') = '' THEN
    RAISE EXCEPTION 'stamp_flags refused: name the receipt whose flags these are' USING ERRCODE = 'invalid_parameter_value';
  END IF;
  WITH new AS (
    INSERT INTO catalog.product_flag (recipe_hash, flag, receipt)
    SELECT hash, f, stamp_flags.receipt FROM (SELECT DISTINCT unnest(flags) AS f) d
    ON CONFLICT (recipe_hash, flag, receipt) DO NOTHING
    RETURNING flag
  )
  SELECT count(*) INTO added FROM new;
  IF added > 0 THEN
    PERFORM catalog._log('stamp_flags', hash, jsonb_build_object('flags', to_jsonb(flags), 'receipt', receipt));
    -- a stamp landing after the product became current takes the pointer off it (the promotion rule)
    PERFORM catalog._demote(hash, format('current product %s carries %s from %s', hash,
                                         array_to_string(flags, ', '), receipt));
  END IF;
  RETURN added;
END $$;

-- The human clears a flag it judged wrong (its own login in owner, ar_human; never the runtime, whose
-- admin is a superuser and is refused here): a `cleared` row with
-- the reason and one event naming every receipt whose stamp it covers (each stamp since the last
-- clear). Refuses an unknown product, a flag that is not live on it, and a clear without a reason.
-- The pointer is left alone: the next reconciler pass promotes as usual. Returns {flag, receipts}.
CREATE FUNCTION catalog.clear_flag(hash text, flag text, reason text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = catalog, pg_temp AS $$
#variable_conflict use_column
DECLARE
  covered jsonb;
BEGIN
  -- the human's own login (ar_human, in owner), never the runtime's superuser admin role, also not
  -- through SET SESSION AUTHORIZATION (the authenticated login is checked)
  IF NOT catalog._as_human() THEN
    RAISE EXCEPTION 'clear_flag refused: % is not the human''s own login; clear as it', catalog._authenticated()
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF btrim(coalesce(clear_flag.reason, '')) = '' THEN
    RAISE EXCEPTION 'clear_flag refused: give the reason' USING ERRCODE = 'invalid_parameter_value';
  END IF;
  PERFORM FROM catalog.product p WHERE p.recipe_hash = clear_flag.hash FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'clear_flag refused: no product %', clear_flag.hash USING ERRCODE = 'invalid_parameter_value';
  END IF;
  IF NOT EXISTS (SELECT FROM catalog.live_flag f
                 WHERE f.recipe_hash = clear_flag.hash AND f.flag = clear_flag.flag) THEN
    RAISE EXCEPTION 'clear_flag refused: product % carries no live flag %', clear_flag.hash, clear_flag.flag
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  SELECT coalesce(jsonb_agg(f.receipt ORDER BY f.id), '[]') INTO covered FROM catalog.product_flag f
   WHERE f.recipe_hash = clear_flag.hash AND f.flag = clear_flag.flag AND f.action = 'stamp'
     AND f.id > coalesce((SELECT max(c.id) FROM catalog.product_flag c WHERE c.recipe_hash = clear_flag.hash
                          AND c.flag = clear_flag.flag AND c.action = 'cleared'), 0);
  INSERT INTO catalog.product_flag (recipe_hash, flag, action, reason, stamped_by)
  VALUES (clear_flag.hash, clear_flag.flag, 'cleared', clear_flag.reason, catalog._authenticated());
  PERFORM catalog._log('clear_flag', clear_flag.hash, jsonb_build_object(
    'flag', clear_flag.flag, 'reason', clear_flag.reason, 'receipts', covered));
  RETURN jsonb_build_object('flag', clear_flag.flag, 'receipts', covered);
END $$;

-- ---------------------------------------------------------------- grants

GRANT USAGE ON SCHEMA catalog TO reader;
GRANT SELECT ON ALL TABLES IN SCHEMA catalog TO reader;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA catalog FROM PUBLIC;
GRANT EXECUTE ON FUNCTION catalog.recipe_hash(jsonb) TO reader;
GRANT EXECUTE ON FUNCTION catalog.claim(jsonb, interval) TO producer;
GRANT EXECUTE ON FUNCTION catalog.commit(text, jsonb, text, text) TO producer;
GRANT EXECUTE ON FUNCTION catalog.fail(text, text, boolean) TO producer;
GRANT EXECUTE ON FUNCTION catalog.verify(text, text, text) TO reconciler;
GRANT EXECUTE ON FUNCTION catalog.adopt_orphan(text, jsonb, text, text, text, text, integer) TO reconciler;
GRANT EXECUTE ON FUNCTION catalog.move_current(text, text, text, text, bigint, text) TO reconciler;
GRANT EXECUTE ON FUNCTION catalog.flag(text, text, jsonb) TO reconciler;
GRANT EXECUTE ON FUNCTION catalog.record_check(text, text, text, text, bigint, timestamptz, text) TO reconciler;
GRANT EXECUTE ON FUNCTION catalog.stamp_flags(text, text[], text) TO reconciler;

RESET ROLE;
