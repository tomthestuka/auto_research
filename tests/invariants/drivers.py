"""The freeze suite's driver: the only role-aware code in ``tests/invariants`` (Phase 3 step 0).

A :class:`Run` builds the four graph seats the way ``graph.runtime.run_research`` builds them, on
the minimal fixture toolkit (``tests/fixtures/toolkit.py``) configured through
``AUTO_RESEARCH_CONFIG_DIR``, in a runtime dir of its own: the run's launch step (``RunLaunch``:
contract and stores bound), the preflight's database step, the environment reconcile and stamp, the
cost meter and drift alert, each role's class and its ``attach_*`` seam (``wire_lead`` for the
lead), the registry, the workers' jailed shell server, the worker's jailed REPL
(``attach_worker_runtime``) and the quick channel (``attach_channel``). :meth:`Run.turn` runs one
turn of a role through its own entry point, with the arguments the graph and the debate pass:

=========  =================================  ==========================================
role       entry point                        built by
=========  =================================  ==========================================
reviewer   ``ReviewerAgent.review``           ``attach_reviewer_runtime``
librarian  ``LibrarianIOAgent.research``      ``attach_librarian_runtime``
worker     ``AnalystWorkerAgent.analyze``     ``attach_worker_runtime``
lead       ``AnalystLeadAgent.run_cycle``     ``wire_lead``
=========  =================================  ==========================================

What is faked lies outside the code under test: the model's provider network (a
:class:`ScriptedProvider` adapter beneath a real ``ModelHandle``, so the sanitiser, the retry and
every loop run for real), the literature sources' search network, the web network
(``WebSource.search`` / ``WebSource.fetch``, for every seat) and the PDF download
(``sources.downloads.download_pdf``). The worker's tools run in production's jailed REPL on the
fixture toolkit. The configuration sets the provider retry cooldowns to zero (timing only). Every
run authenticates with stand-in provider keys in a key pool of its own (W4).

Two options add what ``run_research`` adds:

- ``human=``: the person at the human channel (:class:`ScriptedHuman`), wired as ``run_research``
  wires a ``human_handler``: ``attach_channel``, the workers, the lead's cycle and the reviewer's
  attended flag. A seat's ``message(to="human")`` reaches it through the real ``SeatRouter``; the
  reply comes back as a nudge, and its files are minted by ``mint_reply_attachments``.
- ``pipeline=True``: the lead's coding pipeline. A ``models.yaml`` names stand-in members (the
  launch's decorrelation and harness checks run on it); production's ``_ChainDriver`` builds each
  link's ``AuthoringSession``, the real ``run_gate`` judges it (its tests run in the jail), the real
  review hook builds the co-lead from the registry, and ``approve_edit`` commits and mints the
  ``commit`` receipt. Faked: the author process (the Claude SDK client, ``coder=`` edits), the
  co-lead's and the link compactor's provider networks (``colead=``, ``compactor=`` scripts).

:meth:`Run.resume` delivers a text to a seat whose turn filed, as the channel delivers it, and
returns the resumed turn's observables; its ``report`` is read back from the record production
keeps (:meth:`Run.report_file`), never remembered by the driver.

Role-neutral access a clause test uses instead of naming a role's internals:

- :attr:`TurnResult.outcome` (:class:`Outcome`): filed or not, what raised, the failure the unit
  recorded on the ledger, and what the graph or the debate receives from the turn;
- :meth:`Run.harness_calls` / :meth:`Run.harness_call` (:class:`HarnessCall`): a role's harness
  tools that mint navigation receipts, with minimal arguments, receipt letter and producer;
- ``workers=2`` and :meth:`Run.peer_result`: a second worker seat's real toolkit result;
- :data:`TRUSTED_BRANCH`, ``Run.trusted_tip``, :meth:`Run.branch_tips`: the toolkit's trusted branch;
- :func:`run_chain` (:class:`Chain`): one coding link end to end; ``conftest.approved_chain`` shares
  the approved one across tests; ``colead_members=`` (``Run``'s ``pipeline_colead=``) sets the
  co-lead group's vendors;
- :meth:`Run.consume` (:class:`Handoff`): a turn's outcome handed to the unit the graph or the debate
  hands it to (a failed pass reaches the reviewer as the graph's notice; a lead's rework reaches
  a worker), or the run ending instead;
- :meth:`Run.ledger_events`, :meth:`Run.inbox_of` (:class:`Text`; ``"human"``: queries and
  notices), :meth:`Run.colead_calls`, :meth:`Run.coder_briefs`, :attr:`Run.root`,
  :meth:`Run.workdir`: what the run recorded, what its actors were sent, and where they work;
- :meth:`TurnResult.results_named`, :meth:`~TurnResult.declared`, :meth:`~TurnResult.sent_after`,
  :meth:`~TurnResult.sent_with`: a tool result or a model call found by what it holds; the event
  helpers (:meth:`~TurnResult.grace_opened`, ``grace_exhausted``, ``nudges``, ``silent_turns``,
  ``commits``, ``tree_resets``);
- production's words and event names, written once (:data:`NOT_RUN`, :data:`TYPED_NUMBER`, ...,
  :data:`COMMIT_EVENT`), and :func:`is_evidence` (the predicate step 2 binds per unit);
- reach probes that write nothing: :meth:`Run.repl_writable` (the worker's REPL),
  :func:`writability_probe` / :func:`probed_writable` (the worker's shell), and
  :func:`tree_snapshot` / :func:`tree_changes` (the repository and the analysis environment);
- ``shapes=True``: the run's toolkit copy gains :data:`FAILING_PRODUCT_TOOL` and
  :data:`MISDECLARED_FIGURE_TOOL`, whose returns the runtime corrects;
- ``ScriptedHuman(optional=True)``: replies the run may leave unused;
- :data:`ModelCallFailed`, :data:`ReactBudgetExhausted`: the production exceptions, re-exported.

A script is a list with one item per model call. An item is:

- ``(tool, arguments)``: one tool call; a list of such pairs: one model turn's batch of calls;
- a ``str``: a prose turn (``stop="end_turn"``);
- :data:`STOP`: the model stops (``stop="refusal"``), a provider's own way to end a turn with no call;
- a ``ModelResponse``: returned as it is (a truncation, an empty turn, ...);
- an exception instance: raised by the provider, as the network would;
- a callable ``item(sent: Sent)``: returns one of the above, chosen from what this call was sent.

A script is exact: a model call past its end, a malformed item, a callable item that raises, or an
item left unused when the turn ends raises :class:`ScriptError`, which no containment in the code
under test catches, so a test's own mistake never reads as the unit's failure.

Observables are the :class:`TurnResult` fields a clause test asserts on: ``report``, ``error``,
``run_failure``, ``events``, ``receipts``, ``tool_results``, ``sent``. ``returned`` (the entry point's
own return value) is the driver's: its type is the role class's, so only ``test_drivers.py``, which
step 1 rewrites with this file, reads it.

Phase 3 step 1 rewrites this file when the four role classes become one unit; the public surface
names roles, never classes, so the clause tests stay as they are.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

# The production exceptions a clause test names, re-exported here so step 1 moves only this import:
# a model call that raised (contained, the unit's honest failure) and an exhausted budget (never caught).
from auto_research.agents.roles import ModelCallFailed, ReactBudgetExhausted  # noqa: F401
from auto_research.models.types import ModelResponse, RoleModelConfig, ToolCall

#: The four graph roles the freeze drives.
ROLES = ("reviewer", "librarian", "worker", "lead")

#: The toolkit repository's trusted branch: the one a human merges into, which the runtime never
#: names. Every run's fixture toolkit holds it at the authoring branch's base (master-blindness, §9.3.6).
TRUSTED_BRANCH = "main"

#: The request every turn serves unless a test names another. No digits, so nothing in it is a number.
QUESTION = "Tabulate the band ratios of the reduced product and state what they show."
#: A claim with no number in it: a report holding only this needs no citation.
CLAIM = "The band ratios are tabulated for every row of the product."
#: The paper the literature source serves: its search hit title and identifier.
PAPER_TITLE = "Band ratios of reduced products"
PAPER_ARXIV = "2101.00001"
#: The page the web network serves for any address.
WEB_PAGE = "# A page\nThe page describes how band ratios are tabulated.\n"

# ── production's words and ledger events a clause test matches ──────────────────────────────
# One place for step 1 to update. Production states each phrase inline in an f-string (none is a
# module constant), so each is written here once, with the site it is taken from.

#: A call placed after an accepted ``turn_end`` in the same batch (``agents/roles.py``, ``_react_loop``:
#: "``<tool>`` was not run: your turn already ended with an accepted ``<terminal>``").
NOT_RUN = "was not run: your turn already ended with an accepted"
#: A number typed outside a citation (``provenance/bindings.py``, the typed-number problem).
TYPED_NUMBER = "types numbers outside a citation"
#: A cited handle that names no receipt in the citer's reach (``provenance/bindings.py``, ``entry``).
#: The report's figure and table checks (``report/``) use the same words, so a clause matches it after
#: "cites <handle>, ".
NOT_CITABLE = "which is not a receipt you can cite"
#: A cited receipt that is a record of where a role looked (``provenance/bindings.py``, ``entry``),
#: after "cites <handle>, <what it is>:" (:attr:`HarnessCall.named`).
NOT_EVIDENCE = "it shows where to look, not evidence"
#: A place written after a quote of a receipt that is no paper (``provenance/bindings.py``).
LOCATOR_NOT_PAPER = "Only a paper takes a place there"
#: A paper attribution with no place in the paper (``provenance/bindings.py``).
PAPER_UNPLACED = "without saying where in it the value is"
#: A submission the CI pipeline failed (``agents/react.py``, ``_submit_now``).
CI_FAILED = "CI PIPELINE FAILED"
#: A submission the co-lead's review rejected (``agents/react.py``, ``_submit_now``).
REVIEW_REJECTED = "REVIEW REJECTED"
#: A commit refused after the gate and the review passed (``agents/react.py``, ``_commit_and_advance``).
NOT_COMMITTED = "Not committed:"
#: A ``$ref`` to a receipt flagged failed (``provenance/refs.py``, ``RefError``).
FLAGGED_FAILED = "is flagged failed"
#: An argument a tool's manifest does not declare (``provenance/inputs.py``).
UNDECLARED_ARGUMENT = "is not one this tool takes"
#: Media a model is not sent (``models/sanitise.py``, ``models/providers/_media.py``).
WITHHELD = "[withheld]"
#: A figure the tool declared with a hash its file does not have (``provenance/capture.py``,
#: ``receipt_call``): minted with the file's own hash, and the model told.
FIGURE_HASH_OVERRIDDEN = "the tool declared a hash that does not match the file"
#: A declared figure or product whose file is absent (``provenance/capture.py``, ``_product_problem``):
#: not minted, and the model told.
FILE_ABSENT = "is not a file when the call returns"

#: The producer of a commit receipt (``agents/react.py``, ``Session._commit_and_advance``).
APPROVE_EDIT = "approve_edit"
#: The producer of a locked tool brief's receipt (``agents/react.py``, ``TOOLSPEC_PRODUCER``).
TOOLSPEC = "toolspec"
#: How a refusal names a locked tool brief and a dispatched task brief (``provenance/store.py``,
#: ``NOT_EVIDENCE_NAMES``), after "cites <handle>, ".
TOOL_BRIEF = "a tool brief"
TASK_BRIEF = "a task brief"
#: A submission whose changeset or brief moved after the gate or the review judged it
#: (``sandbox/merge_flow.py``, ``approve_edit``'s guards).
CHANGED_AFTER = "changed after"

#: Ledger event names, by suffix ``<role>.<action>.<suffix>`` (``agents/roles.py``) or whole.
GRACE_OPENED = ".grace_opened"
GRACE_EXHAUSTED = ".grace_exhausted"
SILENT_TURN = ".silent_turn"
NUDGE = ".nudge"
MODEL_CALL_FAILED = ".model_call_failed"
NO_REPORT = ".no_report"
FILED = ".filed"
#: The pipeline's commit and a reset of the toolkit's tree after an uncommitted link
#: (``sandbox/merge_flow.py``), whole names.
COMMIT_EVENT = "commit"
TREE_RESET_EVENT = "coder.tree_reset"

#: The reduced product the worker's toolkit tools read (synthetic rows, marked as such).
_PRODUCT_CSV = (
    "# produced_by=invariant_freeze\nobsid,low_counts,high_counts,pa_deg,pa_err_deg\n"
    "synth260817-4d1f7a,400.0,900.0,20.0,2.0\nsynth260817-9c3b02,500.0,700.0,26.0,3.0\n"
)

#: The configuration a run sets unless a test sets otherwise: retries run for real, without waiting.
_DEFAULT_CONFIG = {"provider_retry_base_cooldown_s": 0, "provider_retry_max_cooldown_s": 0}

#: The tool the coding pipeline's scripted coder adds to ``demo_utils`` (:func:`coder_edit`), and the
#: store record a request for it names as validation data (``store | bucket | key | version``).
NEW_TOOL = "band_totals"
VALIDATION_RECORD = "db | demo | counts/s1/1a2b3c4d/rows.csv | v1"

_NEW_TOOL_LIB = '''\
"""Total counts per band over a reduced product CSV."""

from pathlib import Path

from demo_utils.lib.demo_help import read_band_rows


def band_totals(csv_path, out_dir):
    """Total low and high counts, written to ``<out_dir>/band_totals.csv`` and declared as a product."""
    rows = read_band_rows(csv_path)
    low = sum(r["low_counts"] for r in rows)
    high = sum(r["%s"] for r in rows)
    table = Path(out_dir) / "band_totals.csv"
    table.write_text(f"band,counts\\nlow,{low!r}\\nhigh,{high!r}\\n", encoding="utf-8")
    return {"stdout": f"low={low!r} high={high!r}",
            "products": [{"path": str(table), "caption": "Total counts per band"}]}
'''

_NEW_TOOL_ENTRY = '''\
    "%s.demo_utils.band_totals": {
        "module": "demo_utils.lib.demo_totals",
        "fn": "band_totals",
        "description": "Total counts per band of a reduced product CSV, as a table.",
        "args_schema": {
            "type": "object",
            "properties": {
                "csv_path": {"kind": "path", "type": "string", "description": "Path of the product CSV."},
                "out_dir": {"kind": "output", "shape": "folder", "type": "string",
                            "description": "Folder the table is written to."},
            },
            "required": ["csv_path", "out_dir"],
        },
    },
'''

_NEW_TOOL_TEST = '''\
"""Checks for the band totals, on a marked synthetic product."""

from demo_utils.lib.demo_totals import band_totals
from demo_utils.tests.test_demo_summary import write_csv


def test_band_totals_sum_each_band(tmp_path):
    (tmp_path / "out").mkdir()
    out = band_totals(write_csv(tmp_path / "product.csv"), str(tmp_path / "out"))
    assert out["stdout"] == "low=1200.0 high=1900.0"
'''

#: The provider-key variables a run replaces with stand-ins (W4): each rotating provider's primary
#: and fallback, so a scripted key-scoped error rotates or retires only the run's own stand-ins.
_STAND_IN_KEYS = {
    name: f"freeze-{name.lower()}"
    for provider in ("ANTHROPIC", "OPENAI")
    for name in (f"{provider}_API_KEY", f"{provider}_API_KEY_FALLBACK")
}


# ── the model: a provider adapter answering from a script ───────────────────────────────────


class ScriptError(BaseException):
    """The test's script, not the unit, went wrong. A ``BaseException``, so the model-call
    containment (which takes every ``Exception``) passes it on to the test."""


#: A script item: the model stops with no call (``stop="refusal"``).
STOP = "<stop>"


@dataclass
class Sent:
    """What one model call was sent, as the provider adapter received it."""

    messages: list[Any]
    """The conversation (``ModelMessage`` / ``ModelResponse`` / ``ToolResultMessage``), after the
    model-input sanitiser."""
    tools: list[str]
    """The names of the tools declared on the call."""
    tool_choice: str
    images: list[Any]
    documents: list[Any]

    def text(self) -> str:
        """Everything the model could read on this call, the system prompt included."""
        system = [m.content for m in self.messages
                  if getattr(m, "role", None) == "system" and isinstance(m.content, str)]
        return "\n".join([*system, self.user_text()])

    def user_text(self) -> str:
        """What this run put before the model: every user message (task payloads, nudges) and
        every tool result, without the system prompt (whose examples name handles of no run)."""
        parts: list[str] = []
        for message in self.messages:
            content = getattr(message, "content", None)
            if getattr(message, "role", None) == "user" and isinstance(content, str):
                parts.append(content)
        return "\n".join([*parts, self.results_text()])

    def results_text(self) -> str:
        """The text of every tool result on the conversation, as the model reads it."""
        return "\n".join(str(getattr(result, "content", "")) for message in self.messages
                         for result in getattr(message, "results", None) or ())

    def result_names(self) -> list[str]:
        """The tool each result on the conversation answered, in order."""
        return [getattr(result, "name", "") for message in self.messages
                for result in getattr(message, "results", None) or ()]


class ScriptedProvider:
    """The provider network of one seat: answers each ``ModelHandle.call`` with its script's next item.

    A script error is raised as :class:`ScriptError` and kept in ``errors``, so one raised in a task
    the test does not await (a resumed turn runs in the channel's) still fails the turn."""

    def __init__(self) -> None:
        self.script: list[Any] = []
        self.sent: list[Sent] = []
        self.errors: list[ScriptError] = []
        self._calls = 0

    def load(self, script: Sequence[Any]) -> None:
        """The items the next calls are answered with, one per call."""
        if isinstance(script, (str, bytes)) or not isinstance(script, Sequence):
            raise ScriptError(f"a script is a list of items, not {type(script).__name__}")
        self.script = list(script)

    async def call(self, messages, config, *, schema=None, tools=None, tool_choice="auto",
                   images=(), documents=(), cache_prefix=False) -> ModelResponse:
        await asyncio.sleep(0)  # a network call yields to the event loop; other seats run meanwhile
        self._calls += 1
        sent = Sent(messages=list(messages), tools=[t["name"] for t in tools or ()],
                    tool_choice=tool_choice, images=list(images), documents=list(documents))
        self.sent.append(sent)
        try:
            if not self.script:
                raise ScriptError(f"model call {self._calls} came past the end of the script")
            item = self.script.pop(0)
            if callable(item):
                try:
                    item = item(sent)
                except Exception as exc:  # noqa: BLE001 - the test's own callable
                    raise ScriptError(f"script item for model call {self._calls} raised {exc!r}") from exc
            return _answer(item, self._calls)
        except ScriptError as exc:
            self.errors.append(exc)
            raise


class ResumedTurnFailed(Exception):
    """A resumed turn's failure as the channel recorded it (``channel.resume.error``): the router
    keeps a resumed turn's error as the sender's news, so the ledger's ``repr`` is what remains."""


class ActorScriptFailed(RuntimeError):
    """What a faked actor production calls from a background task (the human, the author process)
    raises when its script fails: an ordinary error, so production reports the failure at once (a
    channel fault, a stopped coder); the turn then raises the :class:`ScriptError` it recorded."""


class _Unscripted:
    """The provider network of a vendor no scripted model serves in this run: any call is the test's error."""

    def __init__(self, vendor: str) -> None:
        self.vendor = vendor

    async def call(self, *args: Any, **kwargs: Any) -> ModelResponse:
        raise ScriptError(f"a model of vendor {self.vendor!r} was called, and no script serves it in this run")


# ── the person at the human channel ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Reply:
    """One answer of the human: its text and the files sent with it (``{file name: bytes or text}``)."""

    text: str
    files: dict[str, bytes | str] | None = None


class ScriptedHuman:
    """The person at the human channel, the one thing faked on it: a ``human_handler`` answering each
    query with its script's next reply. A reply is a ``str``, a :class:`Reply`, or a callable of the
    ``HumanQuery`` returning one. A reply's files are saved into the query's ``reply_dir`` as a
    connector saves them; the runtime mints them (``hitl.attachments.mint_reply_attachments``).

    ``queries`` are the queries the human was sent. A query past the end of the script, or a callable
    reply that raises, is recorded in ``errors`` and raised as :class:`ScriptError` by the turn;
    production meets :class:`ActorScriptFailed` (the router's task calls the human).

    ``optional=True``: the run may close with replies unused (a cell whose target sends the human a
    query that today's code does not send); otherwise an unused reply is the test's error.

    ``notify(text)`` takes a notice, as the Telegram connector's does (the channel's mirror of
    seat-to-seat texts, the stop checklist); ``notices`` keeps them in order."""

    def __init__(self, replies: Sequence[Any] = (), *, optional: bool = False) -> None:
        if isinstance(replies, (str, Reply)) or not isinstance(replies, Sequence):
            raise ScriptError(f"a human script is a list of replies, not {type(replies).__name__}")
        self.replies = list(replies)
        self.optional = optional
        self.queries: list[Any] = []
        self.notices: list[str] = []
        #: Everything the human was sent, in order: ``("query", HumanQuery)`` or ``("notice", text)``.
        self.received: list[tuple[str, Any]] = []
        self.errors: list[ScriptError] = []

    async def notify(self, text: str) -> None:
        self.notices.append(text)
        self.received.append(("notice", text))

    async def __call__(self, query: Any) -> Any:
        import mimetypes

        from auto_research.schemas import HumanAttachment, HumanResponse

        self.queries.append(query)
        self.received.append(("query", query))
        try:
            if not self.replies:
                raise ScriptError(f"human query {len(self.queries)} came past the end of the human's script")
            item = self.replies.pop(0)
            if callable(item):
                try:
                    item = item(query)
                except Exception as exc:  # noqa: BLE001 - the test's own callable
                    raise ScriptError(f"human reply {len(self.queries)} raised {exc!r}") from exc
            reply = Reply(item) if isinstance(item, str) else item
            if not isinstance(reply, Reply):
                raise ScriptError(f"human reply {len(self.queries)} is not a str or a Reply: {item!r}")
            if reply.files and query.reply_dir is None:
                raise ScriptError(f"human reply {len(self.queries)} carries files, and its query names no reply_dir")
        except ScriptError as exc:
            self.errors.append(exc)
            raise ActorScriptFailed(str(exc)) from exc
        attachments = []
        for name, body in (reply.files or {}).items():
            path = Path(query.reply_dir) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(body, str):
                path.write_text(body, encoding="utf-8")
            else:
                path.write_bytes(body)
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            attachments.append(HumanAttachment(path=path, filename=name, mime_type=mime))
        return HumanResponse(query_id=query.id, answer=reply.text, attachments=attachments)


# ── the coder's author process ───────────────────────────────────────────────────────────


class _CoderScript:
    """The author process's turns, one item per turn the session drives: a ``{path: text}`` edit of
    the working tree (``None`` deletes a file), or a callable ``item(message, worktree)`` returning one.
    A script error is recorded in ``errors``; production meets :class:`ActorScriptFailed` (its drive
    runs in a background task) and the turn raises the :class:`ScriptError`."""

    def __init__(self) -> None:
        self.script: list[Any] = []
        self.briefs: list[str] = []  # every message the author process was sent, in order
        self.errors: list[ScriptError] = []

    def load(self, script: Sequence[Any]) -> None:
        if isinstance(script, (str, bytes, dict)) or not isinstance(script, Sequence):
            raise ScriptError(f"a coder script is a list of edits, not {type(script).__name__}")
        self.script = list(script)

    def next_edit(self, message: str, worktree: Path) -> dict[str, str | None]:
        n = len(self.briefs)
        try:
            if not self.script:
                raise ScriptError(f"coder turn {n} came past the end of the coder's script")
            item = self.script.pop(0)
            if callable(item):
                try:
                    item = item(message, worktree)
                except Exception as exc:  # noqa: BLE001 - the test's own callable
                    raise ScriptError(f"coder script item {n} raised {exc!r}") from exc
            if not isinstance(item, dict):
                raise ScriptError(f"coder script item {n} is not a {{path: text}} edit: {item!r}")
            return item
        except ScriptError as exc:
            self.errors.append(exc)
            raise ActorScriptFailed(str(exc)) from exc


class _ScriptedCoderClient:
    """The Claude SDK client of one coding session (``sdk_client_factory``): the author process,
    outside the code under test. Each ``query`` applies the coder script's next edit to the session's
    working tree (``options.cwd``); the response narrates it."""

    def __init__(self, options: Any, script: _CoderScript) -> None:
        self.worktree = Path(options.cwd)
        self.script = script
        self._said = ""
        self._in_turn = False

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def interrupt(self) -> None:
        return None

    async def query(self, message: str, session_id: str = "default") -> None:
        self.script.briefs.append(message)
        if self._in_turn:
            return  # a steer joins the turn in flight (``ClaudeSdkBackend.steer``); no new turn
        self._in_turn = True
        try:
            edit = self.script.next_edit(message, self.worktree)
        except BaseException:
            self._in_turn = False
            raise
        for rel, text in edit.items():
            path = self.worktree / rel
            if text is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
        self._said = f"Edited {', '.join(sorted(edit)) or 'nothing'}."

    async def receive_response(self):
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        try:
            await asyncio.sleep(0)
            yield AssistantMessage(content=[TextBlock(text=self._said)], model="fake")
            yield ResultMessage(subtype="success", duration_ms=0, duration_api_ms=0, is_error=False,
                                num_turns=1, session_id="scripted", total_cost_usd=0.0)
        finally:
            self._in_turn = False


def _answer(item: Any, n: int) -> ModelResponse:
    """One script item as the provider's answer to call ``n``."""
    if isinstance(item, BaseException):
        raise item
    if isinstance(item, ModelResponse):
        return item
    if item == STOP:
        return ModelResponse(content="", stop="refusal")
    if isinstance(item, str):
        return ModelResponse(content=item, stop="end_turn")
    batch = [item] if isinstance(item, tuple) else item
    if not isinstance(batch, list) or not batch or not all(
        isinstance(c, tuple) and len(c) == 2 and isinstance(c[0], str) and isinstance(c[1], dict)
        for c in batch
    ):
        raise ScriptError(f"script item for model call {n} is not a (tool, arguments) call or a "
                          f"batch of them: {item!r}")
    return ModelResponse(content="", stop="tool_use", tool_calls=[
        ToolCall(id=f"c{n}_{i}", name=name, arguments=dict(arguments))
        for i, (name, arguments) in enumerate(batch)
    ])


# ── what a turn shows ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ToolResultSeen:
    """One tool result the unit's loop answered, as the ledger records it."""

    name: str
    text: str
    is_error: bool
    call_id: str


@dataclass
class TurnResult:
    """The observables of one turn."""

    role: str
    unit: str
    """The unit's name: its channel address and ledger actor."""
    source: str
    """The ``<source>`` of the receipt handles the unit mints."""
    report: Any | None
    """The report the unit filed through an accepted terminal call, or ``None``."""
    error: BaseException | None
    """What ended the entry point by raising, or ``None``: its exception, or the run's failure when
    that failure cancelled the turn."""
    run_failure: BaseException | None
    """The run's failure (``RunControl.fatal``) as the turn ended: a seat's resumed turn that ended
    the run (ruling D6), or ``None``."""
    events: list[dict]
    """The ledger lines (``monitor.jsonl``) written during the turn, parsed."""
    receipts: Any
    """The run's receipt store (``provenance.ReceiptStore``)."""
    tool_results: list[ToolResultSeen]
    """Every tool result the unit's loop answered during the turn, in order."""
    sent: list[Sent]
    """What each model call of the turn was sent, in order."""
    returned: Any | None
    """What the role's entry point returned (``None`` when it raised); the driver's, see the module
    docstring."""

    outcome: "Outcome | None" = None
    """How the turn ended, role-neutrally (:class:`Outcome`)."""

    @property
    def filed(self) -> bool:
        return self.report is not None

    def event_names(self) -> list[str]:
        return [e["event"] for e in self.events]

    def handles(self, letter: str) -> list[str]:
        """The unit's own receipt handles of kind ``letter`` (``r`` result, ``p`` paper, ...) its
        tool results named this turn, first seen first."""
        return handles_in("\n".join(r.text for r in self.tool_results), self.source, letter)

    # ── finding a tool result or a model call by what it holds ──

    def results_named(self, tool: str) -> list[ToolResultSeen]:
        """The tool results of the declared tool ``tool`` (its whole name), in order."""
        return [r for r in self.tool_results if r.name == tool]

    def declared(self, suffix: str) -> str:
        """The whole name of the one tool declared this turn whose name ends with ``suffix`` (a
        toolkit tool's name carries its toolkit and package)."""
        names = sorted({n for sent in self.sent for n in sent.tools if n.endswith(suffix)})
        if len(names) != 1:
            raise LookupError(f"{len(names)} declared tools end with {suffix!r}: {names}")
        return names[0]

    def sent_after(self, tool: str, nth: int = 0) -> Sent:
        """The first model call whose conversation holds the result of call ``nth`` (0-based) of
        ``tool``: the call on which the model first read that result."""
        for sent in self.sent:
            if sent.result_names().count(tool) > nth:
                return sent
        raise LookupError(f"no model call of the {self.role}'s turn read result {nth} of {tool!r}")

    def sent_with(self, text: str) -> Sent:
        """The first model call whose run-supplied text (:meth:`Sent.user_text`) holds ``text``."""
        for sent in self.sent:
            if text in sent.user_text():
                return sent
        raise LookupError(f"no model call of the {self.role}'s turn was sent {text!r}")

    # ── the unit's ledger events ──

    def unit_events(self, suffix: str) -> list[dict]:
        """The events this turn's unit recorded whose name ends with ``suffix``, in order."""
        return [e for e in self.events if e.get("actor") == self.unit and e["event"].endswith(suffix)]

    def grace_opened(self) -> list[dict]:
        return self.unit_events(GRACE_OPENED)

    def grace_exhausted(self) -> list[dict]:
        return self.unit_events(GRACE_EXHAUSTED)

    def nudges(self) -> list[dict]:
        """Each request to go on that the loop put to the model after a turn with no tool call."""
        return self.unit_events(NUDGE)

    def silent_turns(self) -> list[bool]:
        """For each model turn with no tool call, whether the loop went on (``continued``)."""
        return [bool(e["data"].get("continued")) for e in self.unit_events(SILENT_TURN)]

    def commits(self) -> list[dict]:
        """The data of each commit the coding pipeline recorded this turn (``tool``, ``commit_sha``,
        ``branch``, ``message``)."""
        return [e["data"] for e in self.events if e["event"] == COMMIT_EVENT]

    def tree_resets(self) -> list[dict]:
        """The data of each reset of the toolkit's tree after an uncommitted link (``patch``: the
        saved patch's path, or ``None``)."""
        return [e["data"] for e in self.events if e["event"] == TREE_RESET_EVENT]


#: Ledger events (by name suffix) with which a unit records that its turn ended without a filing.
_FAILURE_SUFFIXES = (NO_REPORT, GRACE_EXHAUSTED, MODEL_CALL_FAILED)
_RESUME_FAILURES = ("channel.resume.error", "channel.resume.exhausted")


@dataclass(frozen=True)
class Outcome:
    """How a turn ended, in terms every role shares (§2b: an unfiled turn files nothing and fails
    honestly). Step 1 keeps these fields; only how the driver reads them changes."""

    filed: bool
    """The unit filed its report through an accepted terminal call."""
    raised: BaseException | None
    """What ended the turn by raising (``TurnResult.error``)."""
    recorded: tuple[dict, ...]
    """The unit's own ledger events recording a failed end: no report filed, a grace window
    exhausted, a model call that failed, a resumed turn's error."""
    handed: Any | None
    """What the graph or the debate routes on from this turn: the filed report; a failure record
    the runtime wrote; or ``None``."""
    drafted: str = ""
    """Everything the unit's model wrote into its report tools this turn (their arguments)."""

    @property
    def failure_record(self) -> bool:
        """The turn filed nothing, recorded its failure on the ledger, and hands on a record of
        status ``failed`` holding no finding and no summary its model wrote: the runtime's own
        account, never the model's unfiled draft (§2b, F5)."""
        handed = self.handed
        if self.filed or handed is None or not self.recorded or getattr(handed, "status", None) != "failed":
            return False
        summary = getattr(handed, "summary", "") or ""
        return not getattr(handed, "findings", None) and bool(summary) and summary not in self.drafted

    @property
    def failed(self) -> bool:
        """The turn filed nothing and ended as a failure: it raised, or hands on nothing, or hands on
        a failure record. A turn that filed nothing and hands on anything else is a substitute.
        Whether the failure is *recorded* is ``recorded`` (a turn that hands on nothing may record
        nothing); a clause on recording asserts that field."""
        return not self.filed and (self.raised is not None or self.handed is None or self.failure_record)


@dataclass(frozen=True)
class Text:
    """One text a seat or the human was sent (:meth:`Run.inbox_of`)."""

    sender: str
    """The sending unit's name (a query's ``asked_by``; empty for a notice)."""
    text: str
    handles: tuple[str, ...] = ()
    kind: str = "message"
    """The channel's kind (``message``, ``reply``, ...), or ``query`` / ``notice`` for the human."""


@dataclass(frozen=True)
class Handoff:
    """Where a turn's outcome went (:meth:`Run.consume`)."""

    consumer: str | None
    """The role whose turn read it; ``"human"`` for a reviewer's report to the human (the graph's
    human node, no unit: ``turn`` is ``None``); ``None`` when the graph or the debate ends the run."""
    turn: TurnResult | None
    """That role's turn, or ``None``."""
    ended: BaseException | None
    """When the run ends instead: what the turn raised, which the graph lets end it (``None`` for a
    lead whose cycle returned no report, which the debate refuses to pass on)."""

    def shown(self) -> str:
        """What the consumer's model was sent first (:meth:`Sent.user_text`); empty when no one read it."""
        return self.turn.sent[0].user_text() if self.turn is not None and self.turn.sent else ""


def _outcome(filed: bool, error: BaseException | None, events: list[dict], unit: str,
             handed: Any | None) -> Outcome:
    from auto_research.agents.react import EDIT_REPORT_TOOL, WRITE_REPORT_TOOL

    recorded = tuple(e for e in events if e.get("actor") == unit and (
        e["event"].endswith(_FAILURE_SUFFIXES) or e["event"] in _RESUME_FAILURES))
    drafted = []
    for e in events:
        if e.get("actor") == unit and e["event"].endswith(".tool_call") and \
                e["data"].get("tool") in (WRITE_REPORT_TOOL, EDIT_REPORT_TOOL):
            try:
                drafted.extend(str(v) for v in json.loads(e["data"].get("args") or "{}").values())
            except ValueError:
                drafted.append(str(e["data"].get("args")))
    return Outcome(filed=filed, raised=error, recorded=recorded, handed=handed, drafted="\n".join(drafted))


def _accepted_terminal(results: Sequence[ToolResultSeen]) -> bool:
    """The unit's terminal call (``turn_end``) was accepted in this turn: the filing's own
    evidence, independent of what the entry point returns."""
    from auto_research.agents.react import TURN_END_TOOL

    return any(r.name == TURN_END_TOOL and not r.is_error for r in results)


@dataclass(frozen=True)
class HarnessCall:
    """One harness tool of a role whose return the runtime mints as a navigation receipt."""

    kind: str
    """What it does, the same for every role: ``read_file``, ``list_dir``, ``web_fetch``,
    ``web_search``, ``text_search`` (the knowledge base), ``shell``, ``list_env`` or ``use_env``."""
    tool: str
    """The tool's declared name for this role."""
    arguments: dict
    """Minimal valid arguments in this run."""
    letter: str
    """The letter of the receipt handle its return is minted under."""
    producer: str
    """The receipt's ``produced_by``."""

    @property
    def named(self) -> str:
        """How a refusal names the receipt, in production's words (``NOT_EVIDENCE_NAMES``)."""
        from auto_research.provenance.store import NOT_EVIDENCE_NAMES

        return NOT_EVIDENCE_NAMES.get(self.producer, f"a {self.producer} return")

    def step(self, **arguments: Any) -> tuple[str, dict]:
        """The script item calling it, ``arguments`` replacing or adding to the minimal ones."""
        return self.tool, {**self.arguments, **arguments}


#: Per role, the harness tools that mint a navigation receipt: (kind, tool, letter, producer).
_HARNESS = {
    "reviewer": [("read_file", "read_file", "r", "read_file"), ("list_dir", "list_dir", "r", "list_dir"),
                 ("web_fetch", "web_fetch", "w", "web_fetch"), ("web_search", "web_search", "r", "web_search"),
                 ("text_search", "text_search", "r", "text_search")],
    "librarian": [("web_fetch", "web_fetch", "w", "web_fetch")],
    "worker": [("read_file", "read_file", "r", "read_file"),
               ("list_dir", "mcp_builtin_list_dir", "r", "mcp.builtin.list_dir"),
               ("web_fetch", "web_fetch", "w", "web_fetch"), ("web_search", "web_search", "r", "web_search"),
               ("shell", "mcp_builtin_bash_exec", "r", "mcp.builtin.bash_exec"),
               ("list_env", "list_env", "r", "list_env"), ("use_env", "use_env", "r", "use_env")],
    "lead": [("read_file", "read_file", "r", "read_file"), ("list_dir", "list_dir", "r", "list_dir"),
             ("web_fetch", "web_fetch", "w", "web_fetch"), ("web_search", "web_search", "r", "web_search")],
}


# ── the literature and web networks ───────────────────────────────────────────────────────


class _Source:
    """A literature search source serving fixed hits (the source's network, faked)."""

    def __init__(self, hits: Sequence[Any]) -> None:
        self.hits = list(hits)

    async def search(self, query: str, *, limit: int = 10) -> list[Any]:
        return self.hits[:limit]


async def _web_search(self, query: str, *, limit: int = 10) -> list[Any]:
    """``WebSource.search`` without the network: no hits."""
    return []


async def _web_fetch(self, url: str, *, depth: str = "advanced") -> str:
    """``WebSource.fetch`` without the network: :data:`WEB_PAGE` for any address."""
    return WEB_PAGE


def _download(url: str, destination: Path) -> Path:
    """The PDF download: a small, valid PDF in place of the network fetch."""
    from tests.media_files import pdf_bytes

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(pdf_bytes(PAPER_TITLE))
    return destination


def _sources() -> dict[str, Any]:
    """The librarian's sources as production names them, the search ones serving one paper."""
    from auto_research.schemas import SearchResult
    from auto_research.sources import WebSource

    hit = SearchResult(
        source="arxiv", title=PAPER_TITLE, url=f"https://arxiv.org/abs/{PAPER_ARXIV}",
        pdf_url=f"https://arxiv.org/pdf/{PAPER_ARXIV}", abstract="Band ratios are tabulated.",
        identifiers={"arxiv_id": PAPER_ARXIV},
    )
    return {"arxiv": _Source([hit]), "nasa_ads": _Source([]), "web": WebSource()}


# ── the run ──────────────────────────────────────────────────────────────────────────────


def _clear_config_caches() -> None:
    """Forget the caps a process reads once, so this run reads its own configuration as a fresh
    process would, and the next test reads its own."""
    from auto_research.agents import roles

    roles._react_loop_cap.cache_clear()
    roles._structured_retry_cap.cache_clear()


def _set_env(stack: contextlib.ExitStack | contextlib.AsyncExitStack, name: str, value: str) -> None:
    """Set an environment variable for the run (its subprocesses inherit it); restored on close."""
    previous = os.environ.get(name)
    os.environ[name] = value

    def restore() -> None:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous

    stack.callback(restore)


class Run:
    """One run with all four seats wired as production wires them; ``async with`` opens and closes it.

    ``config``: ``orchestration.yaml`` keys to set or replace (``None`` removes one), such as the
    caps ``react_loop_cap``, ``react_loop_absolute_cap`` and ``react_grace_rounds``.
    ``provider``: the vendor every seat's member names (its capability-table stand-in ``fake``).
    ``human``: the person at the human channel, a :class:`ScriptedHuman` or its list of replies;
    wired as ``run_research`` wires a ``human_handler`` (the channel, the workers, the lead's cycle,
    the reviewer's attended flag), so ``human`` is a channel address. ``None``: no human attends.
    ``pipeline``: the coding pipeline as production runs it for the lead (the coder, the gate, the
    co-lead's review, the commit); a turn scripts its faked actors with ``Run.turn``'s ``coder``,
    ``colead`` and ``compactor``. The toolkit then holds no store tool, so the gate judges a change
    with no store running (a brief's validation record is checked only when the tree has a store
    tool; that path is ``tests/test_gate_store.py``'s).

    Every run authenticates with stand-in provider keys and a key pool of its own (W4): a scripted
    key-scoped error rotates or retires only those, and the process's keys and pools are as they were
    once the run closes.

    ``workers``: how many analyst worker seats the run wires (production's ``len(workers)``). Seat 0
    is the role ``worker``; a turn of another seat names it, ``Run.turn("worker", ..., seat=1)``,
    and :meth:`peer_result` drives one to a real toolkit result of its own.

    ``pipeline_colead``: the vendors of the co-lead group's members, in order (default: one
    ``openai`` member). The co-lead's script serves ``openai``; a call to any other member's vendor
    is the test's error, so a group whose first member shares the coder's vendor (``("anthropic",
    "openai")``) shows which member production chose (:meth:`colead_calls`).

    ``shapes``: the run's own toolkit copy gains two tools whose returns the runtime corrects
    (:data:`FAILING_PRODUCT_TOOL`, :data:`MISDECLARED_FIGURE_TOOL`), committed before the launch.

    The fixture toolkit's repository holds :data:`TRUSTED_BRANCH` beside the authoring branch, at
    the authoring branch's base (``trusted_tip``), as a toolkit a human merges into does.
    """

    def __init__(self, tmp_path: Path, *, config: dict[str, Any] | None = None,
                 provider: str = "anthropic", human: Any = None, pipeline: bool = False,
                 workers: int = 1, pipeline_colead: Sequence[str] = ("openai",),
                 shapes: bool = False) -> None:
        if workers < 1:
            raise ValueError("a run wires at least one worker seat")
        if "openai" not in pipeline_colead or "google" in pipeline_colead:
            raise ValueError("the co-lead's script serves the openai member, and google is the compactor's")
        self.pipeline_colead = tuple(pipeline_colead)
        self.shapes = shapes
        self.run_id = f"freeze-{uuid.uuid4().hex[:12]}"
        self.tmp_path = Path(tmp_path) / self.run_id  # its runtime dir, toolkit and configuration
        self.config = {**_DEFAULT_CONFIG, **(config or {})}
        self.provider = provider
        self.models = {role: ScriptedProvider() for role in ROLES}
        #: The provider networks of worker seats 1..n-1 (seat 0's is ``models["worker"]``).
        self.peer_models = [ScriptedProvider() for _ in range(workers - 1)]
        self.human = human if human is None or isinstance(human, ScriptedHuman) else ScriptedHuman(human)
        self.pipeline = pipeline
        #: The pipeline's faked actors: the co-lead's and the compactor's provider networks (the
        #: vendors ``openai`` and ``google`` in the run's ``models.yaml``), and the author process.
        self.aux = {"colead": ScriptedProvider(), "compactor": ScriptedProvider()} if pipeline else {}
        self.coder = _CoderScript() if pipeline else None
        self.router: Any = None
        #: Where production stored each seat's report filed last, and its type: ``(role, seat)`` ->
        #: ``(path, report type)``.
        self._report_files: dict[tuple[str, int], tuple[Path, type]] = {}
        self.seats: dict[str, Any] = {}
        self.workers: list[Any] = []
        #: Each worker seat's REPL (production's jailed ``PersistentSandbox``), by seat.
        self.sandboxes: list[Any] = []
        self.product: Path | None = None
        self.receipts: Any = None
        self.ledger: Path | None = None
        #: The human's replies to the reviewer's reports and the files they sent, accumulated over the
        #: run as the graph's state accumulates them (``human_nudges``, ``human_thread``).
        self._nudges: list[str] = []
        self._human_thread: list[str] = []
        self.trusted_branch = TRUSTED_BRANCH
        self.trusted_tip: str | None = None
        self._stack = contextlib.AsyncExitStack()
        self._passes = {"librarian": 0, "lead": 0}  # the passes each capped team has spent
        #: The analysis pass in progress: its debate round (0 first), and every worker's tool results
        #: of the round the lead last judged (the debate's one snapshot per round, `_tool_results`).
        self._debate_round = 0
        self._round_snapshot: list[Any] | None = None

    def _seat(self, role: str, seat: int = 0) -> Any:
        if seat == 0:
            return self.seats[_check(role)]
        if role != "worker" or not 0 < seat < len(self.workers):
            raise ValueError(f"no {role} seat {seat} in this run (workers={len(self.workers)})")
        return self.workers[seat]

    def _model(self, role: str, seat: int = 0) -> ScriptedProvider:
        return self.models[_check(role)] if seat == 0 else self.peer_models[seat - 1]

    def unit(self, role: str, seat: int = 0) -> str:
        """The unit name of ``role``'s seat in this run (its ``agent_key``)."""
        return self._seat(role, seat).agent_key

    def source(self, role: str, seat: int = 0) -> str:
        """The receipt source of ``role``'s seat in this run."""
        from auto_research.agents.librarian_tools import LIBRARIAN_SOURCE
        from auto_research.agents.reviewer_tools import REVIEWER_SOURCE

        sources = {"reviewer": REVIEWER_SOURCE, "librarian": LIBRARIAN_SOURCE}
        return sources.get(_check(role)) or self.unit(role, seat)

    def report_file(self, role: str, seat: int = 0) -> Path | None:
        """Where production stores the report ``role``'s seat filed last in this run (the report
        record beside the ledger, rewritten by every accepted write), or ``None`` before it filed."""
        stored = self._report_files.get((_check(role), seat))
        return stored[0] if stored else None

    def branch_tips(self) -> dict[str, str]:
        """Every branch of the toolkit's repository and its tip."""
        from tests.fixtures.toolkit import git

        lines = git(self.toolkit_root, "for-each-ref", "--format=%(refname:short) %(objectname)",
                    "refs/heads").split("\n")
        return dict(line.split(" ", 1) for line in lines if line.strip())

    async def __aenter__(self) -> "Run":
        try:
            await self._open()
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._stack.aclose()
        if exc_info[0] is None and self.human is not None and self.human.replies and not self.human.optional:
            raise ScriptError(f"{len(self.human.replies)} human reply(ies) left unused when the run "
                              f"closed: {self.human.replies!r}")

    async def _open(self) -> None:
        from auto_research.agents import AnalystLeadAgent, AnalystWorkerAgent, LibrarianIOAgent, ReviewerAgent
        from auto_research.config_paths import CONFIG_DIR_ENV
        from auto_research.env import RUNTIME_DIR_ENV
        from auto_research.graph.runtime import (
            RunLaunch, SandboxSettings, _reconcile_environments, attach_channel, attach_librarian_runtime,
            attach_reviewer_runtime, attach_worker_runtime, issue_unit_keys, run_reconciler,
            stamp_run_environments, wire_lead, worker_mcp_client,
        )
        from auto_research.models.types import ModelHandle
        from auto_research.monitor import RunMonitor
        from auto_research.observability import install_cost_meter
        from auto_research.provenance import ReceiptStore
        from auto_research.provenance.store import install_drift_alert
        from auto_research.retrieval import SqliteRetrievalBackend
        from auto_research.run_control import RunControl
        from auto_research.sandbox.registry import ToolkitRegistry
        from auto_research.sandbox.workspace import create_run_workspace, ledger_dir, run_dir
        from auto_research.sources import WebSource, downloads
        from auto_research.storage import Storage, check_database_health
        from tests.fixtures import toolkit as fixture

        stack = self._stack
        # A runtime dir of its own (its knowledge base, papers and runs), then the configuration as
        # a run on the fixture toolkit reads it: the toolkit and the run workspaces in the
        # allow-list, the analysis environment this interpreter.
        _set_env(stack, RUNTIME_DIR_ENV, str(self.tmp_path / "runtime"))
        # W4: stand-in provider keys and a key pool of the run's own, the process's put back on close.
        from auto_research.models import keys

        for name, value in _STAND_IN_KEYS.items():
            _set_env(stack, name, value)
        stack.enter_context(mock.patch.dict(keys._POOLS, clear=True))
        prefix = Path(sys.prefix)
        self.toolkit_root = fixture.build_toolkit(self.tmp_path / "toolkit", env_base=prefix.name)
        if self.pipeline:
            _drop_store_tools(self.toolkit_root)
        if self.shapes:
            _add_shape_tools(self.toolkit_root)
        fixture.git(self.toolkit_root, "branch", TRUSTED_BRANCH)
        self.trusted_tip = fixture.git(self.toolkit_root, "rev-parse", TRUSTED_BRANCH).strip()
        config_dir = fixture.write_config(
            self.tmp_path / "config", self.toolkit_root, analysis_prefix=prefix,
            allow=(self.toolkit_root, run_dir(self.run_id).parent), **self.config,
        )
        _set_env(stack, CONFIG_DIR_ENV, str(config_dir))
        stack.callback(_clear_config_caches)
        _clear_config_caches()
        if self.pipeline:
            self._wire_pipeline(stack, config_dir)
        # The networks outside the code under test.
        stack.enter_context(mock.patch.object(downloads, "download_pdf", _download))
        stack.enter_context(mock.patch.object(WebSource, "search", _web_search))
        stack.enter_context(mock.patch.object(WebSource, "fetch", _web_fetch))

        # The run's launch, as run_research takes it; of its preflight, the one step a seat sees
        # (the knowledge base's schema), since the rest is the launch checklist.
        self.ledger = ledger_dir(self.run_id) / "monitor.jsonl"
        monitor = RunMonitor(chatter=10, jsonl_path=self.ledger, run_id=self.run_id)
        self.monitor = monitor
        monitor.cost_meter = install_cost_meter(run_id=self.run_id, monitor=monitor)
        install_drift_alert(monitor)
        stack.callback(install_drift_alert, None)
        launch = RunLaunch(self.run_id, monitor)
        stack.push_async_callback(launch.end)
        orch = await launch.start()
        self.orch = orch
        check_database_health()
        await _reconcile_environments(self.toolkit_root, monitor)
        settings = SandboxSettings.from_config(orch)
        stamp_run_environments(self.run_id, analysis=settings.environment)
        control = RunControl()
        launch.hold(control)
        control.register_run_task(asyncio.current_task())
        stack.callback(control.register_run_task, None)
        self.control = control

        def handle(cls: Any, role: str, seat: int = 0) -> Any:
            return ModelHandle(
                role=cls.role, provider=self._model(role, seat),
                config=RoleModelConfig(name="fake", provider=self.provider, model="fake"),
            )

        storage = Storage()
        retrieval = SqliteRetrievalBackend(storage)
        reviewer = ReviewerAgent(model=handle(ReviewerAgent, "reviewer"), monitor=monitor)
        librarian = LibrarianIOAgent(sources=_sources(), storage=storage,
                                     model=handle(LibrarianIOAgent, "librarian"), monitor=monitor)
        lead = AnalystLeadAgent(model=handle(AnalystLeadAgent, "lead"), monitor=monitor)
        workers = [AnalystWorkerAgent(model=handle(AnalystWorkerAgent, "worker", i), monitor=monitor,
                                      worker_index=i, human_handler=self.human)
                   for i in range(1 + len(self.peer_models))]
        worker = workers[0]
        self.workers = workers
        create_run_workspace(self.run_id, ["reviewer", "analyst_lead", "librarian_io",
                                           *(f"analyst_worker_{i}" for i in range(len(workers)))])
        attach_librarian_runtime(librarian, run_id=self.run_id, run_control=control)
        web_source = WebSource()
        attach_reviewer_runtime(reviewer, run_id=self.run_id, toolkit_root=self.toolkit_root,
                                run_control=control, retrieval=retrieval, web_source=web_source)
        await wire_lead(lead, run_id=self.run_id, toolkit_root=self.toolkit_root, keys=launch.keys,
                        run_monitor=monitor, run_control=control, retrieval=retrieval,
                        web_source=web_source)
        for each in workers:
            each.run_control = control
        registry = ToolkitRegistry(self.toolkit_root, disabled_tools=orch.get("disabled_tools") or [])
        self.registry = registry
        mcp_client = worker_mcp_client(workers, run_id=self.run_id, toolkit_root=self.toolkit_root,
                                       sandbox_settings=settings)
        stack.push_async_callback(mcp_client.shutdown)
        # The coding sessions still open close after the sandboxes, before the tools server, as
        # run_research's teardown closes them (C2c; leaf to root, D8).
        stack.push_async_callback(_close_sessions, control)
        await mcp_client.start()
        registry.load_mcp(mcp_client)
        unit_keys = await issue_unit_keys(launch.keys, [f"analyst_worker_{i}" for i in range(len(workers))],
                                          monitor)
        self.sandboxes = attach_worker_runtime(
            workers, run_id=self.run_id, registry=registry, toolkit_root=self.toolkit_root,
            sandbox_settings=settings, mcp_client=mcp_client, run_control=control, credentials=unit_keys,
            reconciler=next((run_reconciler(launch.keys, c.store) for c in unit_keys.values()), None),
        )
        for sandbox in self.sandboxes:
            stack.push_async_callback(sandbox.shutdown)
        # A seat a text resumed outlives no run: its channel tasks end before the sandboxes close.
        stack.push_async_callback(control.cancel_resumed, timeout=30)
        self.router = attach_channel((reviewer, librarian, lead, *workers), run_id=self.run_id,
                                     run_control=control, human_handler=self.human, monitor=monitor)
        self.seats = {"reviewer": reviewer, "librarian": librarian, "worker": worker, "lead": lead}
        self.receipts = ReceiptStore.for_run(run_dir(self.run_id))
        # The worker's data: a reduced product inside the run's workspace, which its jail reaches.
        self.product = run_dir(self.run_id) / "inputs" / "product.csv"
        self.product.parent.mkdir(parents=True, exist_ok=True)
        self.product.write_text(_PRODUCT_CSV, encoding="utf-8")

    def _wire_pipeline(self, stack: contextlib.AsyncExitStack, config_dir: Path) -> None:
        """The coding pipeline's members and faked actors: a ``models.yaml`` naming every required
        role with stand-in members (the coder ``anthropic`` on the ``claude_sdk`` harness, the
        co-lead ``openai``, the compactor ``google``), checked by the launch's own preflight items;
        the registry's provider network answering the co-lead and the compactor from their scripts;
        the author process answering from the coder script. The session, the gate, the review hook,
        the commit and its receipt are production's."""
        import yaml

        from auto_research.models import registry
        from auto_research.sandbox import merge_flow
        from auto_research.sandbox.coder.harness import check_coder_harness_supported
        from auto_research.sandbox.review import check_author_reviewer_decorrelation

        def member(vendor: str, **extra: Any) -> dict[str, Any]:
            return {"name": f"{vendor}_stand_in", "provider": vendor, "model": "fake", **extra}

        roles = {role: {"members": [member("anthropic")]} for role in sorted(registry.REQUIRED_ROLES)}
        roles["coder"] = {"members": [member("anthropic", harness="claude_sdk")]}
        roles["analyst_colead"] = {"members": [member(vendor) for vendor in self.pipeline_colead]}
        roles["compactor"] = {"members": [member("google")]}
        (config_dir / "models.yaml").write_text(yaml.safe_dump({"roles": roles}, sort_keys=False),
                                                encoding="utf-8")
        check_author_reviewer_decorrelation()
        check_coder_harness_supported()
        vendors = {"openai": self.aux["colead"], "google": self.aux["compactor"]}
        stack.enter_context(mock.patch.object(
            registry, "provider_for", lambda vendor: vendors.get(vendor) or _Unscripted(vendor)))
        stack.enter_context(mock.patch.object(
            merge_flow, "_default_sdk_client_factory",
            lambda options: _ScriptedCoderClient(options, self.coder)))

    # ── one turn ──

    async def turn(self, role: str, script: Sequence[Any], *, handed: Sequence[TurnResult] = (),
                   question: str = QUESTION, coder: Sequence[Any] | None = None,
                   colead: Sequence[Any] | None = None, compactor: Sequence[Any] | None = None,
                   seat: int = 0, nudges: Sequence[str] = (), human_files: Sequence[str] = (),
                   ) -> TurnResult:
        """Run one turn of ``role`` with its model answering from ``script``.

        ``handed``: earlier turns of this run whose work reaches this one the way the graph and the
        debate route it: the reviewer's report (its ``follow_up``) to the librarian, a worker or the
        lead; a worker's report to the lead (a worker whose turn raised reaches it as no report); a
        librarian's or the lead's filed report to the reviewer; the reviewer's own earlier reports
        to the reviewer (the graph's ``previous``; each one assigned to the human counts a published
        version). A librarian or lead pass that raised :data:`ModelCallFailed` (account sound)
        reaches the reviewer alone, as the graph's failure notice (``arrived="<team>_failed"``). A
        lead turn that filed rework reaches a worker as the debate's next round, with the peer
        worker turns handed beside it as its peers' reports. ``question``: the request. ``seat``:
        which worker seat (``workers=``).

        ``nudges`` and ``human_files`` (the reviewer, handed its report assigned to the human, in a run
        a human attends): the human's reply, as the graph's human node hands it back
        (``arrived="human"``): the reply's text, and the handles of the files it sent
        (:meth:`human_files`). Both stay with every later reviewer turn of the run, as the graph's
        state keeps them.

        With ``pipeline``: ``coder``, the author process's edits (:func:`coder_edit`), one per turn a
        session drives; ``colead``, the co-lead's model calls (:func:`review_script`); ``compactor``,
        the link summaries (one model call per link closed after a coding span). Each is exact.
        """
        call = self._entry(_check(role), question, handed, seat=seat, nudges=nudges, human_files=human_files)
        model = self._model(role, seat)
        model.load(script)
        self._load_aux(coder=coder, colead=colead, compactor=compactor)
        sent_mark, line_mark = len(model.sent), len(self._ledger_events())
        unit = self.unit(role, seat)
        before = self._records(unit)
        returned: Any = None
        error: BaseException | None = None
        try:
            returned = await call()
        except asyncio.CancelledError:
            # A seat's resumed turn failed the run and cancelled the run task to end it (ruling D6):
            # run_research ends with that failure, and so does this turn.
            if self.control.fatal is None or self.control.stop_requested:
                raise
            asyncio.current_task().uncancel()
            error = self.control.fatal
        except Exception as exc:  # noqa: BLE001 - an observable of the turn
            error = exc
        self._check_spent(role, model)
        events = self._ledger_events()[line_mark:]
        results = _tool_results(events, unit)
        handed = _handed(role, returned)
        # Filed: an accepted turn_end in this turn, and the entry point hands on the report its
        # record holds (a record this turn wrote in the unit's own folder).
        report = handed if handed is not None and _accepted_terminal(results) else None
        if report is not None:
            self._report_files[(role, seat)] = (self._written_record(unit, before, report), type(report))
        self._count_pass(role, report)
        return TurnResult(
            role=role, unit=unit, source=self.source(role, seat), report=report, error=error,
            run_failure=self.control.fatal, events=events, receipts=self.receipts,
            tool_results=results, sent=model.sent[sent_mark:], returned=returned,
            outcome=_outcome(report is not None, error, events, unit, handed),
        )

    def _count_pass(self, role: str, report: Any | None) -> None:
        """A librarian turn is one pass of its team; a lead turn ends the analysis pass (the graph's
        one ``analysis_node`` call, which holds the whole debate) unless it filed rework, which opens
        the debate's next round in the same pass. A pass counts however it ended (A4b)."""
        from auto_research.consensus.strategy import _tool_results as round_results

        if role == "librarian":
            self._passes[role] += 1
        elif role == "lead" and report is not None and getattr(report, "rework", False):
            self._debate_round += 1
            self._round_snapshot = round_results(self.workers)
        elif role == "lead":
            self._passes[role] += 1
            self._debate_round, self._round_snapshot = 0, None

    def _records(self, unit: str) -> dict[Path, tuple[int, int]]:
        """The unit's report records beside the ledger (production writes each role's report into
        ``reports/<unit>/``), with their modification time and size."""
        from auto_research.sandbox.workspace import ledger_dir

        folder = ledger_dir(self.run_id) / "reports" / unit
        return {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in folder.glob("*.jsonl")} if folder.is_dir() else {}

    def _changed_records(self, unit: str, before: dict[Path, tuple[int, int]]) -> list[Path]:
        """The unit's records written since ``before``, newest first."""
        now = self._records(unit)
        return sorted((p for p, stamp in now.items() if before.get(p) != stamp),
                      key=lambda p: now[p][0], reverse=True)

    def _written_record(self, unit: str, before: dict[Path, tuple[int, int]], report: Any) -> Path:
        """The record this turn wrote in the unit's own folder that holds ``report``."""
        found = [p for p in self._changed_records(unit, before) if _stored(p, type(report)) == report]
        if not found:
            raise AssertionError(f"no record {unit} wrote this turn holds the {type(report).__name__} "
                                 "it filed; the driver's reading of production is stale")
        return found[0]

    async def resume(self, role: str, script: Sequence[Any], *, text: str = "Please read this and answer.",
                     sender: str = "human", timeout: float = 120.0, seat: int = 0) -> TurnResult:
        """Deliver ``text`` to ``role``'s seat after its turn filed, as production delivers it, and
        return the resumed turn's observables, its model answering from ``script``.

        ``sender="human"``: the human's reply (``kind="reply"``), queued in the seat's inbox and the
        seat woken as the channel wakes it (``SeatRouter.wake``); a role name: that role's seat
        texts it through the channel (``SeatRouter.send``), which resumes it; the notice the sender
        then gets (the resumed turn ended) is taken out of its inbox, since no turn of the sender's is
        running to read it. The resumed turn runs in the channel's own task; this returns once the
        ledger records its end. ``report`` is the role's report as production holds it once the
        resumed turn ended: read back from the record its filing wrote (:meth:`report_file`), so a
        resumed turn that rewrote it shows the rewrite. ``error``: the run's failure when the
        resumed turn ended the run (ruling D6), or :class:`ResumedTurnFailed` naming the error the
        channel recorded (``channel.resume.error``).
        """
        from auto_research.hitl.router import HUMAN

        unit = self.unit(_check(role), seat)
        model = self._model(role, seat)
        model.load(script)
        sent_mark, line_mark = len(model.sent), len(self._ledger_events())
        before = self._records(unit)
        if sender == HUMAN:
            self.control.add_steer(unit, text, sender=HUMAN, kind="reply")
            self.router.wake(unit, [(HUMAN, "reply")])
        else:
            reply, is_error = await self.router.send(self.unit(_check(sender)), unit, text)
            if is_error:
                raise ScriptError(f"the {sender}'s text to the {role} was refused: {reply}")
        ends = ("channel.resume.end", "channel.resume.error", "channel.resume.exhausted")

        def ended() -> dict | None:
            return next((e for e in self._ledger_events()[line_mark:]
                         if e["event"] in ends and e.get("actor") == unit), None)

        error: BaseException | None = None
        try:
            async with asyncio.timeout(timeout):
                while (end := ended()) is None:
                    await asyncio.sleep(0.01)
                # The channel task finishes the step that recorded the end; a run failure (D6)
                # cancels this task, and the cancel lands here.
                for _ in range(3):
                    await asyncio.sleep(0)
            if end["event"] == "channel.resume.error":
                error = ResumedTurnFailed(end["data"].get("error", ""))
        except asyncio.CancelledError:
            # The resumed turn exhausted its caps and failed the run (ruling D6), as in `turn`.
            if self.control.fatal is None or self.control.stop_requested:
                raise
            asyncio.current_task().uncancel()
            error = self.control.fatal
        except TimeoutError as exc:
            raise ScriptError(f"the {role}'s resumed turn ran past {timeout} s") from exc
        if sender != HUMAN:
            self.control.drain_inbox(self.unit(sender))
        self._check_spent(role, model)
        events = self._ledger_events()[line_mark:]
        results = _tool_results(events, unit)
        report = None
        stored = self._report_files.get((role, seat))
        if stored is not None:
            # A record the resumed turn wrote (the filed one rewritten, or a new one) is what
            # production now holds; otherwise the filed record stands.
            written = [p for p in self._changed_records(unit, before) if _stored(p, stored[1]) is not None]
            if written:
                self._report_files[(role, seat)] = (written[0], stored[1])
            report = _stored(*self._report_files[(role, seat)])
        # The outcome is the resumed turn's own: it ended through an accepted turn_end, raising nothing.
        ended = report is not None and error is None and _accepted_terminal(results)
        return TurnResult(
            role=role, unit=unit, source=self.source(role, seat), report=report, error=error,
            run_failure=self.control.fatal, events=events, receipts=self.receipts,
            tool_results=results, sent=model.sent[sent_mark:], returned=None,
            outcome=_outcome(ended, error, events, unit, report),
        )

    def _load_aux(self, **scripts: Sequence[Any] | None) -> None:
        for name, script in scripts.items():
            if script is None:
                continue
            if not self.pipeline:
                raise ScriptError(f"a {name} script needs a run opened with pipeline=True")
            (self.coder if name == "coder" else self.aux[name]).load(script)

    def _check_spent(self, role: str, model: ScriptedProvider) -> None:
        """Every script the turn was given was spent exactly, and no faked actor's script failed."""
        actors = [*self.models.values(), *self.peer_models, *self.aux.values(),
                  *(a for a in (self.human, self.coder) if a)]
        failed = [error for actor in actors for error in actor.errors]
        for actor in actors:
            actor.errors.clear()
        if failed:
            raise ScriptError(f"a script failed during the {role}'s turn: {failed[0]}") from failed[0]
        if model.script:
            raise ScriptError(f"{len(model.script)} script item(s) left unused when the {role}'s turn "
                              f"ended: {model.script!r}")
        left = {name: m.script for name, m in self.aux.items() if m.script}
        if self.coder is not None and self.coder.script:
            left["coder"] = self.coder.script
        if left:
            raise ScriptError(f"script item(s) left unused when the {role}'s turn ended: {left!r}")

    def _entry(self, role: str, question: str, handed: Sequence[TurnResult], *, seat: int = 0,
               nudges: Sequence[str] = (), human_files: Sequence[str] = ()) -> Callable[[], Any]:
        """The role's entry point, bound to the arguments the graph and the debate pass; a route
        the graph has not is refused here, before the turn starts."""
        from auto_research.graph.builder import _build_task
        from auto_research.schemas import ResearchRequest

        unit, orch = self._seat(role, seat), self.orch
        request = ResearchRequest(question=question)
        allowed = {"reviewer": {"reviewer", "librarian", "lead"}, "librarian": {"reviewer"},
                   "worker": {"reviewer", "lead", "worker"}, "lead": {"reviewer", "worker"}}[role]
        stray = [t.role for t in handed if t.role not in allowed]
        if stray:
            raise ValueError(f"the graph routes no {stray} turn to the {role}")
        if (nudges or human_files) and role != "reviewer":
            raise ValueError(f"the graph hands the human's reply to the reviewer, not the {role}")
        verdicts = [t.report for t in handed if t.role == "reviewer" and t.report is not None]
        passes = int(orch.get("max_iterations", 3))

        if role == "reviewer":
            from auto_research.agents.reviewer_tools import ReviewerRender
            from auto_research.graph.builder import _literature_cited, _version_dir

            arrived, literature, literature_receipts, report = "request", [], {}, None
            failed_pass: TurnResult | None = None
            for earlier in handed:
                if earlier.report is None and earlier.role != "reviewer":
                    # A pass that filed nothing reaches the reviewer only when its unit's model call
                    # failed and the account is sound (A4b); every other end of it ends the run.
                    if not isinstance(earlier.error, ModelCallFailed) or earlier.error.account_wide:
                        raise ValueError(f"the graph routes this unfiled {earlier.role} pass to no one: it "
                                         f"ends the run ({earlier.error!r})")
                    failed_pass = earlier
                    continue
                if earlier.report is None:
                    raise ValueError(f"the {earlier.role}'s turn filed no report to hand on")
                if earlier.role == "reviewer":
                    continue  # its own earlier report: `previous`
                if earlier.role == "librarian":
                    arrived = "librarian"
                    literature.append(earlier.report)
                    literature_receipts.update(_literature_cited(earlier.report, self.seats["librarian"]))
                else:
                    arrived, report = "analysts", earlier.report
            if failed_pass is not None:
                if arrived != "request":
                    raise ValueError("a failed pass reaches the reviewer alone, as the graph's state carries it")
                team = "librarian" if failed_pass.role == "librarian" else "analysts"
                arrived = f"{team}_failed"
            if nudges or human_files:
                # The graph's human node runs after a report assigned to the human, with a human
                # attending; its reply alone brings the reviewer back.
                if self.human is None or not verdicts or verdicts[-1].assign_to != "human":
                    raise ValueError("the human replies only to a report assigned to the human, in a run a "
                                     "human attends (open_run(human=...), handed the reviewer's report)")
                if arrived != "request":
                    raise ValueError("the human's reply reaches the reviewer alone, as the graph's human node hands it")
                arrived = "human"
                self._nudges.extend(nudges)
                self._human_thread.extend(human_files)
            budgets = {"librarian": passes - self._passes["librarian"],
                       "analysts": passes - self._passes["lead"]}
            # The graph's version: one more than the reports already published to the human.
            version = 1 + sum(v.assign_to == "human" for v in verdicts)
            render = ReviewerRender(
                report_dir=_version_dir(self.run_id, version), run_id=self.run_id,
                formats=tuple(orch.get("report_formats") or ["jsonl", "tex"]), ledger=self.ledger,
            )

            async def review() -> Any:
                failure = {}
                if failed_pass is not None:
                    # The graph's own account of the failed pass (`_failed_pass`): its ledger event,
                    # and the notice the reviewer reads; the pass is already counted.
                    from auto_research.graph.builder import _failed_pass

                    team = "librarian" if failed_pass.role == "librarian" else "analysts"
                    counter = "lit_iterations" if team == "librarian" else "analysis_iterations"
                    state = await _failed_pass(self.monitor, failed_pass.error, team=team, counter=counter,
                                               passes=self._passes[failed_pass.role] - 1)
                    failure = {"failure": state["pass_failure"]}
                return await unit.review(
                    request, arrived=arrived, literature=literature, literature_receipts=literature_receipts,
                    report=report, previous=verdicts, human_nudges=list(self._nudges),
                    human_files=list(self._human_thread),
                    budgets=budgets, human_attended=self.human is not None, render=render, **failure,
                )

            return review
        if role == "librarian":
            follow_up = verdicts[-1].follow_up if verdicts else None
            return lambda: unit.research(request, follow_up=follow_up)
        task = _build_task({"request": request, "verdicts": verdicts, "report": None},
                           tool_catalogue=self.registry.tool_catalogue())
        if role == "worker":
            leads = [t for t in handed if t.role == "lead"]
            peers = [t for t in handed if t.role == "worker"]
            if not leads:
                if peers:
                    raise ValueError("a worker is handed its peers' reports only in a rework round: hand "
                                     "the lead's turn that asked for it")
                return lambda: unit.analyze(task)
            # A rework round, as the debate runs it (`_revision_round`): the lead's probe added to the
            # task, the peers' tool results of the last round, the peers' reports.
            from auto_research.consensus.strategy import DebateToConverge, _augment_with_probe

            if len(leads) > 1 or leads[0].report is None or not getattr(leads[0].report, "rework", False):
                raise ValueError("the debate sends the workers back only on one lead turn that filed rework")
            task = _augment_with_probe(task, leads[0].report)
            task = DebateToConverge(monitor=self.monitor, registry=self.registry)._with_peer_tools(
                self.workers, task, seat, self._round_snapshot)
            peer_reports = [t.returned for t in peers if t.unit != unit.agent_key and t.returned is not None]
            return lambda: unit.analyze(task, peer_reports=peer_reports)
        # The lead, as the debate calls it in the pass's current round: the reports of the workers
        # that filed, after the debate's notice naming each handed worker that filed nothing.
        from auto_research.consensus.strategy import DebateToConverge
        from auto_research.sandbox.workspace import run_dir
        from auto_research.toolkit_contract import toolkit

        ran = [t for t in handed if t.role == "worker"]
        seats = {w.agent_key: w for w in self.workers}
        filed = [(t.unit, t.returned) for t in ran if t.returned is not None]
        max_rounds = int(orch.get("debate_max_rounds", 3))

        async def cycle() -> Any:
            await DebateToConverge(max_rounds=max_rounds, monitor=self.monitor)._notify_lead(
                unit, [seats[t.unit] for t in ran], [t.returned for t in ran])
            return await unit.run_cycle(
                task, [r for _, r in filed], sources=[u for u, _ in filed],
                rounds_left=max(0, max_rounds - self._debate_round - 1),
                author_budget=int(orch.get("tool_author_cap", 3)), toolkit_root=self.toolkit_root,
                worktree_base=run_dir(self.run_id) / "authoring", direct_branch=toolkit().authoring_branch,
                colead=None, human_handler=self.human,
            )

        return cycle

    # ── helpers a clause test reads ──

    def evidence_steps(self, role: str) -> list[Any]:
        """Script items through which ``role``'s seat produces evidence of its own: the worker runs a
        toolkit tool on the run's product (a ``result`` receipt), the librarian opens the source's
        paper (a ``paper`` receipt). The reviewer and the lead produce none; they cite what is handed."""
        _check(role)
        if role == "worker":
            product = str(self.product)
            return [lambda sent: (tool_named(sent, "band_pa_summary"), {"csv_path": product})]
        if role == "librarian":
            return [("search_literature", {"source": "arxiv", "query": QUESTION}), ("read_paper", {"hit": 1})]
        return []

    def file_script(self, role: str, *, claim: str | Callable[[Sent], str] = CLAIM,
                    tool_request: str | None = None) -> list[Any]:
        """The script of a minimal valid filing for ``role``: what its report needs first (the
        librarian's finding needs a document it opened), the report, then ``turn_end``.

        ``claim`` may be a callable of what the write call was sent, for a claim citing a handle the
        turn's own tool results named (``handles_shown``). ``tool_request``: a tool the report asks
        for (:func:`report_content`)."""
        write, _, end = report_tools(role)
        opened = self.evidence_steps(role) if role == "librarian" else []
        source = self.source(role)

        def report(sent: Sent) -> tuple[str, dict]:
            text = claim(sent) if callable(claim) else claim
            support = handles_shown(sent, source, "p")[-1:] if role == "librarian" else ()
            return write, {"content": report_content(role, claim=text, support=support,
                                                     tool_request=tool_request)}

        return [*opened, report, (end, {"summary": "filed"})]

    def harness_calls(self, role: str) -> list[HarnessCall]:
        """``role``'s harness tools whose return the runtime mints as a navigation receipt, each with
        minimal valid arguments in this run (the product, its folder, a web address, a query, a
        shell command)."""
        arguments = {
            "read_file": {"path": str(self.product)}, "list_dir": {"path": str(self.product.parent)},
            "web_fetch": {"url": "https://example.org/a"}, "web_search": {"query": "band ratios"},
            "text_search": {"query": "band ratios"}, "shell": {"command": "ls"}, "list_env": {}, "use_env": {},
        }
        return [HarnessCall(kind=kind, tool=tool, arguments=dict(arguments[kind]), letter=letter,
                            producer=producer)
                for kind, tool, letter, producer in _HARNESS[_check(role)]]

    def harness_call(self, role: str, kind: str) -> HarnessCall:
        """``role``'s harness tool of ``kind`` (``shell``, ``read_file``, ...); ``LookupError`` when
        the role has none."""
        found = [c for c in self.harness_calls(role) if c.kind == kind]
        if not found:
            raise LookupError(f"the {role} has no {kind} tool")
        return found[0]

    async def peer_result(self, *, seat: int = 1) -> TurnResult:
        """One real turn of worker seat ``seat`` (``workers=2`` or more): it runs a toolkit tool on
        the run's product and files a report citing that result. Its ``handles("r")`` are a peer's
        real toolkit results, minted by the runtime, for a clause on what another unit produced."""
        if not 0 < seat < len(self.workers):
            raise ScriptError(f"peer_result needs worker seat {seat}: open the run with workers={seat + 1}")
        source = self.source("worker", seat)
        claim = lambda sent: f"The band ratio is {{{{{handles_shown(sent, source, 'r')[0]}#/ratio}}}}."  # noqa: E731
        return await self.turn("worker", [*self.evidence_steps("worker"), *self.file_script("worker", claim=claim)],
                               seat=seat)

    def human_files(self, files: dict[str, bytes | str]) -> list[str]:
        """The handles of files the human sends with a reply to the reviewer's report, minted as the
        graph's human node mints them (``mint_reply_attachments``, source ``input``); pass them to the
        reviewer's next turn as ``human_files``."""
        import mimetypes

        from auto_research.hitl.attachments import mint_reply_attachments
        from auto_research.provenance.store import INPUT_SOURCE
        from auto_research.sandbox.workspace import ledger_dir
        from auto_research.schemas import HumanAttachment, HumanResponse

        folder = ledger_dir(self.run_id) / "human_replies" / f"freeze-{uuid.uuid4().hex[:6]}"
        folder.mkdir(parents=True, exist_ok=True)
        attachments = []
        for name, body in files.items():
            path = folder / name
            if isinstance(body, str):
                path.write_text(body, encoding="utf-8")
            else:
                path.write_bytes(body)
            attachments.append(HumanAttachment(path=path, filename=name,
                                               mime_type=mimetypes.guess_type(name)[0] or "application/octet-stream"))
        response = HumanResponse(query_id=f"q-{uuid.uuid4().hex[:8]}", answer="", attachments=attachments)
        return [r.handle for r in mint_reply_attachments(self.receipts, source=INPUT_SOURCE,
                                                         response=response, parent=None)]

    # ── what the run's actors were sent ──

    @property
    def root(self) -> Path:
        """The run's own folder: its runtime dir (``runtime/``), toolkit (``toolkit/``) and
        configuration (``config/``). Everything the run may write lies below it."""
        return self.tmp_path

    def workdir(self, role: str, seat: int = 0) -> Path:
        """``role``'s seat's own working folder in the run (``sandbox.workspace.agent_dir``): the one
        folder its jailed processes may write."""
        from auto_research.sandbox.workspace import agent_dir

        return agent_dir(self.run_id, self.unit(role, seat))

    def colead_calls(self) -> list[Sent]:
        """What each model call of the co-lead (the ``openai`` member) was sent, in order."""
        return list(self.aux["colead"].sent) if self.pipeline else []

    def coder_briefs(self) -> list[str]:
        """Every message the author process was sent, in order."""
        return list(self.coder.briefs) if self.coder is not None else []

    def inbox_of(self, role: str, seat: int = 0) -> list[Text]:
        """The texts waiting for ``role``'s seat (unread), or, for ``"human"``, everything the human
        was sent: each query (``kind="query"``) and each notice (``kind="notice"``), in order."""
        from auto_research.hitl.router import HUMAN

        if role == HUMAN:
            if self.human is None:
                return []
            return [Text(sender=item.asked_by, text=item.message, kind=kind) if kind == "query"
                    else Text(sender="", text=item, kind=kind) for kind, item in self.human.received]
        return [Text(sender=e.sender, text=e.text, handles=tuple(e.handles), kind=e.kind)
                for e in self.control.peek_inbox(self.unit(role, seat))]

    async def repl_writable(self, path: Path | str, *, seat: int = 0) -> bool:
        """Whether worker seat ``seat``'s REPL (the jailed process its toolkit calls run in) may
        write ``path``: the kernel's answer to ``os.access(path, W_OK)``, which honours read-only
        mounts and reads an unmounted path as not writable, asked through production's REPL call
        (``PersistentSandbox.call_tool``) with a standard-library function, so nothing is written.
        Like any call it takes the REPL's pending environment notice; probe before a turn, not
        between a turn's calls."""
        out = await self.sandboxes[seat].call_tool("os", "access", {"path": str(path), "mode": os.W_OK})
        if out.get("exit_code") != 0 or out.get("stdout") not in ("True", "False"):
            raise ScriptError(f"the REPL's write probe of {path} did not answer: {out!r}")
        return out["stdout"] == "True"

    async def consume(self, turn: TurnResult, script: Sequence[Any] | None = None) -> Handoff:
        """Hand ``turn``'s outcome to the unit the graph or the debate hands it to, and run that
        unit's turn on ``script`` (default: its minimal filing, :meth:`file_script`).

        A worker's turn goes to the lead (a worker whose model call failed reaches it as no report;
        any other raise ends the run). A librarian's or the lead's filed report goes to the
        reviewer, and a lead's filed rework to the worker; a pass whose model call failed reaches the
        reviewer as the graph's failure notice (A4b), recorded (``<team>.failed``) in the reviewer's
        turn, where the driver runs the graph's step; any other unfiled end of it ends the run. A
        reviewer's filed report goes to the team it assigns (the human is no unit); an unfiled one
        ends the run. ``Handoff.turn`` is ``None`` when the run ends instead. A lead turn that
        briefed a chain or committed a tool is refused (``ValueError``): the debate's rerun after
        authoring is not driven. The rework goes to seat 0; another seat's turn is
        ``turn("worker", ..., handed=[lead, *peers], seat=n)``."""
        consumer = self._consumer(turn)
        if consumer in (None, "human"):
            return Handoff(consumer=consumer, turn=None, ended=turn.error if consumer is None else None)
        result = await self.turn(consumer, self.file_script(consumer) if script is None else script,
                                 handed=[turn])
        return Handoff(consumer=consumer, turn=result, ended=None)

    def _consumer(self, turn: TurnResult) -> str | None:
        error, report = turn.error, turn.report
        contained = isinstance(error, ModelCallFailed) and not error.account_wide
        if turn.role == "worker":
            return "lead" if error is None or isinstance(error, ModelCallFailed) else None
        if turn.role == "reviewer":
            return {"librarian": "librarian", "analysts": "worker", "human": "human"}[report.assign_to] if report else None
        if turn.role == "lead" and turn.returned is not None and (
                turn.returned.authored_chain or turn.returned.committed >= 1):
            # The debate then reloads the toolkit and reruns every worker on it before the lead
            # judges again (`_resume_after_authoring`); the driver does not drive that path.
            raise ValueError("a lead turn that briefed a chain or committed a tool goes to the workers' "
                             "rerun after authoring, which consume does not drive")
        if report is not None:
            return "worker" if getattr(report, "rework", False) else "reviewer"
        return "reviewer" if contained else None

    # ── the ledger ──

    def ledger_events(self) -> list[dict]:
        """Every line of the run's ledger (``monitor.jsonl``) so far, parsed, in order."""
        if self.ledger is None or not self.ledger.is_file():
            return []
        return [json.loads(line) for line in self.ledger.read_text(encoding="utf-8").splitlines() if line]

    _ledger_events = ledger_events


def open_run(tmp_path: Path, **opts: Any) -> Run:
    """A :class:`Run` to open with ``async with``; ``opts`` as :class:`Run` takes them."""
    return Run(tmp_path, **opts)


async def run_turn(role: str, script: Sequence[Any] | Callable[[Run], Sequence[Any]], tmp_path: Path,
                   **opts: Any) -> TurnResult:
    """One turn of ``role`` in a fresh run. ``script`` may be a callable taking the open run, for a
    script built from it (``run.file_script``, ``run.evidence_steps``)."""
    async with open_run(tmp_path, **opts) as run:
        return await run.turn(role, script(run) if callable(script) else script)


# ── role-neutral helpers ───────────────────────────────────────────────────────────────────


def _check(role: str) -> str:
    if role not in ROLES:
        raise ValueError(f"no role {role!r}; the roles are {ROLES}")
    return role


def report_tools(role: str) -> tuple[str, str, str]:
    """``role``'s report tools: (write the whole report, edit it, end the turn)."""
    from auto_research.agents.react import EDIT_REPORT_TOOL, TURN_END_TOOL, WRITE_REPORT_TOOL

    _check(role)
    return WRITE_REPORT_TOOL, EDIT_REPORT_TOOL, TURN_END_TOOL


def report_content(role: str, *, claim: str = CLAIM, support: Sequence[str] = (),
                   tool_request: str | None = None, assign_to: str = "analysts",
                   what_to_probe: str | None = None) -> str:
    """The ``write_report`` text of a complete report for ``role`` whose one claim is ``claim``.

    The reviewer assigns the analysts with ``claim`` as its rationale; the others declare success
    with ``claim`` as summary and one finding; the librarian's finding names ``support``.
    ``tool_request`` (a seat that may request a tool): the report states a failed outcome and asks
    for that tool, naming :data:`VALIDATION_RECORD`. ``assign_to="human"`` (the reviewer): a
    report to the human, its title, abstract, one section and one key finding all ``claim``.
    ``what_to_probe`` (the lead): the report sends the workers back for another round on that probe,
    with ``claim`` as its rationale."""
    if what_to_probe is not None:
        if role != "lead":
            raise ValueError("only the lead's report asks for another round")
        return (f"# summary\n{claim}\n# rework\ntrue\n# rationale\n{claim}\n"
                f"# what_to_probe\n{what_to_probe}\n")
    if _check(role) == "reviewer":
        if assign_to == "human":
            return (f"# assign_to\nhuman\n# accepted\ntrue\n# rationale\n{claim}\n# title\nBand ratios\n"
                    f"# abstract\n{claim}\n# sections\n## Results\n{claim}\n# key_findings\n- {claim}\n")
        return f"# assign_to\n{assign_to}\n# rationale\n{claim}\n# follow_up\nTabulate the band ratios.\n"
    status = "failed" if tool_request else "succeeded"
    text = f"# summary\n{claim}\n# status\n{status}\n# findings\n- {claim}\n"
    if role == "librarian":
        text += "  support: " + ", ".join(support) + "\n"
    if tool_request:
        text += (
            "# tool_request\nscience_context: Total counts per band are the denominator of every band "
            "ratio, and no tool sums them.\nexpected_behavior: Sums each band over the rows of a product "
            f"CSV and writes them as a table.\nproposed_tool_name: {tool_request}\n"
            f"validation_data: {VALIDATION_RECORD}\n"
        )
    return text


def brief_step(tool: str = NEW_TOOL, *, package: str = "demo_utils") -> tuple[str, dict]:
    """The call that briefs the coding pipeline on one tool: one link, extending ``package``."""
    store, bucket, key, version = (part.strip() for part in VALIDATION_RECORD.split("|"))
    link = {
        "science_context": "Total counts per band are the denominator of every band ratio.",
        "expected_behavior": (f"{tool}(csv_path, out_dir) sums each band over the rows of a product CSV "
                              "and writes the totals as a declared table."),
        "proposed_tool_name": tool, "target_domain": package, "disposition": "extend_existing",
        "validation_data": [{"store": store, "bucket": bucket, "key": key, "version_id": version}],
    }
    return "author_chain", {"first_link": link, "rationale": "The analysts need the band totals."}


def submit_step() -> tuple[str, dict]:
    """The call that submits the open coding link to the gate and the co-lead's review."""
    return "submit_for_review", {}


def review_script(approved: bool, *, issue: str = "The test leaves an empty product untested.") -> list[Any]:
    """The co-lead's model calls for one review: it reads, then gives its verdict."""
    from auto_research.schemas import ToolReviewVerdict

    verdict = ToolReviewVerdict(
        approved=approved, rationale="The change does what the brief asks." if approved else issue,
        issues=[] if approved else [issue], commit_summary=f"Add {NEW_TOOL}" if approved else "",
    )
    return ["I read the diff and the gate report.", ModelResponse(parsed=verdict.model_dump(mode="json"))]


def coder_edit(*, passing: bool = True) -> Callable[[str, Path], dict[str, str | None]]:
    """A coder script item adding :data:`NEW_TOOL` to ``demo_utils``: its code, manifest entry and
    test. ``passing=False``: the code sums the wrong column, so the change's own test fails."""

    def edit(message: str, worktree: Path) -> dict[str, str | None]:
        from tests.fixtures import toolkit as fixture

        manifest = worktree / "demo_utils" / "skills" / "__init__.py"
        text = manifest.read_text(encoding="utf-8").rstrip()
        if not text.endswith("}"):
            raise ScriptError("the demo_utils manifest does not end with its dict literal")
        return {
            "demo_utils/lib/demo_totals.py": _NEW_TOOL_LIB % ("high_counts" if passing else "low_counts"),
            "demo_utils/skills/__init__.py": text[:-1] + _NEW_TOOL_ENTRY % fixture.TOOLKIT_NAME + "}\n",
            "demo_utils/tests/test_demo_totals.py": _NEW_TOOL_TEST,
        }

    return edit


#: ``shapes=True``: a tool that copies its product CSV's rows into a declared table (bytes of its own:
#: a header line is added), then returns a failure
#: (``exit_code`` 1): its result and its product are minted flagged ``failed``, and a later call that
#: reads that table descends from it. Arguments: ``csv_path`` (a path), ``out_dir`` (the runtime's).
FAILING_PRODUCT_TOOL = "copy_rows_then_fail"
#: ``shapes=True``: a tool that draws the ratio plot of ``ratios`` (data) and declares two figures:
#: the file it wrote, with a hash of other bytes (:data:`MISDECLARED_CAPTION`), and a file it never
#: wrote (:data:`ABSENT_CAPTION`).
MISDECLARED_FIGURE_TOOL = "plot_misdeclared"
MISDECLARED_CAPTION = "Band ratio per row, declared with a hash of other bytes"
ABSENT_CAPTION = "A figure declared and never written"

_SHAPES_SOURCE = '''\
"""Two tools whose returns the runtime corrects: a product of a call that failed, and figures
declared with a wrong hash or with no file."""

import hashlib
from pathlib import Path

from demo_utils.lib.demo_products import plot_band_ratio


def copy_rows_then_fail(csv_path, out_dir):
    """Copy the product's rows to ``<out_dir>/rows.csv`` under a header line of its own (so its bytes
    are its own), declare it, then report a failed check."""
    table = Path(out_dir) / "rows.csv"
    table.write_text("# copied_by=copy_rows_then_fail\\n" + Path(csv_path).read_text(encoding="utf-8"),
                     encoding="utf-8")
    return {"exit_code": 1, "stdout": "", "stderr": "The rows were copied; the check after the copy failed.",
            "products": [{"path": str(table), "caption": "The product's rows, copied before the check failed"}]}


def plot_misdeclared(ratios, save_path="misdeclared.png"):
    """Draw the ratio plot; declare it with a hash of other bytes, beside a figure never written."""
    out = plot_band_ratio(ratios, save_path)
    (figure,) = out["figures"]
    other = hashlib.sha256(b"bytes other than the figure's").hexdigest()
    return {"stdout": out["stdout"], "figures": [
        dict(figure, sha256=other, caption=%(misdeclared)r),
        dict(figure, path=str(Path(save_path).with_name("never_written.png")), caption=%(absent)r),
    ]}
''' % {"misdeclared": MISDECLARED_CAPTION, "absent": ABSENT_CAPTION}


def _add_shape_tools(root: Path) -> None:
    """Add :data:`FAILING_PRODUCT_TOOL` and :data:`MISDECLARED_FIGURE_TOOL` to the run's own copy of
    the fixture toolkit's ``demo_utils``, committed; the manifest is read and written as data."""
    import pprint

    from auto_research.sandbox.registry import manifest_literal
    from tests.fixtures import toolkit as fixture

    package = root / fixture.PKG_NAME
    manifest = package / "skills" / "__init__.py"
    entries = manifest_literal(manifest.read_text(encoding="utf-8")) or {}
    key = f"{fixture.TOOLKIT_NAME}.{fixture.PKG_NAME}.%s"
    module = f"{fixture.PKG_NAME}.lib.demo_shapes"
    entries[key % FAILING_PRODUCT_TOOL] = {
        "module": module, "fn": FAILING_PRODUCT_TOOL,
        "description": "Copies the rows of a reduced product CSV into a table, then reports a failed check.",
        "args_schema": {"type": "object", "properties": {
            "csv_path": {"kind": "path", "type": "string", "description": "Path of the reduced product CSV."},
            "out_dir": {"kind": "output", "shape": "folder", "type": "string",
                        "description": "Folder the copy is written to."},
        }, "required": ["csv_path", "out_dir"]},
    }
    entries[key % MISDECLARED_FIGURE_TOOL] = {
        "module": module, "fn": MISDECLARED_FIGURE_TOOL,
        "description": "Bar plot of per-row band ratios, declared with a wrong hash beside a figure never written.",
        "args_schema": {"type": "object", "properties": {
            "ratios": {"kind": "data", "type": "array", "items": {"type": "number"},
                       "description": "The ratios write_band_table returned."},
            "save_path": {"kind": "output", "shape": "file", "type": "string", "default": "misdeclared.png",
                          "description": "File the PNG is written to."},
        }, "required": ["ratios"]},
    }
    manifest.write_text('"""The AI-half: a pure-data tool manifest (no domain imports)."""\n\n'
                        "TOOL_MANIFEST: dict = " + pprint.pformat(entries, sort_dicts=False) + "\n",
                        encoding="utf-8")
    (package / "lib" / "demo_shapes.py").write_text(_SHAPES_SOURCE, encoding="utf-8")
    fixture.git(root, "add", "-A")
    fixture.git(root, "commit", "-qm", "the toolkit with the runtime-corrected shapes")


def _drop_store_tools(root: Path) -> None:
    """Take the fixture toolkit's store tools out (``other_utils``: ``count_lines``, ``copy_record``),
    committed, so its tree has no store tool. The manifest is read and written as data."""
    import pprint

    from auto_research.sandbox.registry import manifest_literal
    from tests.fixtures import toolkit as fixture

    package = root / fixture.OTHER_PKG
    manifest = package / "skills" / "__init__.py"
    entries = manifest_literal(manifest.read_text(encoding="utf-8")) or {}
    kept = {name: spec for name, spec in entries.items() if not (
        spec.get("writes") or spec.get("reads")
        or any(arg.get("kind") == "record" for arg in spec["args_schema"]["properties"].values()))}
    if len(kept) == len(entries):
        raise ScriptError("the fixture toolkit's other_utils has no store tool to take out")
    manifest.write_text('"""The AI-half: a pure-data tool manifest (no domain imports)."""\n\n'
                        "TOOL_MANIFEST: dict = " + pprint.pformat(kept, sort_dicts=False) + "\n",
                        encoding="utf-8")
    (package / "lib" / "other_store.py").unlink()
    (package / "tests" / "test_store_tools.py").unlink()
    fixture.git(root, "add", "-A")
    fixture.git(root, "commit", "-qm", "the toolkit without store tools")


async def _close_sessions(control: Any) -> None:
    """Close the coding session still published and every one whose close raised, as
    ``run_research``'s teardown does."""
    leftover = [control.authoring_session] if control.authoring_session else []
    leftover += control.unclosed_sessions
    control.authoring_session, control.unclosed_sessions = None, []
    for session in leftover:
        await asyncio.wait_for(session.aclose(), timeout=30)


def _tool_results(events: list[dict], unit: str) -> list[ToolResultSeen]:
    return [
        ToolResultSeen(name=e["data"].get("tool", ""), text=e["data"].get("text", ""),
                       is_error=bool(e["data"].get("is_error")), call_id=e["data"].get("tool_call_id", ""))
        for e in events if e.get("actor") == unit and e["event"].endswith(".tool_result")
    ]


def tool_named(sent: Sent, suffix: str) -> str:
    """The name of the tool declared on ``sent`` that ends with ``suffix`` (a toolkit tool's name
    carries its toolkit and package)."""
    names = [n for n in sent.tools if n.endswith(suffix)]
    if len(names) != 1:
        raise LookupError(f"{len(names)} declared tools end with {suffix!r}: {sent.tools}")
    return names[0]


def handles_in(text: str, source: str, letter: str) -> list[str]:
    """The whole receipt handles ``<source>:<letter><n>`` in ``text``, first seen first."""
    pattern = rf"(?<![\w.-]){re.escape(source)}:{letter}\d+(?!\d)"
    return list(dict.fromkeys(re.findall(pattern, text)))


def handles_shown(sent: Sent, source: str, letter: str) -> list[str]:
    """The receipt handles ``<source>:<letter><n>`` the tool results on ``sent`` name, first seen first."""
    return handles_in(sent.results_text(), source, letter)


def _handed(role: str, returned: Any) -> Any | None:
    """What the graph or the debate routes on from a turn: the entry point's report (the lead's
    outcome carries it); the worker's is its report whether or not it filed one."""
    if returned is None:
        return None
    return returned.report if role == "lead" else returned


def is_evidence(receipt: Any, unit: str | None = None) -> bool:
    """Production's evidence predicate (``provenance.store.is_evidence``) for a number ``unit`` cites.
    Today's predicate is run-wide and ``unit`` is unused; step 2 binds it to the toolkits registered
    for that unit (§9.3.3), and only this function changes."""
    from auto_research.provenance.store import is_evidence as production

    return production(receipt)


#: What :func:`writability_probe` prints: a line holding exactly one of these.
WRITABLE, NOT_WRITABLE = "FREEZE_WRITABLE", "FREEZE_NOT_WRITABLE"


def writability_probe(path: Path | str) -> str:
    """A shell command that writes nothing and prints whether its shell may write ``path`` (the
    kernel's ``test -w``: read-only mounts and unmounted paths read as not writable). For the
    worker's shell: ``run.harness_call("worker", "shell").step(command=writability_probe(p))``.
    The markers are split by quotes in the command, so only its output carries them whole."""
    import shlex

    head, tail = WRITABLE.split("_", 1), NOT_WRITABLE.split("_", 1)
    return (f'if [ -w {shlex.quote(str(path))} ]; then echo {head[0]}_"{head[1]}"; '
            f'else echo {tail[0]}_"{tail[1]}"; fi')


def probed_writable(text: str) -> bool:
    """What a :func:`writability_probe`'s output says; ``AssertionError`` when it holds neither
    marker (the probe did not run), so a shell that never answered is never read as read-only."""
    lines = {line.strip() for line in text.splitlines()}
    if (WRITABLE in lines) == (NOT_WRITABLE in lines):
        raise AssertionError(f"the writability probe's output holds no single answer: {text!r}")
    return WRITABLE in lines


#: The repository this suite lives in.
REPO = Path(__file__).resolve().parents[2]
#: Folders a snapshot passes over: version control, the test runner's cache, and the editors' and
#: agents' own session folders.
_SNAPSHOT_SKIP = frozenset({".git", ".pytest_cache", ".claude", ".codex", ".agents"})


def tree_snapshot(*roots: Path | str) -> dict[str, tuple[int, ...]]:
    """Every entry below ``roots`` (default: :data:`REPO` and this interpreter's ``sys.prefix``, the
    run's analysis environment), links not followed: each file and link with its inode, size,
    modification and change times; each folder by its presence. Passed over: bytecode files and
    their ``__pycache__`` folders' presence (the test process writes them), :data:`_SNAPSHOT_SKIP`
    except the repository's ``.git/config`` and ``.git/hooks/``, and the repository's ``logs/`` and
    ``tmp_*`` working files (written by the developers' own tools). Compare with :func:`tree_changes`."""
    found: dict[str, tuple[int, ...]] = {}

    def stamp(path: str, folder: bool = False) -> None:
        try:
            st = os.lstat(path)
        except OSError:
            return  # removed while the walk ran
        found[path] = () if folder and not os.path.islink(path) else (
            st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)

    walks = [Path(r) for r in roots] or [REPO, Path(sys.prefix)]
    if not roots:
        walks.append(REPO / ".git" / "hooks")
        stamp(str(REPO / ".git" / "config"))
    for root in walks:
        for folder, dirs, files in os.walk(root):
            top = Path(folder) == REPO
            dirs[:] = [d for d in dirs if d == "__pycache__" or d not in _SNAPSHOT_SKIP
                       and not (top and (d == "logs" or d.startswith("tmp_")))]
            for name in dirs:
                if name != "__pycache__":
                    stamp(os.path.join(folder, name), folder=True)
            for name in files:
                if name.endswith(".pyc") or (top and name.startswith("tmp_")):
                    continue
                stamp(os.path.join(folder, name))
    return found


def tree_changes(before: dict[str, tuple[int, ...]], after: dict[str, tuple[int, ...]]) -> list[str]:
    """The files added (``+``), removed (``-``) or changed (``~``) between two :func:`tree_snapshot`s."""
    return sorted([*(f"+ {p}" for p in after.keys() - before.keys()),
                   *(f"- {p}" for p in before.keys() - after.keys()),
                   *(f"~ {p}" for p in before.keys() & after.keys() if before[p] != after[p])])


def _stored(path: Path, report_type: type) -> Any | None:
    """The report a report record holds (its last line), or ``None`` when it holds none."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        return report_type.model_validate_json(lines[-1]) if lines else None
    except (OSError, ValueError):
        return None


# ── the coding chain, end to end ───────────────────────────────────────────────────────────


@dataclass
class Chain:
    """One link of the lead's coding pipeline driven to its end, and what the toolkit's repository
    holds after the run closed. Shared between tests (``approved_chain``): read it, never change it."""

    run: Run
    """The closed run: its receipts (``run.receipts``), its faked actors' records (``run.coder``), its toolkit (``run.toolkit_root``)."""
    worker: TurnResult
    """The worker's turn that asked for :data:`NEW_TOOL`."""
    lead: TurnResult
    """The lead's turn: the brief, the submissions, its filing."""
    head: str
    """The authoring branch's tip before the lead's turn."""
    log: list[str]
    """``<sha> <subject>`` of each commit on the checked-out branch since ``head``."""
    status: str
    """``git status --porcelain`` of the toolkit after the run."""
    branch: str
    """The toolkit's checked-out branch after the run."""
    tips: dict[str, str]
    """Every branch of the toolkit's repository and its tip after the run (:data:`TRUSTED_BRANCH` too)."""
    providers: tuple[str, str | None]
    """The coder's vendor, and the vendor of the co-lead member whose provider network answered a
    review (``None`` when no review ran)."""
    colead_members: tuple[str, ...]
    """The vendors of the co-lead group's members, in order (``pipeline_colead``)."""

    @property
    def root(self) -> Path:
        return self.run.toolkit_root


async def run_chain(tmp_path: Path, *, passing: bool = True, approved: bool | None = True,
                    colead: Sequence[Any] | None = None, submits: int | None = None,
                    colead_members: Sequence[str] = ("openai",)) -> Chain:
    """Run one coding link of the lead's pipeline (``pipeline=True``, submission cap one): a worker
    asks for :data:`NEW_TOOL`; the lead briefs it, submits, and files; the coder writes it
    (``passing=False``: its own test fails); the co-lead reviews what the gate passed (``approved``,
    or the script ``colead``). ``submits``: how many submissions the lead's script makes (default:
    one when the link commits, two otherwise, the second closing the link at the cap).
    ``colead_members``: the co-lead group's vendors (``Run``'s ``pipeline_colead``)."""
    from tests.fixtures.toolkit import git

    commits = passing and bool(approved)
    async with open_run(tmp_path, pipeline=True, config={"tool_author_submit_cap": 1},
                        pipeline_colead=colead_members) as run:
        worker = await run.turn("worker", run.file_script("worker", tool_request=NEW_TOOL))
        head = git(run.toolkit_root, "rev-parse", "HEAD").strip()
        count = submits if submits is not None else (1 if commits else 2)
        lead = await run.turn(
            "lead", [brief_step(), *(submit_step() for _ in range(count)), *run.file_script("lead")],
            handed=[worker], coder=[coder_edit(passing=passing)],
            colead=colead if colead is not None else (review_script(bool(approved)) if passing else []),
            compactor=["The coder added the tool and its test."],
        )
        from auto_research.models import resolve

        # The co-lead's script serves only the openai member; any other member's call fails the turn.
        providers = (resolve("coder").handles[0].config.provider, "openai" if run.colead_calls() else None)
    root = run.toolkit_root
    return Chain(
        run=run, worker=worker, lead=lead, head=head,
        log=git(root, "log", "--format=%H %s", f"{head}..HEAD").strip().splitlines(),
        status=git(root, "status", "--porcelain").strip(),
        branch=git(root, "rev-parse", "--abbrev-ref", "HEAD").strip(),
        tips=run.branch_tips(), providers=providers, colead_members=run.pipeline_colead,
    )
