"""The configured toolkit against a store (step 0b, item S8).

* The toolkit's store helper (``storelib``, which imports nothing from ``auto_research``) writes what the
  reconciler reads: its contract constants equal ``store.reconcile``'s, read from its source as data.
* The toolkit's store tests pass against the CI pipeline's throwaway store (``store.ci.throwaway``: real
  versitygw + Postgres in temporary folders, mirroring the configured store), reached only through the
  variables the CI test run gets; what they store is then verified by the runtime's own reconciler pass.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from auto_research.config_paths import repo_config_dir
from auto_research.store import reconcile
from auto_research.toolkit_contract import toolkits

needs_store = pytest.mark.skipif(not all(shutil.which(b) for b in ("versitygw", "initdb", "pg_ctl")),
                                 reason="versitygw and Postgres are not installed here")

#: The toolkit's test folders that reach a store.
STORE_TESTS = ("storelib/tests", "heasarc_utils/tests", "catalog_utils/tests")


def _repo_toolkits() -> list[dict]:
    """The configured ``toolkits:`` block of the repository's ``orchestration.yaml``."""
    return yaml.safe_load((repo_config_dir() / "orchestration.yaml").read_text(encoding="utf-8"))["toolkits"]


def _toolkit_root() -> Path:
    return toolkits({"toolkits": _repo_toolkits()})[0].root


def _constants(source: Path) -> dict:
    """The module-level constants ``source`` assigns from literals (read, never run)."""
    out = {}
    for node in ast.parse(source.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue
    return out


def test_the_toolkit_helper_writes_what_the_reconciler_reads():
    contract = _constants(_toolkit_root() / "storelib" / "contract.py")
    for name in ("META_RECIPE_HASH", "META_SHA256", "META_PRODUCER", "META_TOOLKIT_COMMIT", "META_ENV_FINGERPRINT",
                 "RECIPE_FILE"):
        assert contract[name] == getattr(reconcile, name), name
    assert sorted(contract["RECIPE_FILE_KEYS"]) == sorted(reconcile.RecipeFile.model_fields)
    assert sorted(contract["RECIPE_OBJECT_KEYS"]) == sorted(reconcile.RecipeObject.model_fields)
    # The recipe and record shapes catalog.claim checks (catalog.sql's _recipe_problem).
    sql = (Path(reconcile.__file__).parent / "catalog.sql").read_text(encoding="utf-8")
    def listed(pattern: str) -> list[str]:
        return sorted(re.search(pattern, sql).group(1).replace("'", "").replace(" ", "").split(","))

    assert sorted(contract["RECIPE_KEYS"]) == listed(r"IS DISTINCT FROM ARRAY\[([^\]]*)\] THEN\s*RETURN 'a recipe holds")
    assert sorted(contract["RECORD_KEYS"]) == listed(
        r"IS DISTINCT FROM ARRAY\[([^\]]*)\]\s*OR EXISTS \(SELECT FROM jsonb_each\(inp\)")
    from auto_research.provenance.inputs import RECORD_KEYS

    assert sorted(contract["RECORD_KEYS"]) == sorted(RECORD_KEYS)


def _archive_python(root: Path, config: dict) -> Path:
    """The interpreter of the environment heasarc_utils and catalog_utils name (envs/archive/: astroquery,
    boto3 + awscrt, s3fs, psycopg), resolved as the launch resolves it: its verified prefix, or built."""
    from auto_research.env import agent_conda_dir, conda_executable, declared_externals, human_environments
    from auto_research.sandbox.env_build import build, read_definition

    return build(read_definition(root / "envs" / "archive"), conda_dir=agent_conda_dir(),
                 conda_exe=conda_executable(), base_envs=human_environments(config),
                 externals=declared_externals(config), package_archive=agent_conda_dir() / "pkgs-archive").python


@needs_store
def test_the_toolkits_store_tests_pass_against_a_throwaway_store(tmp_path):
    from auto_research.store import ci
    from auto_research.store import config as store_config

    root = _toolkit_root()
    config = yaml.safe_load((repo_config_dir() / "orchestration.yaml").read_text(encoding="utf-8"))
    mirror = ci.mirror_of(config)
    python = _archive_python(root, config)
    with ci.throwaway(mirror) as made:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "PG", "AR_", "TOOLKIT_STORE"))}
        env.update(made.env)
        env.update({"PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1",
                    "AR_TOOLKIT_COMMIT": "1" * 40, "AR_ENV_FINGERPRINT": "s8-test-environment"})
        argv = [str(python), "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", "--rootdir", str(root),
                *(str(root / t) for t in STORE_TESTS)]
        done = subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=900)
        report = done.stdout + done.stderr
        assert done.returncode == 0, report[-6000:]
        assert re.search(r"\d+ passed", report) and "skipped" not in report, report[-3000:]

        # The runtime's reconciler verifies what the toolkit stored.
        store_config.reset()
        passed = asyncio.run(reconcile.reconcile(made.store, run=made.run))
        assert passed.verified and not passed.failed and not passed.nondeterministic, passed.as_dict()
        import psycopg

        with psycopg.connect(made.store.admin_dsn()) as conn:
            left = conn.execute("SELECT recipe_hash, state FROM catalog.product WHERE state = 'committed'").fetchall()
        assert left == [], f"committed products the reconciler did not verify: {left}"


_TWO_BUCKETS = """
import storelib

def make(work):
    (work / "x.txt").write_text("x")

for bucket in ("ixpe", "nicer"):
    storelib.produce(bucket=bucket, kind="raw", subject="S1", tool="research_toolkit.heasarc_utils.acquire",
                     algorithm_version="1", settings={"mission": bucket}, inputs=[], make=make)
"""


@needs_store
def test_two_missions_sharing_a_subject_each_list_their_current_product_once(tmp_path):
    """The current pointer is keyed (bucket, kind, subject): two buckets' products of one subject, both
    made current by the runtime's own reconciler pass, are each listed once, marked current, under their
    bucket (the toolkit's data_catalog, run in its own environment as a tool process would)."""
    from auto_research.provenance.capture import toolkit_commit
    from auto_research.store import ci
    from auto_research.store import config as store_config

    root = _toolkit_root()
    config = yaml.safe_load((repo_config_dir() / "orchestration.yaml").read_text(encoding="utf-8"))
    python = _archive_python(root, config)
    with ci.throwaway(ci.mirror_of(config)) as made:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "PG", "AR_", "TOOLKIT_STORE"))}
        env.update(made.env)
        # A commit of the authoring branch, so the reconciler qualifies the products for "current".
        env.update({"PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1",
                    "AR_TOOLKIT_COMMIT": toolkit_commit(root), "AR_ENV_FINGERPRINT": "s8-test-environment"})
        script = tmp_path / "two_buckets.py"
        script.write_text(_TWO_BUCKETS, encoding="utf-8")
        argv = [str(python), str(script)]
        stored = subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
        assert stored.returncode == 0, stored.stderr[-3000:]

        store_config.reset()
        passed = asyncio.run(reconcile.reconcile(made.store, run=made.run))
        moved = {(m["bucket"], m["kind"], m["subject"]) for m in passed.promoted}
        assert moved == {("ixpe", "raw", "S1"), ("nicer", "raw", "S1")}, passed.as_dict()

        listing = subprocess.run([str(python), "-c", "from catalog_utils.lib.catalog_read import data_catalog; "
                                  "print(data_catalog(subject='S1'))"],
                                 cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
        assert listing.returncode == 0, listing.stderr[-3000:]
    lines = listing.stdout.splitlines()
    assert lines[0] == "Stored products (2):", listing.stdout
    products = [line for line in lines if line.startswith("- ")]
    assert [line.split(": record ")[0] for line in products] == ["- S1 raw [current, 1 files]"] * 2, listing.stdout
    buckets = [json.loads(line.split(": record ", 1)[1])["bucket"] for line in products]
    assert buckets == ["ixpe", "nicer"] and lines[1] == "ixpe:" and lines[3] == "nicer:", listing.stdout
