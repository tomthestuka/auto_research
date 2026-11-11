"""The lead's catalog query (``catalog_query``, step 0b S9) on a throwaway local store: real
versitygw (+ IAM) and Postgres in temporary folders.

Driven through the run path's own seams: the lead's login is issued by
``graph.runtime.issue_unit_keys`` (as ``run_research`` issues it; the store's default ``roles`` make
it a reader), its tools are built by
``attach_lead_runtime``, and every query goes through ``ReadOnlyToolProvider.execute``. Products are
made by a producer login through the catalog's own SQL functions, as the toolkit's helper makes them.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import psycopg
import pytest

from auto_research.store import keys
from tests.test_store_servers import SECRETS, _no_dotenv, make_store  # noqa: F401 - fixtures

pytestmark = pytest.mark.skipif(
    not __import__("shutil").which("versitygw"), reason="versitygw and Postgres are not installed here")

RUN = "run-s9"


def _recipe(kind: str, subject: str, tool: str = "demo.demo_utils.reduce") -> dict:
    return {"kind": kind, "subject": subject, "tool": tool, "algorithm_version": "1", "settings": {"bin": 2},
            "inputs": []}


def _make(producer: keys.Credentials, recipe: dict, objects: list[tuple[str, str]]) -> str:
    """One product committed by ``producer`` through ``catalog.claim``/``catalog.commit``; its hash."""
    with psycopg.connect(producer.dsn(), autocommit=True) as conn:
        claim = conn.execute("SELECT catalog.claim(%s::jsonb)", (json.dumps(recipe),)).fetchone()[0]
        rows = [{"store": "db", "bucket": "ixpe", "key": key, "version_id": version, "sha256": "a" * 64, "size": 3}
                for key, version in objects]
        conn.execute("SELECT catalog.commit(%s, %s::jsonb, %s, %s)",
                     (claim["recipe_hash"], json.dumps(rows), "c0ffee", "fp1"))
    return claim["recipe_hash"]


@pytest.fixture
def world(make_store, tmp_path, monkeypatch):
    """A running store whose catalog holds three products (two of one kind and subject, one of them
    verified and current), the lead attached as ``run_research`` attaches it, and the run's keys."""
    import yaml

    from auto_research.config_paths import orchestration_yaml
    from auto_research.graph import runtime
    from auto_research.monitor import RunMonitor
    from auto_research.provenance import ReceiptStore
    from auto_research.store import launch

    from tests.fixtures import toolkit as fixture

    monkeypatch.setenv("AUTO_RESEARCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    root = fixture.build_toolkit(tmp_path / "toolkit", env_base="astropy")
    declared = {"name": fixture.TOOLKIT_NAME, "root": str(root), "package_glob": "*_utils",
                "template_package": fixture.PKG_NAME, "authoring_branch": fixture.BRANCH,
                "default_env": fixture.ENV_ID}
    store = make_store(extra_config={"toolkits": [declared]})
    launch.start(store)
    held = keys.RunKeys(RUN)
    runtime.bind_run_stores(yaml.safe_load(orchestration_yaml().read_text()), RUN)
    try:
        producer = asyncio.run(held.issue(store, "analyst_worker_0", "producer"))
        old = _make(producer, _recipe("spectrum", "s1"), [("spectrum/s1/old/a.dat", "v1")])
        new = _make(producer, {**_recipe("spectrum", "s1"), "settings": {"bin": 4}},
                    [("spectrum/s1/new/a.dat", "v2"), ("spectrum/s1/new/recipe.json", "v3")])
        other = _make(producer, _recipe("events", "s2"), [("events/s2/x/e.dat", "v4")])
        with psycopg.connect(store.admin_dsn(), autocommit=True) as conn:  # the human's admin role
            conn.execute("SELECT catalog.verify(%s)", (new,))
            conn.execute("SELECT catalog.move_current('ixpe', 'spectrum', 's1', %s, 0, 'test')", (new,))
        monitor = RunMonitor(jsonl_path=tmp_path / "ledger" / "monitor.jsonl")
        lead_keys = asyncio.run(runtime.issue_unit_keys(held, ["analyst_lead"], monitor))
        lead = SimpleNamespace(tools=None)
        runtime.attach_lead_runtime(lead, run_id=RUN, toolkit_root=root,
                                    catalog={c.store: c for c in lead_keys.values()})
        receipts = lead.tools.receipts  # the run's one receipt store, as production wires it
        assert isinstance(receipts, ReceiptStore) and lead.tools.receipt_source == "analyst_lead"
        yield SimpleNamespace(store=store, held=held, lead=lead, login=lead_keys["analyst_lead"],
                              receipts=receipts, old=old, new=new, other=other, ledger=tmp_path / "ledger")
    finally:
        asyncio.run(held.revoke_all())
        runtime.release_run_stores(RUN)


def _ask(world, **arguments):
    out = asyncio.run(world.lead.tools.execute("catalog_query", arguments))
    if out.is_error:
        return out, None
    return out, json.loads(out.text.split("\n", 1)[1])  # under the receipt's header line


def test_the_lead_holds_a_reader_login_and_declares_the_query(world):
    assert world.login.role_class == "reader" and world.login.unit == "analyst_lead"
    names = [d["name"] for d in world.lead.tools.tool_definitions()]
    assert names.count("catalog_query") == 1
    with psycopg.connect(world.login.dsn()) as conn:  # the login itself writes nothing
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT catalog.claim(%s::jsonb)", (json.dumps(_recipe("k", "s")),))
    ledger = world.ledger.joinpath("monitor.jsonl").read_text()
    assert world.login.pg_password not in ledger and world.login.secret_access_key not in ledger


def test_products_are_listed_newest_first_with_their_state_and_current_mark(world):
    out, answer = _ask(world)
    assert answer["view"] == "products" and answer["matched"] == 3 and answer["store"] == "db"
    rows = {r["recipe_hash"]: r for r in answer["rows"]}
    assert [r["recipe_hash"] for r in answer["rows"]] == [world.other, world.new, world.old]
    assert rows[world.new]["state"] == "verified" and rows[world.new]["current"] is True
    assert rows[world.old]["current"] is False and rows[world.old]["settings"] == {"bin": 2}
    assert rows[world.new]["objects"] == 2 and rows[world.new]["tool"] == "demo.demo_utils.reduce"
    assert rows[world.new]["inputs"] == 0 and rows[world.new]["flags"] == []  # a count in the list
    # Filters and the limit: only what was asked, with how many matched.
    _, answer = _ask(world, kind="spectrum", state="verified")
    assert [r["recipe_hash"] for r in answer["rows"]] == [world.new]
    _, answer = _ask(world, kind="spectrum", limit=1)
    assert answer["matched"] == 2 and answer["shown"] == 1
    _, answer = _ask(world, recipe_hash=world.old[:8])
    assert [r["recipe_hash"] for r in answer["rows"]] == [world.old]
    _, answer = _ask(world, bucket="ixpe", kind="spectrum")
    assert answer["matched"] == 2 and {r["bucket"] for r in answer["rows"]} == {"ixpe"}
    _, answer = _ask(world, bucket="nicer")
    assert answer["matched"] == 0  # every product of the fixture lies in ixpe


def test_current_and_objects_answer_records_a_brief_can_name(world):
    from auto_research.schemas import ValidationDataset

    _, answer = _ask(world, view="current")
    assert [(r["bucket"], r["kind"], r["subject"], r["recipe_hash"]) for r in answer["rows"]] == [
        ("ixpe", "spectrum", "s1", world.new)]
    _, answer = _ask(world, view="current", bucket="nicer")
    assert answer["matched"] == 0  # the pointer is keyed by its bucket
    _, answer = _ask(world, view="objects", recipe_hash=world.new[:8])
    product = answer["product"]
    assert product["recipe_hash"] == world.new and answer["matched"] == 2
    assert product["current"] is True and product["state"] == "verified" and product["flags"] == []
    assert product["inputs"] == [] and product["settings"] == {"bin": 4}
    records = [ValidationDataset.model_validate({k: r[k] for k in ("store", "bucket", "key", "version_id")})
               for r in answer["rows"]]
    assert [(r.key, r.version_id) for r in records] == [("spectrum/s1/new/a.dat", "v2"),
                                                       ("spectrum/s1/new/recipe.json", "v3")]


def test_the_answer_is_a_receipt_of_where_the_lead_looked_never_evidence(world):
    from auto_research.provenance.store import is_evidence

    out, _ = _ask(world, view="current")
    (receipt,) = world.receipts.receipts()
    assert receipt.produced_by == "catalog_query" and not is_evidence(receipt)
    assert out.text.startswith(f"[receipt {receipt.handle}]")


@pytest.mark.parametrize("arguments, said", [
    ({"view": "everything"}, "view is one of products, current, objects"),
    ({"view": "objects"}, "view objects names the product: give its recipe_hash"),
    ({"recipe_hash": "abc"}, "its first 8 or more hex digits"),
    ({"limit": 0}, "limit is a whole number from 1 to 200"),
    ({"state": "done"}, "state is one of claimed, committed, verified, failed, nondeterministic"),
    ({"path": "/x"}, "catalog_query takes store, view"),
    ({"store": "elsewhere"}, "name the store to read, one of db"),
    ({"view": "objects", "recipe_hash": "f" * 64}, "no product's recipe hash starts with"),
    ({"view": "objects", "recipe_hash": "f" * 8, "kind": "spectrum"}, "names one product by its recipe_hash only"),
    ({"kind": ""}, "kind is a non-empty string"),
])
def test_a_query_the_catalog_cannot_answer_says_what_to_write(world, arguments, said):
    out, _ = _ask(world, **arguments)
    assert out.is_error and said in out.text, out.text
    assert world.receipts.receipts() == []  # an error result mints nothing


def test_a_catalog_that_does_not_answer_is_the_calls_error(world):
    from auto_research.store import launch

    launch.stop(world.store)
    try:
        out, _ = _ask(world)
    finally:
        launch.start(world.store)  # the run's keys are revoked through it
    assert out.is_error and "store db's catalog did not answer" in out.text
    assert world.login.pg_password not in out.text


def test_kind_and_subject_match_exactly_and_a_null_limit_is_the_default(world):
    _, answer = _ask(world, kind=" spectrum ")
    assert answer["matched"] == 0  # opaque strings: never trimmed into another kind
    _, answer = _ask(world, limit=None)
    assert answer["matched"] == 3


def test_the_leads_login_ends_with_the_runs_keys(world):
    asyncio.run(world.held.revoke("analyst_lead"))
    out, _ = _ask(world)
    assert out.is_error and "store db's catalog did not answer" in out.text


def test_the_reviewer_reads_no_catalog(world, tmp_path):
    from auto_research.graph.runtime import readonly_tools_for

    reviewer = readonly_tools_for("reviewer", roots=[tmp_path], catalog={"db": world.login})
    assert "catalog_query" not in [d["name"] for d in reviewer.tool_definitions()]
