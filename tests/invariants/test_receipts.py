"""Freeze of CLAUDE.md §9.3.2 (runtime-minted receipts) and §9.3.4 (texts are not evidence), through
each role's own loop (Phase 3 step 0, item I2).

Every cell scripts a turn of the real role (``drivers.Run.turn``) and judges what the runtime
minted (``Run.receipts``, the run's one store), what the model was shown (its tool results and
the next call's input) and what the unit filed. Harness reads reach the store through the real
providers, never ``store.mint``; hashes are recomputed here from the stored bytes and the files
on disk; a refusal carries its accepted counterpart in the same run.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.invariants import drivers as d

MARKER = "zebra-marker"
"""A word no tool output or report holds, so a scan of the store finds a text that was minted."""
PAGE_NUMBER = "7.5"


# ── script building ──────────────────────────────────────────────────────────────────────────


def _write(run: d.Run, role: str, claim):
    """The ``write_report`` call of ``role`` whose claim is ``claim`` (text, or a callable of what
    the call was sent); the librarian's finding names the paper it opened."""
    write = d.report_tools(role)[0]
    source = run.source(role)

    def item(sent: d.Sent):
        text = claim(sent) if callable(claim) else claim
        support = d.handles_shown(sent, source, "p")[-1:] if role == "librarian" else ()
        return write, {"content": d.report_content(role, claim=text, support=support)}

    return item


def _end(role: str):
    return d.report_tools(role)[2], {"summary": "filed"}


def _ends(result: d.TurnResult, role: str):
    return result.results_named(d.report_tools(role)[2])


def _results(result: d.TurnResult, name: str):
    return result.results_named(name)


def _new(run: d.Run, before: set[str]):
    return [r for r in run.receipts.receipts() if r.handle not in before]


def _handles(run: d.Run) -> set[str]:
    return {r.handle for r in run.receipts.receipts()}


def _head(run: d.Run) -> str:
    from tests.fixtures.toolkit import git

    return git(run.toolkit_root, "rev-parse", "HEAD").strip()


def _tool_call(run: d.Run, suffix: str, **arguments):
    """A toolkit tool call whose declared name ends with ``suffix``."""
    return lambda sent: (d.tool_named(sent, suffix), {"csv_path": str(run.product), **arguments})


# ── §9.3.2 every tool call's output is a receipt the runtime mints ───────────────────────────

@pytest.mark.parametrize("role", d.ROLES)
def test_a_harness_return_is_a_navigation_receipt_minted_through_the_provider(role, tmp_path):
    # §9.3.2 every tool call's output is a runtime-minted receipt; §9.3.3 a harness return is navigation
    from auto_research.provenance.store import sha256_file

    async def go():
        async with d.open_run(tmp_path) as run:
            calls = run.harness_calls(role)
            before = _handles(run)
            result = await run.turn(role, [*(c.step() for c in calls), *run.file_script(role)])
            return run, calls, result, before

    run, calls, result, before = asyncio.run(go())

    assert result.error is None and result.filed, [(r.name, r.text) for r in result.tool_results]
    minted = {r.handle: r for r in _new(run, before)}
    for call in calls:
        (seen,) = _results(result, call.tool)
        assert not seen.is_error, (call.kind, seen.text)
        (handle,) = d.handles_in(seen.text, result.source, call.letter)  # the model was shown the handle
        receipt = minted[handle]
        assert receipt.produced_by == call.producer and not d.is_evidence(receipt), call.kind
        # The hash is the runtime's, over the bytes it stored.
        assert receipt.sha256 == sha256_file(receipt.path) and run.receipts.verify(handle) is None, call.kind
        if not call.producer.startswith("mcp."):  # an in-process call runs in the run's default environment
            assert receipt.env == run.receipts.default_env, call.kind


def test_a_toolkit_call_is_minted_with_producer_commit_arguments_and_environment(tmp_path):
    # §9.3.2 producer stamped (toolkit + commit), args hash, the environment it ran in
    from auto_research.provenance.calls import calls_by_receipt, ledger_calls
    from auto_research.provenance.store import sha256_file
    from tests.fixtures import toolkit as fixture

    async def go():
        async with d.open_run(tmp_path) as run:
            failing = {"min_total_counts": {"value": 1e9, "reason": "a threshold no row reaches"}}
            result = await run.turn("worker", [
                _tool_call(run, "band_pa_summary"), _tool_call(run, "band_pa_summary", **failing),
                *run.file_script("worker"),
            ])
            from auto_research.env import env_lock_dir

            locks = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in env_lock_dir().glob("*.json")}
            return run, result, locks

    run, result, locks = asyncio.run(go())

    assert result.error is None and result.filed, [(r.name, r.text) for r in result.tool_results]
    ok, bad = result.results_named(result.declared("band_pa_summary"))
    assert not ok.is_error and bad.is_error
    (good_handle,), (bad_handle,) = (d.handles_in(r.text, result.source, "r") for r in (ok, bad))
    good, failed = run.receipts.get(good_handle), run.receipts.get(bad_handle)
    producer = f"{fixture.TOOLKIT_NAME}.{fixture.PKG_NAME}.{fixture.TOOL_NAME}"
    for receipt in (good, failed):  # a failed call's output is minted too
        assert receipt.kind == "result" and receipt.produced_by == producer
        assert receipt.toolkit_commit == _head(run) and receipt.toolkit_root == run.toolkit_root
        assert receipt.sha256 == sha256_file(receipt.path) and run.receipts.verify(receipt.handle) is None
    assert d.is_evidence(good) and good.flags == [] and failed.flags == ["failed"]
    # The arguments the receipt hashes rebuild from the ledger's record of the call.
    calls = calls_by_receipt(ledger_calls(run.ledger), [good, failed])
    assert calls[good.handle].model_args == {"csv_path": str(run.product)}
    assert good.args_sha256 != failed.args_sha256
    # The environment is the package's: stamped on both receipts, with a lock the store can rebuild it from.
    assert good.env and good.env == failed.env and good.env != run.receipts.default_env
    lock = locks[good.env]
    assert lock["environment"] == f"ar-{fixture.ENV_ID}" and lock["fingerprint"] == good.env


# ── §9.3.2 products, scratch and figures ─────────────────────────────────────────────────────


def test_a_declared_product_is_a_data_receipt_of_its_call(tmp_path):  # §9.3.2 a declared product is minted
    from auto_research.provenance.store import sha256_file

    async def go():
        async with d.open_run(tmp_path) as run:
            result = await run.turn("worker", [_tool_call(run, "write_band_table"), *run.file_script("worker")])
            return run, result

    run, result = asyncio.run(go())

    assert result.error is None, result.error
    (shown,) = result.results_named(result.declared("write_band_table"))
    (call_handle,) = d.handles_in(shown.text, result.source, "r")
    (product_handle,) = d.handles_in(shown.text, result.source, "d")  # the model was shown both handles
    call, product = run.receipts.get(call_handle), run.receipts.get(product_handle)
    assert product.kind == "data" and product.produced_by == call.produced_by and d.is_evidence(product)
    # It belongs to the call: the call's id, arguments, commit and environment.
    assert (product.call_id, product.args_sha256) == (call.call_id, call.args_sha256)
    assert (product.toolkit_commit, product.env) == (call.toolkit_commit, call.env)
    # It lies in the call's own output folder, and its hash is the runtime's over the file's bytes.
    folder = product.path.parent
    assert (folder.parent.parent.name, folder.parent.name, folder.name) == ("products", "write_band_table", call.call_id)
    assert product.sha256 == sha256_file(product.path)
    assert product.path.read_text(encoding="utf-8").startswith("obsid,ratio,ratio_err")


def test_a_figure_is_rehashed_from_its_file_and_scratch_is_minted_as_nothing(tmp_path):
    # §9.3.2 a figure is re-hashed from its file; an undeclared file is scratch, minted as nothing
    from auto_research.provenance.store import sha256_file

    async def go():
        async with d.open_run(tmp_path) as run:
            unit = run.unit("worker")
            report = d.report_content("worker") + "# figures\n- [self:1]\n"
            result = await run.turn("worker", [
                _tool_call(run, "write_band_table"),
                lambda sent: (d.tool_named(sent, "plot_band_ratio"),
                              {"ratios": {"$ref": f"{d.handles_shown(sent, unit, 'r')[0]}#/ratios"}}),
                _tool_call(run, "count_rows", min_counts={"value": 1250, "reason": "the brighter row only"}),
                run.harness_call("worker", "shell").step(command="echo scratch > scratch_i2.txt && ls"),
                (d.report_tools("worker")[0], {"content": report}), (d.report_tools("worker")[2], {"summary": "filed"}),
            ])
            return run, result

    run, result = asyncio.run(go())

    assert result.error is None and result.filed, [(r.name, r.text) for r in result.tool_results]
    receipts = run.receipts.receipts(source=result.source)
    by = lambda suffix: [r for r in receipts if r.produced_by.endswith(suffix)]  # noqa: E731
    (figure,) = [r for r in by("plot_band_ratio") if r.kind == "figure"]
    (table_call,) = [r for r in by("write_band_table") if r.kind == "result"]
    assert figure.parents == [table_call.handle]  # the plot took the table's ratios by reference
    assert figure.mime_type == "image/png" and figure.caption == "Band ratio per row" and d.is_evidence(figure)
    assert figure.sha256 == sha256_file(figure.path) and figure.path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert (figure.path.parent.parent.name, figure.path.parent.name) == ("plot_band_ratio", figure.call_id)
    # A tool that writes a scratch copy in a folder it removes mints one receipt, its output.
    assert [r.kind for r in by("count_rows")] == ["result"]
    # A shell command that writes a file mints one receipt, its output; the file is minted as nothing.
    (shell,) = by("bash_exec")
    assert shell.kind == "result" and not d.is_evidence(shell)
    assert not [r for r in receipts if r.path.name == "scratch_i2.txt"]
    assert not [r for r in receipts if "scratch" in (run.receipts.text(r.handle) or "")
                and r.produced_by != shell.produced_by]


# ── §9.3.2 ancestry ──────────────────────────────────────────────────────────────────────────


def test_a_ref_links_the_units_own_result_as_parent_and_a_failed_one_is_refused(tmp_path):
    # §9.3.2 ancestry through `$ref`; a failed receipt grounds nothing
    async def go():
        async with d.open_run(tmp_path) as run:
            unit = run.unit("worker")
            at = lambda sent, n: d.handles_shown(sent, unit, "r")[n]  # noqa: E731
            failing = {"min_total_counts": {"value": 1e9, "reason": "a threshold no row reaches"}}
            count = lambda ref: lambda sent: (d.tool_named(sent, "count_rows"), {  # noqa: E731
                "csv_path": str(run.product), "min_counts": {"$ref": ref(sent)}})
            result = await run.turn("worker", [
                _tool_call(run, "band_pa_summary"),                                     # r1: no parent
                _tool_call(run, "band_pa_summary", **failing),                          # r2: failed
                count(lambda sent: f"{at(sent, 1)}#/$args/min_total_counts"),           # refused
                count(lambda sent: f"{at(sent, 0)}#/ratio"),                            # r3: parent r1
                _write(run, "worker", lambda sent: f"The threshold was {{{{{at(sent, 1)}#/$args/min_total_counts}}}}."),
                _end("worker"),
                _write(run, "worker", lambda sent: f"The ratio is {{{{{at(sent, 0)}#/ratio}}}}."), _end("worker"),
            ])
            return run, result

    run, result = asyncio.run(go())

    assert result.error is None and result.filed, [(r.name, r.text) for r in result.tool_results]
    receipts = run.receipts.receipts(source=result.source)
    results = {r.handle.split(":")[1]: r for r in receipts if r.kind == "result"}
    assert results["r1"].parents == [] and results["r2"].parents == []  # no reference, no parent
    assert results["r2"].flags == ["failed"] and results["r1"].flags == []
    # The call that referenced a failed result was refused before it ran; the one that referenced a
    # sound result ran and carries it as parent.
    refused, accepted = result.results_named(result.declared("count_rows"))
    assert refused.is_error and d.FLAGGED_FAILED in refused.text and not accepted.is_error
    assert results["r3"].parents == [results["r1"].handle]
    assert len([r for r in receipts if r.produced_by.endswith("count_rows")]) == 1
    # A number a failed result carries cannot be cited; the same citation of a sound one is accepted.
    bad, good = _ends(result, "worker")
    assert bad.is_error and results["r2"].handle in bad.text and not good.is_error


def test_a_flag_reaches_every_descendant_of_a_failed_calls_product(tmp_path):
    # §9.3.2 `failed` propagates to every descendant
    async def go():
        async with d.open_run(tmp_path, shapes=True) as run:
            unit = run.unit("worker")
            copied = lambda sent: run.receipts.get(d.handles_shown(sent, unit, "d")[0]).path  # noqa: E731
            result = await run.turn("worker", [
                lambda sent: (d.tool_named(sent, d.FAILING_PRODUCT_TOOL), {"csv_path": str(run.product)}),
                lambda sent: (d.tool_named(sent, "count_rows"), {
                    "csv_path": str(copied(sent)), "min_counts": {"value": 0, "reason": "every row"}}),
                _tool_call(run, "count_rows", min_counts={"value": 0, "reason": "every row"}),
                *run.file_script("worker"),
            ])
            return run, result

    run, result = asyncio.run(go())

    assert result.error is None and result.filed, [(r.name, r.text) for r in result.tool_results]
    mine = run.receipts.receipts(source=result.source)
    failed_call, product = sorted([r for r in mine if r.produced_by.endswith(d.FAILING_PRODUCT_TOOL)],
                                  key=lambda r: r.kind != "result")
    assert failed_call.flags == ["failed"] and product.kind == "data" and "failed" in product.flags
    # The call that read the failed call's product descends from it and carries the flag; the call that
    # read the sound product does not.
    descended, sound = [r for r in mine if r.produced_by.endswith("count_rows") and r.kind == "result"]
    assert product.handle in descended.parents and "failed" in descended.flags
    assert sound.parents == [] and sound.flags == []
    assert [r.handle for r in mine if failed_call.handle in r.parents or product.handle in r.parents] == [
        descended.handle]


def test_a_declared_figure_hash_that_disagrees_with_its_file_is_overridden(tmp_path):
    # §9.3.2 a figure whose declared hash disagrees is overridden with a note; one whose file is absent is dropped
    from auto_research.provenance.store import sha256_file

    async def go():
        async with d.open_run(tmp_path, shapes=True) as run:
            unit = run.unit("worker")
            report = d.report_content("worker") + "# figures\n- [self:1]\n"
            result = await run.turn("worker", [
                _tool_call(run, "write_band_table"),
                lambda sent: (d.tool_named(sent, d.MISDECLARED_FIGURE_TOOL),
                              {"ratios": {"$ref": f"{d.handles_shown(sent, unit, 'r')[0]}#/ratios"}}),
                (d.report_tools("worker")[0], {"content": report}), _end("worker"),
            ])
            return run, result

    run, result = asyncio.run(go())

    assert result.error is None and result.filed, [(r.name, r.text) for r in result.tool_results]
    figures = [r for r in run.receipts.receipts(source=result.source) if r.kind == "figure"]
    (figure,) = figures  # the figure whose file is absent is not minted
    assert figure.caption == d.MISDECLARED_CAPTION and figure.sha256 == sha256_file(figure.path)
    # The model is told what the runtime corrected.
    (shown,) = result.results_named(result.declared(d.MISDECLARED_FIGURE_TOOL))
    assert d.FIGURE_HASH_OVERRIDDEN in shown.text and d.FILE_ABSENT in shown.text
    assert d.ABSENT_CAPTION in shown.text and not [r for r in figures if r.caption == d.ABSENT_CAPTION]


# ── §9.3.2 the librarian's paper and web receipts ────────────────────────────────────────────


def test_a_paper_receipt_is_the_pdf_itself_and_a_web_receipt_is_reused_for_the_same_extract(tmp_path):
    # §9.3.2 a paper receipt is the PDF; a web receipt is reused for the same extract
    from auto_research.provenance.store import sha256_bytes, sha256_file
    from tests.media_files import pdf_bytes

    async def go():
        async with d.open_run(tmp_path) as run:
            fetch = ("web_fetch", {"url": "https://x.test/p"})
            result = await run.turn("librarian", [
                *run.evidence_steps("librarian"), ("read_paper", {"hit": 1}), fetch, fetch,
                *run.file_script("librarian")[2:],
            ])
            return run, result

    run, result = asyncio.run(go())

    assert result.error is None and result.filed, [(r.name, r.text) for r in result.tool_results]
    (paper,) = run.receipts.receipts(kind="paper")
    pdf = pdf_bytes(d.PAPER_TITLE)
    assert paper.sha256 == sha256_bytes(pdf) == paper.source_sha256 and paper.source_path.read_bytes() == pdf
    assert d.is_evidence(paper) and paper.ids["arxiv"] == d.PAPER_ARXIV
    reads = _results(result, "read_paper")
    assert [d.handles_in(r.text, result.source, "p") for r in reads] == [[paper.handle]] * 2
    (web,) = run.receipts.receipts(kind="web")
    fetches = _results(result, "web_fetch")
    assert [d.handles_in(r.text, result.source, "w") for r in fetches] == [[web.handle]] * 2
    assert not d.is_evidence(web) and web.sha256 == sha256_file(web.path)


# ── §9.3.2 the coding pipeline's commit ──────────────────────────────────────────────────────


def test_a_commit_mints_a_commit_receipt_with_tool_sha_and_branch_that_is_evidence(approved_chain):
    # §9.3.2 a commit the coding pipeline made is a `commit` receipt (tool, SHA, branch), evidence
    from auto_research.provenance.store import sha256_file

    chain = approved_chain
    run, lead = chain.run, chain.lead
    assert lead.error is None and lead.filed, [(r.name, r.text) for r in lead.tool_results]
    (commit,) = run.receipts.receipts(kind="commit")
    (sha,) = [line.split()[0] for line in chain.log]  # the one commit the link made on the branch
    assert commit.source == lead.source and commit.produced_by == d.APPROVE_EDIT and d.is_evidence(commit)
    record = json.loads(run.receipts.text(commit.handle))
    assert (record["tool"], record["commit_sha"], record["branch"]) == (d.NEW_TOOL, sha, chain.branch)
    assert record["submissions_used"] == 1 and commit.sha256 == sha256_file(commit.path)
    # The commit names the brief it was reviewed against; the brief is a record, not evidence.
    (brief,) = [run.receipts.get(h) for h in commit.parents]
    assert brief.produced_by == d.TOOLSPEC and not d.is_evidence(brief) and record["toolspec"] == brief.handle
    (submitted,) = _results(lead, "submit_for_review")
    assert commit.handle in submitted.text and lead.handles("c") == [commit.handle]


# ── §9.3.4 texts are not evidence ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("receiver", ["reviewer", "librarian", "worker", "lead"])
def test_a_channel_text_mints_no_receipt_and_widens_no_scope(receiver, tmp_path):
    # §9.3.4 a handle a text names stays outside the receiver's scope
    seat = 1 if receiver == "worker" else 0  # a worker hears a peer; the others hear worker 0

    async def go():
        async with d.open_run(tmp_path, workers=2) as run:
            sender = run.source("worker", seat)
            note = lambda sent: ("message", {"to": run.unit(receiver), "text": (  # noqa: E731
                f"{MARKER}: {d.handles_shown(sent, sender, 'r')[0]} holds the ratio, 0.28.")})
            worker = await run.turn("worker", [*run.evidence_steps("worker"), note,
                                               *run.file_script("worker", claim=_ratio_cited(sender))], seat=seat)
            (handle,) = worker.handles("r")
            steps = run.evidence_steps("librarian") if receiver == "librarian" else []
            plain = _write(run, receiver, d.CLAIM)
            own_scope = lambda sent: f"The ratio is {{{{{handle}#/ratio}}}}."  # noqa: E731
            result = await run.turn(receiver, [*steps, _write(run, receiver, own_scope), _end(receiver),
                                               plain, _end(receiver)])
            handed = None
            if receiver == "lead":  # the accepted counterpart: the same handle once the worker's report arrives
                handed = await run.turn("lead", run.file_script("lead", claim=own_scope), handed=[worker])
            return run, worker, result, handle, handed

    run, worker, result, handle, handed = asyncio.run(go())

    assert worker.filed and result.error is None and result.filed
    # The text reached the receiver and named the handle; nothing was minted from it.
    heard = result.sent_with(MARKER).user_text()  # the text reached the receiver and named the handle
    assert handle in heard
    assert not [r for r in run.receipts.receipts() if MARKER in (run.receipts.text(r.handle) or "")]
    refused, accepted = _ends(result, receiver)
    assert refused.is_error and f"cites {handle}, {d.NOT_CITABLE}" in refused.text and not accepted.is_error
    assert handle not in result.report.model_dump_json()
    if handed is not None:
        assert handed.error is None and handed.filed and f"{handle}#/ratio" in handed.report.model_dump_json()


def _ratio_cited(source: str):
    return lambda sent: f"The ratio is {{{{{d.handles_shown(sent, source, 'r')[0]}#/ratio}}}}."


@pytest.mark.parametrize("role", d.ROLES)
def test_a_humans_reply_is_a_nudge_and_its_file_is_the_seats_own_navigation_receipt(role, tmp_path):
    # §9.3.4 a human's text mints no receipt and a number it states cannot be cited; its file is not evidence
    from auto_research.provenance.store import sha256_bytes

    reply = f"The ratio is 0.28 per my notes, {MARKER}."
    notes = "ratio\n0.28\n"

    async def go():
        async with d.open_run(tmp_path, human=[d.Reply(reply, files={"notes.csv": notes})]) as run:
            source = run.source(role)
            sent_handle = lambda sent: d.handles_in(sent.user_text(), source, "d")[0]  # noqa: E731
            steps = run.evidence_steps(role) if role == "librarian" else []
            result = await run.turn(role, [
                ("message", {"to": "human", "text": "Which ratio do you expect?"}), ("wait_for_reply", {}), *steps,
                _write(run, role, "The ratio is 0.28 per the human."), _end(role),
                _write(run, role, lambda sent: f'The ratio is {{{{{sent_handle(sent)} "0.28"}}}}.'), _end(role),
                _write(run, role, d.CLAIM), _end(role),
            ])
            return run, result

    run, result = asyncio.run(go())

    assert result.error is None and result.filed, [(r.name, r.text) for r in result.tool_results]
    # Only the file was minted, as the seat's own record; the text minted nothing.
    (minted,) = [r for r in run.receipts.receipts() if r.produced_by == "human_reply"]
    assert minted.source == result.source and minted.kind == "data" and not d.is_evidence(minted)
    assert minted.sha256 == sha256_bytes(notes.encode()) and minted.path.read_text(encoding="utf-8") == notes
    assert not [r for r in run.receipts.receipts() if MARKER in (run.receipts.text(r.handle) or "")]
    assert result.sent_with(reply)  # the model read the text, as direction
    typed, quoted, accepted = _ends(result, role)
    assert typed.is_error and d.TYPED_NUMBER in typed.text
    assert quoted.is_error and f"cites {minted.handle}, a file the human sent" in quoted.text
    assert not accepted.is_error and "0.28" not in result.report.model_dump_json()


# ── §9.3.4 a brief dispatched downstream grounds nothing there ───────────────────────────────

FOLLOW_UP = f"Check that the band ratio is 0.28 and say what the {PAGE_NUMBER} means."


async def _dispatch(run: d.Run) -> d.TurnResult:
    """The reviewer files its report; its ``follow_up`` is the brief dispatched downstream."""
    content = f"# assign_to\nanalysts\n# rationale\n{d.CLAIM}\n# follow_up\n{FOLLOW_UP}\n"
    reviewer = await run.turn("reviewer", [(d.report_tools("reviewer")[0], {"content": content}),
                                           _end("reviewer")])
    assert reviewer.filed, [(r.name, r.text) for r in reviewer.tool_results]
    return reviewer


def _brief_handle(run: d.Run) -> str:
    (brief,) = [r for r in run.receipts.receipts() if r.produced_by == "dispatch"]
    return brief.handle


@pytest.mark.parametrize("receiver", ["worker", "lead"])
def test_a_dispatched_follow_up_is_a_brief_receipt_that_grounds_nothing(receiver, tmp_path):
    # §9.3.4 a reviewer follow_up, dispatched, grounds nothing in the receiver
    async def go():
        async with d.open_run(tmp_path) as run:
            reviewer = await _dispatch(run)
            # The brief's receipt exists once the receiver's turn starts; its handle is named when the call is made.
            quote = lambda sent: f'The brief said {{{{{_brief_handle(run)} "{PAGE_NUMBER}"}}}}.'  # noqa: E731
            result = await run.turn(receiver, [
                _write(run, receiver, quote), _end(receiver), _write(run, receiver, d.CLAIM), _end(receiver),
            ], handed=[reviewer])
            return run, result

    run, result = asyncio.run(go())

    assert result.error is None and result.filed, [(r.name, r.text) for r in result.tool_results]
    brief = run.receipts.get(_brief_handle(run))
    assert PAGE_NUMBER in run.receipts.text(brief.handle) and not d.is_evidence(brief)
    refused, accepted = _ends(result, receiver)
    assert refused.is_error and f"cites {brief.handle}, {d.TASK_BRIEF}" in refused.text and not accepted.is_error
    assert PAGE_NUMBER not in result.report.model_dump_json()


def test_a_follow_up_handed_to_the_librarian_is_text_and_no_receipt_holds_it(tmp_path):
    # §9.3.4 a follow_up, dispatched, grounds nothing downstream: the librarian is handed it as text only
    async def go():
        async with d.open_run(tmp_path) as run:
            reviewer = await _dispatch(run)
            request = [r.handle for r in run.receipts.receipts() if r.produced_by == "research_request"]
            result = await run.turn("librarian", [
                *run.evidence_steps("librarian"),
                _write(run, "librarian", lambda sent: f'The brief said {{{{{request[0]} "{PAGE_NUMBER}"}}}}.'),
                _end("librarian"), _write(run, "librarian", d.CLAIM), _end("librarian"),
            ], handed=[reviewer])
            return run, request, result

    run, request, result = asyncio.run(go())

    assert result.error is None and result.filed, [(r.name, r.text) for r in result.tool_results]
    assert result.sent_with(FOLLOW_UP)  # the librarian was handed the brief as text
    # No receipt of the run holds it: there is no brief receipt for the librarian to cite.
    assert not [r for r in run.receipts.receipts() if PAGE_NUMBER in (run.receipts.text(r.handle) or "")]
    assert not [r for r in run.receipts.receipts() if r.produced_by == "dispatch"]
    # The request's receipt holds the question and no such span: a quote of the brief's number is refused.
    (request_handle,) = request
    quoted, accepted = _ends(result, "librarian")
    assert quoted.is_error and request_handle in quoted.text and not accepted.is_error
    assert PAGE_NUMBER not in result.report.model_dump_json()


def test_a_tool_request_and_the_tool_brief_it_leads_to_ground_nothing_in_the_lead(tmp_path):
    # §9.3.4 a worker's tool_request, dispatched, grounds nothing in the lead; the lead's own ToolSpec is "a tool brief"
    async def go():
        async with d.open_run(tmp_path, pipeline=True, config={"tool_author_submit_cap": 1}) as run:
            content = d.report_content("worker", tool_request=d.NEW_TOOL).replace(
                "Total counts", f"Total {PAGE_NUMBER} counts")
            assert PAGE_NUMBER in content
            worker = await run.turn("worker", [(d.report_tools("worker")[0], {"content": content}), _end("worker")])
            toolspec = lambda sent: next(  # noqa: E731
                r.handle for r in run.receipts.receipts() if r.produced_by == d.TOOLSPEC)
            lead = await run.turn("lead", [
                d.brief_step(), d.submit_step(),
                _write(run, "lead", lambda sent: f"The brief is {{{{{toolspec(sent)}}}}}."), _end("lead"),
                _write(run, "lead", d.CLAIM), _end("lead"),
            ], handed=[worker], coder=[d.coder_edit()], colead=d.review_script(approved=True),
                compactor=["The coder added the tool and its test."])
            return run, worker, lead

    run, worker, lead = asyncio.run(go())

    assert worker.filed and lead.error is None and lead.filed, [(r.name, r.text) for r in lead.tool_results]
    assert lead.sent_with(PAGE_NUMBER)  # the lead was handed the request
    # The tool_request mints no receipt, so nothing holds its number as evidence.
    assert not [r for r in run.receipts.receipts() if PAGE_NUMBER in (run.receipts.text(r.handle) or "")]
    (spec,) = [r for r in run.receipts.receipts() if r.produced_by == d.TOOLSPEC]
    assert not d.is_evidence(spec)
    refused, accepted = _ends(lead, "lead")
    assert refused.is_error and f"cites {spec.handle}, {d.TOOL_BRIEF}" in refused.text and not accepted.is_error


def test_a_leads_what_to_probe_grounds_nothing_in_the_workers(tmp_path):
    # §9.3.4 a lead's what_to_probe, dispatched, grounds nothing in the workers
    probe = f"Check the {PAGE_NUMBER} counts."

    async def go():
        async with d.open_run(tmp_path) as run:
            rework = d.report_content("lead", what_to_probe=probe)
            lead = await run.turn("lead", [(d.report_tools("lead")[0], {"content": rework}), _end("lead")])
            quote = lambda sent: f'The probe said {{{{{_probe_handle(run)} "{PAGE_NUMBER}"}}}}.'  # noqa: E731
            handoff = await run.consume(lead, [_write(run, "worker", quote), _end("worker"),
                                               *run.file_script("worker")])
            return run, lead, handoff

    run, lead, handoff = asyncio.run(go())

    assert lead.filed and lead.report.rework and handoff.consumer == "worker"
    worker = handoff.turn
    assert worker.error is None and worker.filed, [(r.name, r.text) for r in worker.tool_results]
    assert probe in handoff.shown()  # the worker was handed the probe as text
    brief = run.receipts.get(_probe_handle(run))
    assert PAGE_NUMBER in run.receipts.text(brief.handle) and not d.is_evidence(brief)
    refused, accepted = _ends(worker, "worker")[:2]
    assert refused.is_error and f"cites {brief.handle}, {d.TASK_BRIEF}" in refused.text and not accepted.is_error
    assert PAGE_NUMBER not in worker.report.model_dump_json()


def _probe_handle(run: d.Run) -> str:
    (brief,) = [r for r in run.receipts.receipts() if PAGE_NUMBER in (run.receipts.text(r.handle) or "")]
    return brief.handle
