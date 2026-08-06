"""Deterministic tests for the reviewer knowledge-base tools and the _react
document-threading contract (Phase-2 reviewer-driven retrieval).

Covers: the KB tools (`text_search` + `open_paper`) are scoped together; `open_paper`
validates path/PDF and records opened paths; and `_react` accumulates a tool's documents
and re-attaches them to every subsequent model call.
"""

import asyncio

import pytest

from auto_research.agents.react import (
    ReadOnlyToolProvider,
    SandboxToolProvider,
    ToolOutcome,
    _OPEN_PAPER_MAX_MB,
)
from auto_research.agents.roles import ReviewerAgent
from auto_research.models import ModelResponse, ToolCall
from auto_research.sandbox.fs_guard import load_fs_allowlist
from auto_research.schemas import HumanResponse, document_from_file


# ── filesystem jail for ReAct agents (todo #3 Stage C) ───────────────────────────


def test_readonly_provider_denies_governance_files(tmp_path):
    """Reviewer/lead can read source inside a root, but never governance files (§4)."""
    (tmp_path / "CLAUDE.md").write_text("governance — off-limits")
    (tmp_path / "notes.py").write_text("x = 1\n")
    p = ReadOnlyToolProvider(roots=[tmp_path])
    assert asyncio.run(p.execute("read_file", {"path": str(tmp_path / "CLAUDE.md")})).is_error
    assert asyncio.run(p.execute("read_file", {"path": str(tmp_path / "DOCS.md")})).is_error
    assert not asyncio.run(p.execute("read_file", {"path": str(tmp_path / "notes.py")})).is_error


def test_worker_sandbox_guard_blocks_auto_research(tmp_path):
    """Workers' builtin file tools are jailed to the allow-list; auto_research is denied."""
    if not load_fs_allowlist():
        pytest.skip("agent_fs_allow not configured — jail inactive")

    class _Reg:
        def kind_for_function(self, name):
            return f"mcp.builtin.{name}"

    p = SandboxToolProvider(_Reg(), sandbox=None, cwd=tmp_path)  # guard denies before sandbox use
    out = asyncio.run(p.execute(
        "bash_exec", {"command": "cat /data/fzhou/auto_research/src/auto_research/sandbox/gate.py"}))
    assert out.is_error and "not permitted" in out.text
    out2 = asyncio.run(p.execute("read_file", {"path": "/data/fzhou/auto_research/x.py"}))
    assert out2.is_error and "not permitted" in out2.text


def _names(provider):
    return {d["name"] for d in provider.tool_definitions()}


def test_kb_tools_scoped_together(tmp_path):
    """text_search + open_paper are gated together: reviewer has both; lead/co-lead neither."""
    reviewer = ReadOnlyToolProvider(roots=[tmp_path])  # include_text_search default True
    assert {"text_search", "open_paper"} <= _names(reviewer)

    lead = ReadOnlyToolProvider(roots=[tmp_path], include_text_search=False)
    assert not ({"text_search", "open_paper"} & _names(lead))


def test_open_paper_attaches_pdf_and_records_path(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.5\nbody")
    provider = ReadOnlyToolProvider(roots=[tmp_path])

    out = asyncio.run(provider.execute("open_paper", {"path": str(pdf)}))

    assert not out.is_error
    assert len(out.documents) == 1
    assert out.documents[0].sha256
    assert provider.opened_paths == [pdf.resolve()]


def test_open_paper_rejects_out_of_root(tmp_path):
    provider = ReadOnlyToolProvider(roots=[tmp_path])
    out = asyncio.run(provider.execute("open_paper", {"path": "/etc/passwd"}))
    assert out.is_error
    assert provider.opened_paths == []


def test_open_paper_rejects_non_pdf(tmp_path):
    fake = tmp_path / "x.pdf"
    fake.write_bytes(b"not a pdf at all")
    provider = ReadOnlyToolProvider(roots=[tmp_path])
    out = asyncio.run(provider.execute("open_paper", {"path": str(fake)}))
    assert out.is_error
    assert provider.opened_paths == []


def test_open_paper_rejects_oversized_pdf(tmp_path):
    """A PDF over the inline size limit is refused with its size named and NOT attached, so an
    ultra-high-resolution paper cannot blow the provider's request-size cap (the live 413)."""
    big = tmp_path / "huge.pdf"
    big.write_bytes(b"%PDF-1.5\n" + b"0" * ((_OPEN_PAPER_MAX_MB + 1) * 1024 * 1024))
    provider = ReadOnlyToolProvider(roots=[tmp_path])

    out = asyncio.run(provider.execute("open_paper", {"path": str(big)}))

    assert out.is_error
    assert not out.documents  # nothing attached by value
    assert "too large" in out.text and f"{_OPEN_PAPER_MAX_MB} MB" in out.text
    assert provider.opened_paths == []  # not counted against the open cap either


def test_react_threads_documents_into_subsequent_calls(tmp_path):
    """A document a tool attaches is re-sent on every later model call and returned."""
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF-1.5\n")
    doc = document_from_file(pdf)

    class _FakeTools:
        def tool_definitions(self):
            return [
                {
                    "name": "open_paper",
                    "description": "d",
                    "input_schema": {"type": "object", "properties": {}, "required": []},
                }
            ]

        async def execute(self, name, arguments):
            return ToolOutcome(text="attached", documents=[doc])

    class _FakeModel:
        def __init__(self):
            self.documents_per_call: list[list] = []
            self._calls = 0

        async def call(self, messages, *, schema=None, tools=None, tool_choice="auto",
                       images=(), documents=(), cache_prefix=False):
            self.documents_per_call.append(list(documents))
            self._calls += 1
            if self._calls == 1:
                return ModelResponse(
                    tool_calls=[ToolCall(id="c1", name="open_paper", arguments={"path": str(pdf)})]
                )
            return ModelResponse(content="done")

    model = _FakeModel()
    agent = ReviewerAgent(model=model, prompt="p")

    messages, figures, documents = asyncio.run(
        agent._react(seed_messages=[], tools=_FakeTools(), action="review", cap=3)
    )

    # First call saw no documents; the second (after open_paper) saw the attached PDF.
    assert model.documents_per_call[0] == []
    assert [d.sha256 for d in model.documents_per_call[1]] == [doc.sha256]
    # The accumulated documents are returned for the terminal structured call.
    assert [d.sha256 for d in documents] == [doc.sha256]


# ── §5.5 budget-free design channel + plan_and_debate tool ──────────────────────


class _LoopTools:
    """A no-op ToolProvider that always succeeds — for budget-accounting tests."""

    def tool_definitions(self):
        return [{"name": "t", "description": "d",
                 "input_schema": {"type": "object", "properties": {}, "required": []}}]

    async def execute(self, name, arguments):
        return ToolOutcome(text="ok")


class _LoopModel:
    """Always emits one tool call (of a fixed name); counts model invocations."""

    def __init__(self, tool_name):
        self.tool_name = tool_name
        self.calls = 0

    async def call(self, messages, *, schema=None, tools=None, tool_choice="auto",
                   images=(), documents=(), cache_prefix=False):
        self.calls += 1
        return ModelResponse(
            tool_calls=[ToolCall(id=f"c{self.calls}", name=self.tool_name, arguments={})]
        )


def _react(model, **kwargs):
    agent = ReviewerAgent(model=model, prompt="p")
    asyncio.run(agent._react(seed_messages=[], tools=_LoopTools(), action="a", **kwargs))


def test_react_paid_tools_bounded_by_cap():
    """With no free_tools, every iteration consumes budget — identical to range(cap)."""
    model = _LoopModel("paid")
    _react(model, cap=3)
    assert model.calls == 3


def test_react_free_tool_does_not_consume_budget():
    """A free-only iteration never increments the budget; the run is bounded by absolute_cap."""
    model = _LoopModel("plan_and_debate")
    _react(model, cap=3, free_tools=frozenset({"plan_and_debate"}), absolute_cap=7)
    assert model.calls == 7  # budget never spent → only the absolute backstop bounds it


def test_react_absolute_cap_defaults_to_cap_when_unset():
    """Free tools without an explicit absolute_cap stay bounded by cap (no opt-in = unchanged)."""
    model = _LoopModel("plan_and_debate")
    _react(model, cap=3, free_tools=frozenset({"plan_and_debate"}))
    assert model.calls == 3


def test_plan_and_debate_exposed_only_with_flag_and_handler(tmp_path):
    async def handler(query):
        return HumanResponse(query_id=query.id, answer="ok")

    lead = ReadOnlyToolProvider(
        roots=[tmp_path], include_plan_and_debate=True, human_handler=handler
    )
    assert "plan_and_debate" in _names(lead)
    # Flag set but no handler → absent (it needs the human channel).
    assert "plan_and_debate" not in _names(
        ReadOnlyToolProvider(roots=[tmp_path], include_plan_and_debate=True)
    )
    # Handler but no flag (the reviewer) → absent (lead-only).
    assert "plan_and_debate" not in _names(
        ReadOnlyToolProvider(roots=[tmp_path], human_handler=handler)
    )


def test_plan_and_debate_routes_message_to_human(tmp_path):
    seen = {}

    async def handler(query):
        seen["query"] = query
        return HumanResponse(query_id=query.id, answer="extend heasarc_utils")

    provider = ReadOnlyToolProvider(
        roots=[tmp_path], include_plan_and_debate=True, human_handler=handler,
        asked_by="analyst_lead",
    )
    out = asyncio.run(provider.execute(
        "plan_and_debate",
        {"message": "## Plan\nextend the acquire package\n\nWhich package?"},
    ))
    assert not out.is_error
    assert "extend heasarc_utils" in out.text
    assert seen["query"].kind == "clarify_science"
    assert seen["query"].asked_by == "analyst_lead"
    assert "extend the acquire package" in seen["query"].message  # the Markdown body


# --- web_search: shared tool wired to reviewer / lead / worker (§9.3, [I1]) -------------------

from types import SimpleNamespace as _NS  # noqa: E402


class _RegDefs:
    """Minimal ToolkitRegistry stand-in exposing no toolkit tools."""

    def tool_definitions(self):
        return []

    def kind_for_function(self, name):
        return f"mcp.builtin.{name}"


class _FakeWeb:
    async def search(self, query, *, limit=10):
        self.query, self.limit = query, limit
        return [_NS(title="IXPE Cal Memo", url="https://ixpe/cal", abstract="epoch guidance")]


def test_web_search_exposed_only_with_web_source(tmp_path):
    """web_search appears on a provider exactly when a web_source is attached."""
    # ReadOnly (reviewer/lead): present with a source, absent without.
    assert "web_search" in _names(ReadOnlyToolProvider(roots=[tmp_path], web_source=_FakeWeb()))
    assert "web_search" not in _names(ReadOnlyToolProvider(roots=[tmp_path]))
    # Sandbox (worker): same gating.
    with_src = SandboxToolProvider(_RegDefs(), sandbox=None, cwd=tmp_path, web_source=_FakeWeb())
    without = SandboxToolProvider(_RegDefs(), sandbox=None, cwd=tmp_path)
    assert "web_search" in _names(with_src)
    assert "web_search" not in _names(without)


def test_web_search_routes_query_and_degrades_on_fault(tmp_path):
    """Both providers route to the source and format results; a source fault → error outcome."""
    for provider in (
        ReadOnlyToolProvider(roots=[tmp_path], web_source=_FakeWeb()),
        SandboxToolProvider(_RegDefs(), sandbox=None, cwd=tmp_path, web_source=_FakeWeb()),
    ):
        out = asyncio.run(provider.execute("web_search", {"query": "ixpe irf epoch", "limit": 3}))
        assert not out.is_error
        assert "IXPE Cal Memo" in out.text and "https://ixpe/cal" in out.text
        assert provider.web_source.query == "ixpe irf epoch" and provider.web_source.limit == 3

    class _BoomWeb:
        async def search(self, query, *, limit=10):
            raise RuntimeError("tavily down")

    provider = ReadOnlyToolProvider(roots=[tmp_path], web_source=_BoomWeb())
    out = asyncio.run(provider.execute("web_search", {"query": "x"}))
    assert out.is_error and "web_search failed" in out.text


def test_web_source_constructs_without_key(monkeypatch):
    """WebSource() never reads TAVILY_API_KEY at construction (lazy client); the key is a
    launch preflight requirement, keeping the tool always-attachable and tests key-independent."""
    from auto_research.sources import WebSource

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    WebSource()  # must not raise — no client built yet
    sentinel = object()
    assert WebSource(client=sentinel).client is sentinel


# --- read_file: read-by-line for the reviewer/lead reader (F8) ------------------------------

from auto_research.agents.react import (  # noqa: E402
    _DEFAULT_LINE_LIMIT,
    _MAX_LINE_CHARS,
    _read_lines,
)


def _write_numbered(tmp_path, n, name="src.py"):
    p = tmp_path / name
    p.write_text("\n".join(f"line{i}" for i in range(1, n + 1)) + "\n", encoding="utf-8")
    return p


def test_read_lines_cat_n_and_window(tmp_path):
    p = _write_numbered(tmp_path, 50)
    page = _read_lines(p, offset=10, limit=5)
    lines = page["content"].splitlines()
    assert lines[0] == f"{10:>6}\tline10"          # cat -n: numbered from the offset
    assert lines[4] == f"{14:>6}\tline14"
    assert page["start_line"] == 10 and page["end_line"] == 14
    assert page["total_lines"] == 50 and page["truncated"] is True
    assert "offset=15 to continue" in page["content"]  # footer names the resume offset


def test_read_lines_whole_short_file_not_truncated(tmp_path):
    p = _write_numbered(tmp_path, 5)
    page = _read_lines(p, offset=1, limit=_DEFAULT_LINE_LIMIT)
    assert page["truncated"] is False
    assert "to continue]" not in page["content"]
    assert page["content"].splitlines()[-1] == f"{5:>6}\tline5"


def test_read_lines_offset_past_eof_clamps(tmp_path):
    p = _write_numbered(tmp_path, 5)
    page = _read_lines(p, offset=999, limit=5)
    assert page["start_line"] == 5 and page["end_line"] == 5 and page["truncated"] is False


def test_read_lines_per_line_width_cap(tmp_path):
    p = tmp_path / "wide.txt"
    p.write_text("x" * (_MAX_LINE_CHARS + 500) + "\n", encoding="utf-8")
    body = _read_lines(p)["content"]
    # the numbered line is capped to _MAX_LINE_CHARS of content (+ the 6-wide number + tab)
    assert body.count("x") == _MAX_LINE_CHARS


def test_read_lines_full_coverage_by_paging(tmp_path):
    """Walking offset by limit reconstructs the whole file — nothing is dropped."""
    p = _write_numbered(tmp_path, 2500)  # > default limit → forces multiple pages
    recovered, offset, pages = [], 1, 0
    while True:
        page = _read_lines(p, offset=offset, limit=400)
        pages += 1
        for ln in page["content"].splitlines():
            if "\t" not in ln:  # skip the footer note + its blank separator line
                continue
            recovered.append(ln.split("\t", 1)[1])  # strip the cat -n prefix
        if not page["truncated"]:
            break
        offset = page["end_line"] + 1
    assert pages > 1  # actually paged
    assert recovered == [f"line{i}" for i in range(1, 2501)]


def test_read_file_tool_exposes_offset_limit(tmp_path):
    p = _write_numbered(tmp_path, 10)
    prov = ReadOnlyToolProvider(roots=[tmp_path])
    rf = next(d for d in prov.tool_definitions() if d["name"] == "read_file")
    assert {"offset", "limit"} <= set(rf["input_schema"]["properties"])
    out = asyncio.run(prov.execute("read_file", {"path": str(p), "offset": 8, "limit": 5}))
    assert not out.is_error
    assert out.text.splitlines()[0] == f"{8:>6}\tline8"  # honored offset, cat -n output


# --- web_search (uncapped) + web_fetch (Tavily extract, paged) — [I10] --------------------------

from types import SimpleNamespace as _WNS  # noqa: E402

from auto_research.agents.react import (  # noqa: E402
    WEB_FETCH_TOOL,
    WEB_SEARCH_TOOL,
    execute_web_fetch,
    execute_web_search,
    web_fetch_tool_def,
)


class _FetchWeb:
    """Fake WebSource with search + fetch for the tool-handler tests."""

    def __init__(self, *, page="", raise_on_fetch=False):
        self._page = page
        self._raise = raise_on_fetch
        self.fetch_depth = None

    async def search(self, query, *, limit=10):
        return [_WNS(title="T", url="https://x/y", abstract="Z" * 500)]  # long snippet

    async def fetch(self, url, *, depth="advanced"):
        self.fetch_depth = depth
        if self._raise:
            raise RuntimeError("extract boom")
        return self._page


def test_web_search_returns_full_snippet_uncapped():
    """No 300-char per-snippet cut and no 8000-char overall cut — Tavily's snippet is returned whole."""
    out = asyncio.run(execute_web_search(_FetchWeb(), {"query": "q"}))
    assert not out.is_error
    assert out.text.count("Z") == 500  # full snippet, not clipped to 300


def test_web_fetch_pages_markdown_with_footer_and_depth():
    md = "\n".join(f"para {i}" for i in range(1, 30))  # 29 lines
    web = _FetchWeb(page=md)
    out = asyncio.run(execute_web_fetch(web, {"url": "https://x", "offset": 1, "limit": 10}))
    assert not out.is_error
    assert out.text.splitlines()[0] == f"{1:>6}\tpara 1"          # cat -n
    assert "call web_fetch with offset=11 to continue" in out.text  # web_fetch resume hint
    assert web.fetch_depth == "advanced"                            # default depth
    # honors offset on the next page
    out2 = asyncio.run(execute_web_fetch(web, {"url": "https://x", "offset": 11, "limit": 10}))
    assert out2.text.splitlines()[0] == f"{11:>6}\tpara 11"


def test_web_fetch_guards_and_degrades():
    assert asyncio.run(execute_web_fetch(None, {"url": "https://x"})).is_error          # no source
    assert asyncio.run(execute_web_fetch(_FetchWeb(), {})).is_error                      # no url
    boom = asyncio.run(execute_web_fetch(_FetchWeb(raise_on_fetch=True), {"url": "https://x"}))
    assert boom.is_error and "web_fetch failed" in boom.text                             # fault → error
    empty = asyncio.run(execute_web_fetch(_FetchWeb(page=""), {"url": "https://x"}))
    assert not empty.is_error and "no extractable content" in empty.text                 # soft note


def test_web_fetch_exposed_only_with_web_source(tmp_path):
    web = _FetchWeb()
    assert WEB_FETCH_TOOL in _names(ReadOnlyToolProvider(roots=[tmp_path], web_source=web))
    assert WEB_FETCH_TOOL not in _names(ReadOnlyToolProvider(roots=[tmp_path]))
    with_src = SandboxToolProvider(_RegDefs(), sandbox=None, cwd=tmp_path, web_source=web)
    assert {WEB_SEARCH_TOOL, WEB_FETCH_TOOL} <= _names(with_src)


def test_web_source_fetch_extracts_raw_content():
    from auto_research.sources import WebSource

    ok = WebSource(client=_WNS(extract=lambda urls, extract_depth: {"results": [{"raw_content": "# md"}]}))
    assert asyncio.run(ok.fetch("https://x")) == "# md"
    failed = WebSource(
        client=_WNS(extract=lambda urls, extract_depth: {"results": [], "failed_results": [{"error": "nope"}]})
    )
    with pytest.raises(RuntimeError, match="nope"):
        asyncio.run(failed.fetch("https://x"))


# --- Authoring tools (A1 slice 2): drive_coder / submit_for_review over an AuthoringSession ---

from types import SimpleNamespace  # noqa: E402

from auto_research.agents.react import (  # noqa: E402
    DRIVE_CODER_TOOL,
    SUBMIT_TOOL,
    execute_drive_coder,
    execute_submit,
)


class _FakeSession:
    """Minimal AuthoringSession stand-in exercising drive/gate/review/commit primitives."""

    def __init__(
        self, *, gate, review, tool_name="my_tool", submit_cap=5, debate_cap=3,
        rebuttal_review=None,
    ):
        self._gate = gate
        self._review = review
        self._rebuttal_review = rebuttal_review  # verdict when a lead_rebuttal is supplied
        self.tool_spec = SimpleNamespace(proposed_tool_name=tool_name)
        self.driven: list[str] = []
        self.committed_with: str | None = None
        self.submit_cap = submit_cap
        self.submits_used = 0
        self.debate_cap = debate_cap
        self.debates_used = 0
        self.last_gate = None
        self.last_review = None

    async def drive(self, message):
        self.driven.append(message)
        return f"coder did: {message}"

    async def run_gate_step(self):
        self.last_gate = self._gate
        return self._gate

    async def run_review_step(self, gate, *, lead_rebuttal=None):
        review = (
            self._rebuttal_review
            if lead_rebuttal is not None and self._rebuttal_review is not None
            else self._review
        )
        self.last_review = review
        return review

    async def commit(self, message):
        self.committed_with = message
        return "deadbeefcafe123"


def _check(passed, *, layer="A", name="determinism", detail="ok"):
    return SimpleNamespace(passed=passed, layer=layer, name=name, detail=detail)


def _gate(passed, *, layer_a=None, layer_b=None, reason="gate"):
    return SimpleNamespace(
        passed=passed, layer_a=layer_a or [], layer_b=layer_b or [], reason=reason
    )


def _review(approved, *, issues=None, rationale="r", commit_summary="add my_tool"):
    return SimpleNamespace(
        approved=approved, issues=issues or [], rationale=rationale,
        commit_summary=commit_summary, checks_adequate=approved,
    )


def test_authoring_tools_masked_without_session(tmp_path):
    p = ReadOnlyToolProvider(roots=[tmp_path])
    names = [d["name"] for d in p.tool_definitions()]
    assert DRIVE_CODER_TOOL not in names and SUBMIT_TOOL not in names
    # dispatch is denied too, not just hidden
    assert asyncio.run(p.execute(DRIVE_CODER_TOOL, {"instructions": "x"})).is_error
    assert asyncio.run(p.execute(SUBMIT_TOOL, {})).is_error


def test_authoring_tools_exposed_only_with_session(tmp_path):
    sess = _FakeSession(gate=_gate(True), review=_review(True))
    p = ReadOnlyToolProvider(roots=[tmp_path], authoring_session=sess)
    names = [d["name"] for d in p.tool_definitions()]
    assert DRIVE_CODER_TOOL in names and SUBMIT_TOOL in names


def test_drive_coder_routes_instructions_to_session():
    sess = _FakeSession(gate=_gate(True), review=_review(True))
    out = asyncio.run(execute_drive_coder(sess, {"instructions": "write pcube_timeseg"}))
    assert not out.is_error
    assert sess.driven == ["write pcube_timeseg"]
    assert "coder did" in out.text


def test_drive_coder_requires_instructions():
    sess = _FakeSession(gate=_gate(True), review=_review(True))
    assert asyncio.run(execute_drive_coder(sess, {})).is_error


def test_submit_gate_failure_reports_checks_and_does_not_commit():
    sess = _FakeSession(
        gate=_gate(False, layer_a=[_check(False, name="no_fabrication.vocabulary",
                                          detail="banned phrase 'synthetic'")]),
        review=_review(True),
    )
    out = asyncio.run(execute_submit(sess))
    assert "GATE FAILED" in out.text and "no_fabrication.vocabulary" in out.text
    assert sess.committed_with is None  # gate is the immovable wall


def test_submit_review_rejection_offers_debate_and_does_not_commit():
    sess = _FakeSession(
        gate=_gate(True),
        review=_review(False, issues=["tests do not prove per-segment time-filtering"]),
    )
    out = asyncio.run(execute_submit(sess))
    assert "REVIEW REJECTED" in out.text and "time-filtering" in out.text
    assert "respond_to_review" in out.text  # the debate path is offered
    assert sess.committed_with is None


def test_submit_gate_and_review_pass_commits():
    sess = _FakeSession(gate=_gate(True), review=_review(True, commit_summary="add my_tool"))
    out = asyncio.run(execute_submit(sess))
    assert "COMMITTED" in out.text and "my_tool" in out.text
    assert sess.committed_with == "add my_tool"  # commit ⇔ gate ∧ review, no self-approval


def test_submit_cap_exhaustion_returns_terminal_message():
    sess = _FakeSession(
        gate=_gate(True), review=_review(False, issues=["x"]), submit_cap=1
    )
    out1 = asyncio.run(execute_submit(sess))
    assert "REVIEW REJECTED" in out1.text and sess.submits_used == 1
    out2 = asyncio.run(execute_submit(sess))
    assert "used all 1 review submissions" in out2.text
    assert sess.submits_used == 1  # the cap is not exceeded


# --- respond_to_review (A1 slice 4): the lead↔co-lead debate ---

from auto_research.agents.react import (  # noqa: E402
    RESPOND_TO_REVIEW_TOOL,
    execute_respond_to_review,
)


def test_respond_to_review_before_submit_is_error():
    sess = _FakeSession(gate=_gate(True), review=_review(False, issues=["x"]))
    out = asyncio.run(execute_respond_to_review(sess, {"message": "I disagree"}))
    assert out.is_error and "submit_for_review first" in out.text


def test_respond_to_review_holds_position_no_commit():
    sess = _FakeSession(gate=_gate(True), review=_review(False, issues=["weak test"]))
    asyncio.run(execute_submit(sess))  # establishes last_gate/last_review (rejected)
    out = asyncio.run(execute_respond_to_review(sess, {"message": "the test is fine because..."}))
    assert "HELD its position" in out.text
    assert sess.committed_with is None
    assert sess.debates_used == 1


def test_respond_to_review_persuades_then_regates_and_commits():
    sess = _FakeSession(
        gate=_gate(True),
        review=_review(False, issues=["weak test"]),
        rebuttal_review=_review(True, commit_summary="add my_tool"),
    )
    asyncio.run(execute_submit(sess))  # gate pass, review reject
    out = asyncio.run(execute_respond_to_review(sess, {"message": "already handled at line 40"}))
    assert "PERSUADED" in out.text and "COMMITTED" in out.text
    assert sess.committed_with == "add my_tool"  # commit only after re-gate passed


def test_respond_to_review_debate_cap():
    sess = _FakeSession(
        gate=_gate(True), review=_review(False, issues=["x"]), debate_cap=1
    )
    asyncio.run(execute_submit(sess))
    asyncio.run(execute_respond_to_review(sess, {"message": "arg 1"}))
    out = asyncio.run(execute_respond_to_review(sess, {"message": "arg 2"}))
    assert "used all 1 debate turns" in out.text
    assert sess.debates_used == 1


def test_respond_to_review_exposed_with_session(tmp_path):
    sess = _FakeSession(gate=_gate(True), review=_review(True))
    p = ReadOnlyToolProvider(roots=[tmp_path], authoring_session=sess)
    assert RESPOND_TO_REVIEW_TOOL in [d["name"] for d in p.tool_definitions()]


# ── The assistant turn IS the response (the fold) ───────────────────────────────


def test_react_replays_the_received_turn_whole():
    """`_react` appends the `ModelResponse` it received, rather than copying selected fields
    into a separate assistant-turn type. That copy used to live here and lost a part every time
    the provider part-set grew — tool-search discovery blocks until 2026-07-27, thinking blocks
    until 2026-08-05 — so what this pins is that *no* part needs enumerating to survive."""

    class _Model:
        def __init__(self):
            self.seen: list[list] = []
            self._calls = 0

        async def call(self, messages, *, schema=None, tools=None, tool_choice="auto",
                       images=(), documents=(), cache_prefix=False):
            self.seen.append(list(messages))
            self._calls += 1
            if self._calls == 1:
                return ModelResponse(
                    content="on it",
                    tool_calls=[ToolCall(id="c1", name="t", arguments={})],
                    provider_parts=[
                        {"type": "thinking", "thinking": "reasoning", "signature": "sig"},
                        {"type": "some_future_part", "payload": 1},
                    ],
                )
            return ModelResponse(content="done")

    model = _Model()
    agent = ReviewerAgent(model=model, prompt="p")
    asyncio.run(agent._react(seed_messages=[], tools=_LoopTools(), action="review", cap=3))

    replayed = model.seen[1][0]                      # the turn carried into the next request
    assert isinstance(replayed, ModelResponse)
    assert replayed.content == "on it"
    assert [c.name for c in replayed.tool_calls] == ["t"]
    assert [p["type"] for p in replayed.provider_parts] == ["thinking", "some_future_part"]
    assert replayed.provider_parts[0]["signature"] == "sig"


def test_terminal_compose_declares_the_loops_tools():
    """The structured compose after a ReAct loop declares the SAME tools, and forbids calling.

    The compose replays the loop's trajectory, whose provider items reference those tools. A
    request that omits the declarations orphans them, which is what killed two live runs — a
    dangling `Tool reference` on Anthropic (2026-07-29) and `function_call … without its required
    'reasoning' item` on OpenAI (2026-08-05, run 8c75990e). `tool_choice="none"` keeps the
    declarations while forbidding another call, so the model must answer.
    """
    from pydantic import BaseModel

    class _Out(BaseModel):
        note: str

    class _Model:
        def __init__(self):
            self.calls: list[dict] = []
            self._n = 0

        async def call(self, messages, *, schema=None, tools=None, tool_choice="auto",
                       images=(), documents=(), cache_prefix=False):
            self.calls.append({"schema": schema, "tools": tools, "tool_choice": tool_choice})
            self._n += 1
            if self._n == 1:
                return ModelResponse(
                    tool_calls=[ToolCall(id="c1", name="paid", arguments={})],
                    provider_parts=[{"type": "reasoning", "id": "rs_1"}],
                )
            if schema is not None:
                return ModelResponse(parsed={"note": "done"})
            return ModelResponse(content="done")

    model = _Model()
    agent = ReviewerAgent(model=model, prompt="p")
    tools = _LoopTools()

    async def run():
        messages, _, _ = await agent._react(
            seed_messages=[], tools=tools, action="review", cap=1
        )
        return await agent._call_over_messages(messages, _Out, action="review", tools=tools)

    result = asyncio.run(run())

    assert result.note == "done"
    loop_call, compose_call = model.calls[0], model.calls[-1]
    assert compose_call["schema"] is _Out
    assert compose_call["tools"] == loop_call["tools"], "the same declarations the loop used"
    assert compose_call["tool_choice"] == "none"


def test_compose_without_tools_declares_none():
    """A compose over a trajectory-free conversation (`_call`) still declares nothing — the
    threading is additive, so payload-only structured calls are unchanged."""
    from pydantic import BaseModel

    class _Out(BaseModel):
        note: str

    class _Model:
        def __init__(self):
            self.calls: list[dict] = []

        async def call(self, messages, *, schema=None, tools=None, tool_choice="auto",
                       images=(), documents=(), cache_prefix=False):
            self.calls.append({"tools": tools})
            return ModelResponse(parsed={"note": "ok"})

    model = _Model()
    agent = ReviewerAgent(model=model, prompt="p")
    asyncio.run(agent._call({"q": "x"}, _Out, action="review"))
    assert model.calls[0]["tools"] is None
