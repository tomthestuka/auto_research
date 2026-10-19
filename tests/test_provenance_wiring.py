"""Provenance through the real tool providers and turn_end (no LLM): call → receipts → report."""

import asyncio
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

import auto_research.agents.react as react
from auto_research.agents.react import (
    ReportWorkspace,
    SandboxToolProvider,
    execute_edit_report,
    execute_write_report,
    lead_turn_problems,
    worker_report_problems,
)
from auto_research.provenance import ReceiptStore, ScopedLookup
from auto_research.schemas import AnalystReport
from tests.test_sandbox import _FakeRegistry, _FakeSandbox


@pytest.fixture(autouse=True)
def _allow_list_is_the_test_folder(monkeypatch, tmp_path):
    """Receipt minting only admits files inside the allow-list (F22); pin it, not the host's."""
    from auto_research.sandbox import fs_guard

    monkeypatch.setattr(fs_guard, "load_fs_allowlist", lambda config=None: [tmp_path.resolve()])


@pytest.fixture(autouse=True)
def _no_data_roots(monkeypatch):
    monkeypatch.setattr(react, "_observation_path_args", lambda: {})


def _provider(tmp_path, store, tool_fn, schema=None):
    reg = _FakeRegistry(known={"research_toolkit.tk.fit"}, mcp=set(), schema=schema or {"properties": {}})
    return SandboxToolProvider(
        reg, _FakeSandbox(tool_fn=tool_fn), cwd=tmp_path,
        receipts=store, receipt_source="analyst_worker_0",
    )


def _handles(text):
    return re.findall(r"\[receipt ([^\]]+)\]|\[([^\]\s]+:[frd]\d+)\]", text)


def test_a_toolkit_call_is_receipted_and_the_model_sees_its_handle(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    prov = _provider(tmp_path, store, lambda m, f, a: {"exit_code": 0, "stdout": "PD=3.453 +/- 0.194", "stderr": ""})
    out = asyncio.run(prov.execute("research_toolkit.tk.fit", {}))
    assert out.text.startswith("[receipt analyst_worker_0:r1]")
    assert store.text("analyst_worker_0:r1") == "PD=3.453 +/- 0.194"


def test_figure_hash_is_the_files_not_the_tools_and_a_missing_figure_is_dropped(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    png = tmp_path / "plot.png"
    png.write_bytes(b"\x89PNG real bytes")
    now = datetime.now(timezone.utc).isoformat()
    figs = [
        {"path": str(png), "caption": "PD vs E", "mime_type": "image/png", "produced_by": "research_toolkit.tk.plot",
         "created_at": now, "sha256": "a" * 64},
        {"path": str(tmp_path / "never.png"), "caption": "ghost", "mime_type": "image/png",
         "produced_by": "research_toolkit.tk.plot", "created_at": now, "sha256": "b" * 64},
    ]
    prov = _provider(tmp_path, store, lambda m, f, a: {"exit_code": 0, "stdout": "ok", "figures": figs})
    out = asyncio.run(prov.execute("research_toolkit.tk.fit", {}))
    assert [f.caption for f in out.figures] == ["PD vs E"]
    assert out.figures[0].sha256 == hashlib.sha256(png.read_bytes()).hexdigest()
    assert "does not match the file" in out.text and "ghost" in out.text
    assert "[analyst_worker_0:f1] PD vs E" in out.text


def test_a_written_file_is_a_data_receipt_and_links_the_next_call_as_its_child(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    out_csv = tmp_path / "slices.csv"

    def write(m, f, a):
        Path(a["out_path"]).write_text("t,pd\n0,3.4\n")
        return {"exit_code": 0, "stdout": f"wrote {a['out_path']}"}

    prov = _provider(tmp_path, store, write)
    first = asyncio.run(prov.execute("research_toolkit.tk.fit", {"out_path": str(out_csv)}))
    assert "[analyst_worker_0:d1] slices.csv" in first.text

    prov2 = _provider(tmp_path, store, lambda m, f, a: {"exit_code": 0, "stdout": "mean 3.4"})
    asyncio.run(prov2.execute("research_toolkit.tk.fit", {"csv_path": str(out_csv)}))
    child = store.get("analyst_worker_0:r2")
    assert child.parents == ["analyst_worker_0:d1"]


def _workspace(tmp_path, lookup, source="analyst_worker_0"):
    return ReportWorkspace(
        report_path=tmp_path / "report.jsonl", report_model=AnalystReport, catalog={"self": []},
        receipts=lookup, receipt_source=source,
    )


def test_worker_turn_end_refuses_an_ungrounded_number_then_accepts_it_cited(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    prov = _provider(tmp_path, store, lambda m, f, a: {"exit_code": 0, "stdout": "PD=3.453 +/- 0.194 %"})
    asyncio.run(prov.execute("research_toolkit.tk.fit", {}))
    ws = _workspace(tmp_path, ScopedLookup(store, own_source="analyst_worker_0"))

    draft = asyncio.run(execute_write_report(ws, {"content": (
        "# summary\nPolarisation measured.\n# status\nsucceeded\n# findings\n- PD is 3.45 %.\n"
    )}))
    assert "Provenance" in draft.text
    assert any('"3.45"' in p for p in worker_report_problems(ws))

    asyncio.run(execute_edit_report(ws, {  # A7: fix the one finding with an Edit
        "old_string": "- PD is 3.45 %.", "new_string": "- PD is 3.453 ± 0.194 % [analyst_worker_0:r1].",
    }))
    assert worker_report_problems(ws) == []


def test_a_worker_cannot_ground_a_number_in_a_peers_unseen_receipt(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    peer = store.mint(source="analyst_worker_1", kind="result", produced_by="research_toolkit.tk.fit", data="PD=5.1")
    ws = _workspace(tmp_path, ScopedLookup(store, own_source="analyst_worker_0"))
    asyncio.run(execute_write_report(ws, {
        "content": f"# summary\ns\n# status\nsucceeded\n# findings\n- PD 5.1 [{peer.handle}]\n",
    }))
    assert any("not a receipt you can cite" in p for p in worker_report_problems(ws))


def test_the_lead_may_cite_what_a_received_worker_report_cites(tmp_path):
    from auto_research.agents.roles import _receipt_scope

    store = ReceiptStore(tmp_path / "receipts")
    r = store.mint(source="analyst_worker_0", kind="result", produced_by="research_toolkit.tk.fit", data="PD=3.453")
    worker_report = AnalystReport(summary="s", findings=[f"PD 3.453 [{r.handle}]"], status="succeeded")
    ws = _workspace(tmp_path, _receipt_scope(store, "analyst_lead", [worker_report]), "analyst_lead")
    ws.prose_fields = react.LEAD_REPORT_FIELDS
    asyncio.run(execute_write_report(ws, {
        "content": f"# summary\nAdjudicated.\n# status\nsucceeded\n# findings\n- PD 3.453 [{r.handle}]\n",
    }))
    assert lead_turn_problems(None, ws) == []


def test_a_submit_that_commits_mints_a_commit_receipt_the_lead_may_cite(tmp_path):
    """Human ruling 2026-09-25: the coding pipeline's outcome is evidence, written by the runtime."""
    from types import SimpleNamespace

    from auto_research.agents.react import ReadOnlyToolProvider, ToolOutcome
    from auto_research.provenance import ReceiptStore, check_bindings
    from auto_research.provenance.store import is_evidence

    store = ReceiptStore(tmp_path / "receipts")
    prov = ReadOnlyToolProvider(roots=[], receipts=store, receipt_source="analyst_lead")
    session = SimpleNamespace(last_commit_sha="da3ba8b909aa", branch="ai", submits_used=2,
                              submit_cap=5, tool_spec=SimpleNamespace(proposed_tool_name="fit_pol"))
    out = prov._commit_receipted(session, None, ToolOutcome(text="committed"))
    (commit,) = store.receipts(kind="commit")
    assert is_evidence(commit) and out.text.startswith(f"[receipt {commit.handle}]")
    assert check_bindings(f"committed in 2 of 5 submissions [{commit.handle}]", store).problems == []
    # A submit that did not commit (the SHA did not move) mints nothing.
    prov._commit_receipted(session, "da3ba8b909aa", ToolOutcome(text="rejected"))
    assert len(store.receipts(kind="commit")) == 1


def test_the_leads_in_process_data_catalog_is_receipted_as_the_toolkit_tool(tmp_path, monkeypatch):
    import asyncio

    from auto_research.agents import react
    from auto_research.agents.react import DATA_CATALOG_TOOL, ReadOnlyToolProvider, ToolOutcome
    from auto_research.provenance import ReceiptStore
    from auto_research.provenance.store import is_evidence

    async def fake_catalog(arguments):
        return ToolOutcome(text="01002901 level 2")

    monkeypatch.setattr(react, "execute_data_catalog", fake_catalog)
    store = ReceiptStore(tmp_path / "receipts")
    prov = ReadOnlyToolProvider(roots=[], include_data_catalog=True, receipts=store,
                                receipt_source="analyst_lead")
    asyncio.run(prov.execute(DATA_CATALOG_TOOL, {}))
    (r,) = store.receipts()
    assert r.produced_by == "research_toolkit.catalog_utils.data_catalog" and is_evidence(r)
