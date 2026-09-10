"""Async role wrappers for the Phase 1 agent team."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from auto_research.config_paths import orchestration_yaml
from auto_research.agents.prompts import PromptRole, load_prompt
from auto_research.agents.react import (
    TURN_END_TOOL,
    ADJUDICATE_STAGE,
    AUTHOR_CHAIN_TOOL,
    AUTHOR_STAGE,
    DECIDE_STAGE,
    DRIVE_CODER_TOOL,
    EDIT_REPORT_TOOL,
    HOLD_AUTHORING_TOOL,
    LEAD_CYCLE_TERMINALS,
    LEAD_FORM_TOOLS,
    LeadCycle,
    LeadCycleOutcome,
    MonitorToolProvider,
    PLAN_AND_DEBATE_TOOL,
    REVIEW_REPORT_TOOL,
    RESPOND_TO_REVIEW_TOOL,
    ReadOnlyToolProvider,
    ReportWorkspace,
    SUBMIT_TOOL,
    SandboxToolProvider,
    SourceToolProvider,
    _written_report,
)
from auto_research.consensus import IODebateToConverge
from auto_research.env import ledger_caps
from auto_research.monitor import RunMonitor
from auto_research.models import (
    ModelGroupHandle,
    ModelHandle,
    ModelMessage,
    ModelResponse,
    ToolResultMessage,
    ToolResultPart,
    resolve,
)
from auto_research.sandbox.workspace import append_memory, read_memory, round_report_path
from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    AuthoringDecision,
    DocumentRef,
    FigureRef,
    GateReport,
    MergeFlowResult,
    ResearchRequest,
    ReviewVerdict,
    SearchRequest,
    SearchResult,
    ToolResult,
    ToolReviewVerdict,
    ToolSpec,
    document_from_file,
)
from auto_research.sources import ArxivSource, NasaAdsSource, QuerySource, WebSource
from auto_research.sources.downloads import download_and_ingest_result, papers_dir
from auto_research.storage import MemoryRecord, Storage


class CallableModel(Protocol):
    """Minimal model interface used by role wrappers and tests."""

    async def call(
        self,
        messages: Sequence[Any],
        *,
        schema: type[BaseModel] | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str = "auto",
        images: Sequence[FigureRef] = (),
        documents: Sequence[DocumentRef] = (),
        cache_prefix: bool = False,
    ) -> ModelResponse:
        """Call the model; return structured output and/or tool calls."""


class AgentRole:
    """Base class shared by thin role wrappers."""

    role: PromptRole

    def __init__(
        self,
        *,
        model: CallableModel | None = None,
        model_group: ModelGroupHandle | None = None,
        model_name: str | None = None,
        prompt: str | None = None,
        monitor: RunMonitor | None = None,
        tools: Any | None = None,
    ):
        self.prompt = prompt or load_prompt(self.role)
        self.model = model or _select_model(model_group or resolve(self.role), model_name)
        self.last_response: ModelResponse | None = None
        self.monitor = monitor
        # Optional ToolProvider for an inner ReAct loop (reviewer/lead read-only,
        # librarian sources). When None, the role makes plain structured calls.
        self.tools = tools
        # Run-wide control (HITL /steer, /stop) + this role's steer key, wired by run_research.
        # Both default off so a role built without them behaves exactly as before.
        self.run_control: Any | None = None
        self.agent_key: str | None = None

    async def _call(
        self,
        payload: Mapping[str, Any],
        output_model: type[BaseModel],
        *,
        images=(),
        documents=(),
        action: str = "call",
        overrides: dict | None = None,
    ) -> Any:
        """Call the model for a structured message and validate it strictly.

        Uses provider-native structured output (``schema=output_model``): the
        provider returns a parsed object in ``ModelResponse.parsed``, which is
        validated against ``output_model``. There is **no salvage parsing** — a
        missing or off-schema result raises loudly. ``overrides`` injects
        runtime-owned fields (e.g. ``run_id``) after the model result and before
        validation.
        """
        await self._record(
            f"{self.role}.{action}.input",
            level=10,
            summary=f"{self.role} received {action} input",
            data={
                "payload": payload,
                "image_count": len(images),
                "document_count": len(documents),
                "documents": [doc.model_dump(mode="json") for doc in documents],
            },
        )

        def make_messages(feedback: str | None) -> list:
            call_payload: Mapping[str, Any] = payload if feedback is None else {**payload, "_retry_feedback": feedback}
            return [
                ModelMessage(role="system", content=self.prompt),
                ModelMessage(role="user", content=_payload_text(call_payload)),
            ]

        return await self._structured_loop(
            make_messages, output_model, action=action, images=images,
            documents=documents, overrides=overrides,
        )

    async def _call_over_messages(
        self,
        base_messages: Sequence[Any],
        output_model: type[BaseModel],
        *,
        action: str,
        images=(),
        documents=(),
        overrides: dict | None = None,
        decide_instruction: str | None = None,
        tools: Any | None = None,
    ) -> Any:
        """Produce a terminal structured ``output_model`` over a ReAct trajectory.

        ``base_messages`` is the conversation gathered by :meth:`_react` (system +
        interleaved ``ModelResponse``/``ToolResultMessage`` turns). A final user
        turn instructs the model to emit the structured result grounded in the
        tool outputs above. Same fail-loud retry as :meth:`_call`; ``cache_prefix``
        is on because the trajectory prefix is large and reused across retries.

        ``tools`` is the SAME provider :meth:`_react` just used, and passing it is not
        optional bookkeeping: the trajectory being replayed contains items that reference
        those tools, and a request that omits the declarations orphans them. That shape
        produced two run-killers — *"Tool reference 'x' not found in available tools"*
        (Anthropic, 2026-07-29) and *"function_call … without its required 'reasoning'
        item"* (OpenAI, 2026-08-05, run 8c75990e). ``tool_choice="none"`` keeps the
        declarations while forbidding a call, so the model must answer — the alternative,
        ``"auto"``, lets it call again and return ``parsed=None``, burning a retry.
        """
        instruction = decide_instruction or (
            f"You have finished using tools. Now produce the final {output_model.__name__} "
            "as your response, grounded only in the tool results above. Report what the "
            "tools actually returned — do not describe intentions or plans."
        )
        await self._record(
            f"{self.role}.{action}.input",
            level=10,
            summary=f"{self.role} composing {action} over {len(base_messages)} message(s)",
            data={"message_count": len(base_messages)},
        )

        def make_messages(feedback: str | None) -> list:
            text = instruction if feedback is None else f"{instruction}\n\n{feedback}"
            return list(base_messages) + [ModelMessage(role="user", content=text)]

        return await self._structured_loop(
            make_messages, output_model, action=action, images=images,
            documents=documents, overrides=overrides, cache_prefix=True, tools=tools,
        )

    async def _structured_loop(
        self,
        make_messages,
        output_model: type[BaseModel],
        *,
        action: str,
        images=(),
        documents=(),
        overrides: dict | None = None,
        cache_prefix: bool = False,
        tools: Any | None = None,
    ) -> Any:
        """Shared structured-output call with a bounded off-schema retry.

        ``make_messages(feedback)`` returns the conversation for one attempt;
        ``feedback`` is ``None`` on the first try and the prior validation error
        thereafter. Native structured output enforces the schema at the provider,
        so a bad shape is rare — but a transient off-schema result must not kill
        the run. Cap (``structured_retry_cap``) exhaustion still raises loudly.

        ``tools`` (a provider, when the conversation replays a tool trajectory) is declared
        alongside the schema with ``tool_choice="none"`` — see :meth:`_call_over_messages`.
        """
        cap = max(1, _structured_retry_cap())
        last_exc: Exception | None = None
        for attempt in range(cap):
            feedback: str | None = None
            if attempt > 0:
                feedback = (
                    f"Your previous response was not a valid {output_model.__name__}: "
                    f"{last_exc}. Return a valid {output_model.__name__} matching the "
                    "schema exactly — do not wrap or nest the fields."
                )
            try:
                response = await self.model.call(
                    make_messages(feedback),
                    schema=output_model,
                    tools=tools.tool_definitions() if tools is not None else None,
                    tool_choice="none",
                    images=images,
                    documents=documents,
                    cache_prefix=cache_prefix,
                )
                self.last_response = response
                await self._record_context_edit(response, action)
                value = response.parsed
                if value is None:
                    raise ValueError(
                        f"{self.role}.{action}: provider returned no structured output for "
                        f"{output_model.__name__}"
                    )
                if overrides:
                    if not isinstance(value, dict):
                        raise ValueError(
                            f"{self.role}.{action}: cannot apply overrides to non-object output"
                        )
                    value = {**value, **overrides}
                parsed = output_model.model_validate(value)
            except (ValidationError, ValueError) as exc:
                # Shape problems are retryable; everything else (auth, network,
                # rate limits — already retried by the SDK) propagates at once.
                last_exc = exc
                if attempt < cap - 1:
                    await self._record(
                        f"{self.role}.{action}.retry",
                        level=4,
                        summary=f"{self.role} got an off-schema {action} result; retrying",
                        data={"attempt": attempt + 1, "cap": cap, "error": repr(exc)},
                    )
                    continue
                await self._record(
                    f"{self.role}.{action}.error",
                    level=2,
                    summary=f"{self.role} failed during {action}",
                    data={"error": repr(exc)},
                )
                raise
            except Exception as exc:
                await self._record(
                    f"{self.role}.{action}.error",
                    level=2,
                    summary=f"{self.role} failed during {action}",
                    data={"error": repr(exc)},
                )
                raise
            await self._record(
                f"{self.role}.{action}.output",
                level=10,
                summary=f"{self.role} returned {action} output",
                data={
                    "output": parsed,
                    "usage": response.usage,
                    "raw_content": response.content,
                },
            )
            return parsed

    async def _react(
        self,
        *,
        seed_messages: Sequence[Any],
        tools: Any,  # ToolProvider — duck-typed to avoid import cycle
        action: str,
        cap: int,
        images: Sequence[FigureRef] = (),
        on_result=None,
        free_tools: frozenset[str] = frozenset(),
        absolute_cap: int | None = None,
        terminal_tools: frozenset[str] = frozenset(),
    ) -> tuple[list, list[FigureRef], list[DocumentRef]]:
        """Run an inner ReAct loop: gather evidence by calling tools to completion.

        Each iteration calls the model with ``tools.tool_definitions()`` and a
        growing conversation. If the model emits tool calls, each is executed via
        ``tools.execute(name, arguments)``; the assistant ``tool_use`` turn and the
        ``tool_result`` turn are appended so the model sees its own results
        in-context and can chain dependent calls. The loop ends when the model
        emits no tool calls (it's done) or the budget is spent.

        Budget: ``cap`` bounds the **budget-consuming** iterations — those calling ≥1
        tool NOT in ``free_tools``. ``free_tools`` (default empty) are budget-free: an
        iteration that calls only free tools does not count toward ``cap``. This lets the
        analyst lead debate a toolkit design with the human (``plan_and_debate``, §5.5)
        across as many rounds as it needs to converge without starving the evidence-
        gathering iterations the same loop is for; the human ending the debate is the
        normal stop. With ``free_tools`` empty the loop is identical to ``range(cap)``.
        ``absolute_cap`` (config ``react_loop_absolute_cap``, passed only by the lead's
        design debate) is a hard ceiling on TOTAL iterations — the runaway-free-tool backstop;
        when ``None`` it defaults to ``cap`` (so non-opt-in callers are unchanged).

        ``terminal_tools`` names tools whose successful call **ends the loop**: the role has
        filed the form its turn exists to produce, and there is nothing further to gather. The
        loop stops after appending that call's result, so the trajectory stays valid (every
        ``tool_use`` keeps its ``tool_result``) and the filed arguments are readable from
        :attr:`last_filed_form`.

        Before this existed the convention was carried entirely by prose — ``submit_for_review``'s
        description tells the model *"this is the ONLY way to finish"* — and enforced afterwards by
        Python inspecting side effects (``session.last_commit_sha``, ``workspace.written_report``). A
        model that simply kept calling tools ran to its cap, and a model that stopped without
        filing looked identical to one that had. Naming the terminal tools makes the loop's own
        exit condition match the contract the descriptions state.

        A terminal tool whose handler returns ``is_error`` does **not** end the loop: a rejected
        form is a correction to act on, which is what lets the phase check live in the handler.

        Returns ``(messages, figures, documents)``: the full trajectory (seed + all
        turns) for a terminal :meth:`_call_over_messages`, the figures produced by
        tools, and the documents a tool attached (e.g. PDFs opened via ``open_paper``).
        Accumulated documents are re-attached to every subsequent model call so the
        model can read them multimodally mid-loop; ``cache_prefix=True`` keeps the
        stable system+tools prefix cached so only the growing tail is re-sent.
        ``on_result(call, outcome)`` is an optional async hook (monitor/memory).
        """
        messages: list = list(seed_messages)
        figures: list[FigureRef] = []
        documents: list[DocumentRef] = []
        seen_docs: set[str] = set()
        # Which terminal form this loop filed, and with what. `None` after the loop means the
        # role never filed one — the failure the caller has to treat as such.
        self.last_filed_form: tuple[str, dict] | None = None
        filed_terminal = False
        budget_cap = max(1, cap)
        absolute_ceiling = max(budget_cap, absolute_cap) if absolute_cap is not None else budget_cap
        budget_used = 0
        iteration = 0
        silent_turns = 0  # consecutive tool-call-free turns nudged through (F5b)
        while budget_used < budget_cap and iteration < absolute_ceiling:
            # Cooperative HITL steer (§12): before the next model turn, inject any human steer for
            # this role. A no-op when run_control is unset. A steer rides as a user turn (the
            # images= path already appends user turns after tool results, so this is safe).
            # A /stop is NOT read here: it cancels the run task outright (see
            # ``RunControl.interrupt``), so there is nothing for this loop to notice.
            if self.run_control is not None:
                for steer in self.run_control.drain_steer(self.agent_key):
                    messages.append(ModelMessage(role="user", content=f"[Human steer] {steer}"))
                    await self._record(
                        f"{self.role}.{action}.steer", level=3,
                        summary=f"{self.role} received a human steer",
                        data={"message": _cap_text(steer, _event_arg_cap())},
                    )
            response = await self.model.call(
                messages,
                tools=tools.tool_definitions(),
                tool_choice="auto",
                images=images if iteration == 0 else (),
                documents=documents,
                cache_prefix=True,
            )
            self.last_response = response
            await self._record_context_edit(response, action)
            if not response.tool_calls:
                # A turn with no tool calls is FIVE different outcomes, and breaking here
                # collapsed them into "the model is done" (F5b). Slot 3's lead returned
                # `output_tokens: 2` with no content after nine consecutive tool-result turns and
                # the run died, because the loop read a documented provider quirk as a finished
                # turn: Anthropic states *"Sometimes Claude returns an empty response (exactly 2-3
                # tokens with no content) with `stop_reason: "end_turn"` ... particularly after
                # tool results"*, with the prevention *"don't retry empty responses without
                # modification — add a continuation prompt in a NEW user message"*
                # (platform.claude.com/docs/en/build-with-claude/handling-stop-reasons).
                #
                # `response.stop` classifies it from that vendor's own field (F5a). The class
                # decides what we SAY next; it never decides that the loop is over — that is the
                # terminal tool's job, because a tool call means the same thing on all six vendors
                # while these fields do not (Z.AI reports a dropped inference as `network_error`,
                # which the ecosystem maps to `stop`).
                nudge = _continuation_for(response, terminal_tools)
                await self._record(
                    f"{self.role}.{action}.silent_turn", level=4,
                    summary=(
                        f"{self.role} returned no tool calls (stop={response.stop}); "
                        + ("continuing" if nudge else "ending the loop")
                    ),
                    data={
                        "stop": response.stop,
                        "output_tokens": response.usage.output_tokens,
                        "content_chars": len(response.content or ""),
                        "iteration": iteration,
                        "continued": bool(nudge),
                    },
                )
                if nudge is None or silent_turns >= _SILENT_TURN_CAP:
                    break
                silent_turns += 1
                # The whole turn is replayed, then the nudge as its own NEW user message — never
                # appended to a tool-result turn, which is the arrangement Anthropic names as the
                # cause of the empty response in the first place.
                messages.append(response)
                messages.append(ModelMessage(role="user", content=nudge))
                continue
            silent_turns = 0
            # The turn we replay IS the turn we received: `ModelResponse` is the neutral
            # assistant turn, so it goes back on the conversation whole rather than being
            # re-typed field by field. That copy used to live here, and it lost a part every
            # time the provider part-set grew (tool-search discovery blocks until 2026-07-27,
            # thinking blocks until 2026-08-05).
            messages.append(response)
            result_parts: list[ToolResultPart] = []
            # Steers that arrive WHILE this batch runs. A model turn can emit many tool calls, and
            # the loop dispatches them all before it comes back round to the top-of-iteration
            # drain — so a human's message could sit unseen behind a whole batch of long calls
            # (observed 2026-07-29: two steers waited behind a pair of 25-minute downloads and
            # landed only when the batch broke). Collected here, appended as a user turn right
            # after this batch's results, so the very next model call sees them.
            batch_steers: list[str] = []
            for call in response.tool_calls:
                # Input event — written (and flushed) BEFORE the call runs, so a tool that
                # hangs and never returns still leaves a record of exactly what was issued.
                # Output is a separate event below, written when execute() returns; the two
                # share `tool_call_id` so a hung call is the input with no matching output.
                await self._record(
                    f"{self.role}.{action}.tool_call",
                    level=7,
                    summary=f"{self.role} calling {call.name}",
                    data={
                        "iteration": iteration + 1,
                        "tool": call.name,
                        "tool_call_id": call.id,
                        # Full-fidelity (capped) call args, so a run is diagnosable from the
                        # JSONL alone (terminal ignores these keys).
                        "args": _cap_text(
                            json.dumps(dict(call.arguments), default=str, sort_keys=True),
                            _event_arg_cap(),
                        ),
                    },
                )
                outcome = await tools.execute(call.name, dict(call.arguments))
                result_parts.append(
                    ToolResultPart(
                        tool_call_id=call.id,
                        name=call.name,
                        content=outcome.text,
                        is_error=outcome.is_error,
                        # Carry the figures on the turn so the adapter renders them inline
                        # in history (an adjacent image turn), keeping them visible every
                        # later round and in the terminal report call, and cache-stable.
                        figures=outcome.figures,
                    )
                )
                # Also accumulate for the return value: worker.analyze staples these onto
                # AnalystReport.figures (structured output can't reproduce a FigureRef).
                figures.extend(outcome.figures)
                for doc in outcome.documents:
                    if doc.sha256 not in seen_docs:
                        seen_docs.add(doc.sha256)
                        documents.append(doc)
                if on_result is not None:
                    await on_result(call, outcome)
                await self._record(
                    f"{self.role}.{action}.tool_result",
                    level=7,
                    summary=(
                        f"{self.role} tool {call.name} "
                        f"{'failed' if outcome.is_error else 'returned'}"
                    ),
                    data={
                        "iteration": iteration + 1,
                        "tool": call.name,
                        "tool_call_id": call.id,
                        "is_error": outcome.is_error,
                        "chars": len(outcome.text),
                        "figure_count": len(outcome.figures),
                        # Returned text at full fidelity (capped); args live on the paired
                        # `tool_call` input event above.
                        "text": _cap_text(outcome.text, _event_result_cap()),
                    },
                )
                # A terminal form, accepted by its handler, is this loop's answer. Recorded here
                # but acted on after the whole batch is appended, so the trajectory never ends
                # mid-batch with a `tool_use` whose `tool_result` was dropped.
                if call.name in terminal_tools and not outcome.is_error:
                    self.last_filed_form = (call.name, dict(call.arguments))
                    filed_terminal = True
                if self.run_control is not None:
                    batch_steers.extend(self.run_control.drain_steer(self.agent_key))
            messages.append(ToolResultMessage(results=result_parts))
            for steer in batch_steers:
                # Ordered after the tool results (assistant → tool results → user), so the turn
                # sequence stays valid for every provider.
                messages.append(ModelMessage(role="user", content=f"[Human steer] {steer}"))
                await self._record(
                    f"{self.role}.{action}.steer",
                    level=3,
                    summary=f"{self.role} received a human steer (mid-batch)",
                    data={"message": _cap_text(steer, _event_arg_cap())},
                )
            # Budget accounting: an iteration consumes budget unless EVERY tool it called is
            # free (e.g. plan_and_debate). Free-only iterations advance `iteration` (so the
            # absolute backstop still bounds them) but not `budget_used`.
            consumed_budget = any(call.name not in free_tools for call in response.tool_calls)
            if consumed_budget:
                budget_used += 1
            iteration += 1
            await self._record(
                f"{self.role}.{action}.tools",
                level=7,
                summary=f"{self.role} ran {len(result_parts)} tool(s) (iteration {iteration})",
                data={
                    "iteration": iteration,
                    "cap": cap,
                    "budget_used": budget_used,
                    "budget_free": not consumed_budget,
                    "calls": [
                        {"name": p.name, "is_error": p.is_error, "chars": len(p.content)}
                        for p in result_parts
                    ],
                },
            )
            # The form is filed and its result is on the trajectory: stop. Placed after the whole
            # batch is appended and accounted for, so a turn that filed alongside other calls
            # still leaves every `tool_use` paired with its `tool_result`.
            if filed_terminal:
                await self._record(
                    f"{self.role}.{action}.filed",
                    level=3,
                    summary=(
                        f"{self.role} filed {self.last_filed_form[0]} — "
                        f"{action} loop complete"
                    ),
                    data={
                        "form": self.last_filed_form[0],
                        "iteration": iteration,
                        "budget_used": budget_used,
                    },
                )
                break
        return messages, figures, documents

    async def _record(
        self,
        event: str,
        *,
        level: int,
        summary: str,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        if self.monitor is not None:
            record_data = dict(data or {})
            model_label = _model_label(self.model)
            if model_label:
                record_data.setdefault("model", model_label)
            await self.monitor.record(
                _team_for_role(self.role),
                event,
                level=level,
                summary=summary,
                # The indexed agent_key when there is one, so a line names the SAME identity the
                # steer channel addresses (`analyst_worker_0`, not the shared role
                # `analyst_worker`). Two workers were previously indistinguishable in the log, so
                # neither a reader nor a human deciding where to send a steer could tell them
                # apart. Falls back to the role for roles that carry no key.
                actor=getattr(self, "agent_key", None) or self.role,
                data=record_data,
            )

    async def _record_context_edit(self, response: Any, action: str) -> None:
        """Record when provider-native context editing cleared stale context on the last
        model call (``ModelResponse.context_edits`` — Anthropic only, empty otherwise), so a
        run-watcher sees context pressure relieved. A no-op when nothing was cleared. NOT LIVE
        TESTED (2026-07-22)."""
        edits = getattr(response, "context_edits", None)
        if not edits:
            return
        cleared_tokens = sum(int(e.get("cleared_input_tokens", 0) or 0) for e in edits)
        cleared_uses = sum(int(e.get("cleared_tool_uses", 0) or 0) for e in edits)
        await self._record(
            f"{self.role}.{action}.context_edit",
            level=4,
            summary=(
                f"{self.role} context editing cleared {cleared_uses} tool result(s) "
                f"(~{cleared_tokens} tokens)"
            ),
            data={"edits": edits},
        )


class ReviewerAgent(AgentRole):
    """Reviewer role: compare request, literature, and analysis into a verdict."""

    role = "reviewer"

    async def review(
        self,
        request: ResearchRequest,
        *,
        report: AnalystReport | None = None,
    ) -> ReviewVerdict:
        """Return a review verdict for the supplied research state.

        The reviewer pulls literature itself: it searches the local knowledge base
        (``text_search``) and reads chosen papers (``open_paper``) inside its ReAct
        loop, so the papers it opens arrive as ``DocumentRef`` attachments via the
        loop and are forwarded into the terminal verdict call.
        """

        payload = {
            "request": request.model_dump(mode="json"),
            "analysis_report": report.model_dump(mode="json") if report else None,
        }
        images = report.figures if report else ()

        if self.tools is None:
            return await self._call(
                payload, ReviewVerdict, images=images, action="review"
            )

        # Investigate run artifacts + the knowledge base via read-only tools (seeing
        # the analysis figures from the first turn), opening the papers it needs, then
        # issue the verdict grounded in that investigation and the opened papers.
        seed = [
            ModelMessage(role="system", content=self.prompt),
            ModelMessage(role="user", content=_payload_text(payload)),
        ]
        messages, _, docs = await self._react(
            seed_messages=seed, tools=self.tools, action="review",
            cap=_react_loop_cap(), images=images,
        )
        return await self._call_over_messages(
            messages, ReviewVerdict, action="review", images=images, documents=docs,
            tools=self.tools,
        )

    async def compose_machine_report(
        self,
        request: ResearchRequest,
        report: AnalystReport,
        references: "list",
        verdict: ReviewVerdict,
        run_id: str,
    ) -> "ResearchReport":
        """Compose a structured machine-readable research report (JSONL handle).

        After the LLM returns the parsed report, figures/references/run_id/created_at
        are overwritten deterministically so machine refs are correct by construction.
        Only the prose fields (title, abstract, sections, key_findings) are LLM-authored.
        """
        from datetime import UTC, datetime

        from auto_research.schemas.report import ResearchReport

        payload = {
            "output_format": "jsonl",
            "output_format_instructions": (
                "You are composing a structured machine-readable research report in JSON. "
                "Populate title, abstract, sections (list of {heading, body}), and key_findings. "
                "The runtime will overwrite figures, references, run_id, and created_at — "
                "you may leave them as empty lists / empty string / now."
            ),
            "request": request.model_dump(mode="json"),
            "analysis_report": report.model_dump(mode="json"),
            "verdict_rationale": verdict.rationale,
            "references": [r.model_dump(mode="json") for r in references],
            "run_id": run_id,
        }
        result: ResearchReport = await self._call(
            payload,
            ResearchReport,
            images=report.figures,
            action="compose_machine_report",
            overrides={
                "run_id": run_id,
                "request_question": request.question,
                "verdict_rationale": verdict.rationale,
                "created_at": datetime.now(UTC),
            },
        )
        return result.model_copy(
            update={
                "run_id": run_id,
                "figures": report.figures,
                "references": references,
            }
        )

    async def author_latex_report(
        self,
        request: ResearchRequest,
        report: AnalystReport,
        staged_figures: "list[tuple]",
        staged_papers: "list[tuple]",
        verdict: ReviewVerdict,
        *,
        tex_path: Path,
        validator,
        roots: "Sequence[Path]",
        cap: int,
    ) -> Path:
        """Author the LaTeX report by **editing the file**, ending on a validated ``turn_end``.

        Replaces the recompose loop (F5e). That loop asked for a whole `LatexReport` through
        structured output on every attempt, fed the previous attempt's compile errors back as
        prose, and got a fresh document each time — which is how [D7] happened: the retry saw
        `! Undefined control sequence.` with no macro named and re-emitted the same document.
        Two problems the file-editing shape removes:

        1. **A regeneration can silently undo an earlier fix.** Editing keeps every fix that
           already landed, and `file_edit`'s targeted mode makes that the cheap path.
        2. **`LatexReport` is structured output, and the contrast arm's reviewer cannot reliably
           produce it.** `ResearchReport`/`AuthoringDecision` failed on `qwen3.8-max` at every
           reasoning effort (2026-08-24: unresolved `$ref` in `list[Model]` fields, so the model
           invented a shape and returned HTTP 200). Nothing here declares an output schema.

        The validator is the door: figures resolve, links resolve, and pdflatex compiles. A
        failing `turn_end` returns the specific errors and the loop continues with the tools.
        """
        from auto_research.agents.react import TURN_END_TOOL, FILE_EDIT_TOOL, ReadOnlyToolProvider

        fig_descriptions = [
            {"relpath": relpath, "caption": figref.caption, "sha256": figref.sha256}
            for figref, relpath in staged_figures
        ]
        payload = {
            "task": (
                "Write the LaTeX research report for this run, then end your turn. Draft it with "
                f"`{FILE_EDIT_TOOL}`, read it back with `read_file`, and call `{TURN_END_TOOL}` "
                "when it is complete — the harness then checks that every figure and link "
                "resolves and that the document compiles, and tells you precisely what to fix if "
                "it does not."
            ),
            # Carried over verbatim in substance from the retired `compose_latex_report`. Both
            # clauses are fixes for real run-killers and neither is optional:
            #
            # [D6] the preamble list. `\lesssim` with no `amssymb` is a fatal compile error, and
            # it exhausted `report_retry_cap` on Q21 (2026-08-10) — the run finished with a
            # `report.tex` and no `report.pdf`. `prompts/reviewer.md` mandates the same list; the
            # changelog records that BOTH were needed, so this is not redundant.
            #
            # The references clause is a no-fabrication guard: the References section is
            # hyperlinks to the local PDFs actually used, never invented author/title text.
            "format_requirements": (
                "Write ordinary LaTeX source. Load graphicx, amsmath, amssymb and hyperref in the "
                "preamble, plus any further package a symbol you use requires — amssymb carries "
                "the relation symbols science prose reaches for (\\lesssim, \\gtrsim, \\simeq), "
                "and a symbol whose package is absent is a fatal compile error that costs the "
                "whole PDF. Use \\includegraphics{<relpath>} verbatim for each figure relpath "
                "supplied below. Cite papers in prose in the body. The References section is a "
                "list of hyperlinks to the local PDFs you used: for each supplied paper relpath "
                "emit \\href{<relpath>}{<relpath>} using the relpath verbatim. Use ONLY the "
                "supplied paper relpaths; add no \\cite, no \\begin{thebibliography}, and no "
                "invented author/title text."
            ),
            "output_path": tex_path.name,
            "request": request.model_dump(mode="json"),
            "analysis_report": report.model_dump(mode="json") if report else None,
            "verdict_rationale": verdict.rationale,
            "figures": fig_descriptions,
            "paper_links": [relpath for _, relpath in staged_papers],
        }
        provider = ReadOnlyToolProvider(
            roots=list(roots),
            include_text_search=False,
            editable_file=tex_path,
            turn_end_validator=validator,
        )
        seed = [
            ModelMessage(role="system", content=self.prompt),
            ModelMessage(role="user", content=_payload_text(payload)),
        ]
        await self._react(
            seed_messages=seed,
            tools=provider,
            action="author_latex_report",
            cap=cap,
            images=report.figures if report else (),
            # Both are free: the drafting is the work, and the validation must not compete with
            # it for budget (F5c/D3).
            free_tools=frozenset({FILE_EDIT_TOOL, TURN_END_TOOL}),
            terminal_tools=frozenset({TURN_END_TOOL}),
        )
        return tex_path

class LibrarianIOAgent(AgentRole):
    """Librarian I/O role: compose search strategies, query external sources, and abstract papers."""

    role = "librarian_io"

    def __init__(
        self,
        *,
        sources: Mapping[str, QuerySource] | None = None,
        storage: Storage | None = None,
        model: CallableModel | None = None,
        model_group: ModelGroupHandle | None = None,
        model_name: str | None = None,
        prompt: str | None = None,
        monitor: RunMonitor | None = None,
    ):
        super().__init__(
            model=model,
            model_group=model_group,
            model_name=model_name,
            prompt=prompt,
            monitor=monitor,
        )
        self.sources = dict(sources) if sources is not None else _default_sources()
        self.storage = storage or Storage()

    async def compose_search_strategy(
        self,
        request: ResearchRequest,
        *,
        follow_up: str | None = None,
    ) -> SearchRequest:
        """Compose a focused search strategy from a research request."""

        payload = {
            "research_request": request.model_dump(mode="json"),
            "follow_up": follow_up,
        }
        return await self._call(payload, SearchRequest, action="compose_search_strategy")

    async def gather_literature(
        self,
        request: ResearchRequest,
        *,
        follow_up: str | None = None,
    ) -> list[SearchResult]:
        """ReAct over literature sources: search, read results, refine, repeat.

        Runs an inner loop over a :class:`SourceToolProvider` (arxiv/ADS/web +
        local text_search), letting the model iteratively refine its queries until
        it has a focused set, then returns the de-duplicated accumulated results.
        Falls back to the single-shot compose→search path when no sources are
        configured.
        """
        if not self.sources:
            return await self.search(await self.compose_search_strategy(request, follow_up=follow_up))

        provider = SourceToolProvider(self.sources)
        payload = {
            "research_request": request.model_dump(mode="json"),
            "follow_up": follow_up,
            "instruction": (
                "Search the literature sources for papers relevant to this request. "
                "Read each batch of results, refine your query, and search again as "
                "needed across sources. Stop when you have a focused, relevant set."
            ),
        }
        seed = [
            ModelMessage(role="system", content=self.prompt),
            ModelMessage(role="user", content=_payload_text(payload)),
        ]
        await self._react(
            seed_messages=seed, tools=provider, action="gather_literature",
            cap=_librarian_react_loop_cap(),
        )
        return _dedup_results(provider.results)

    async def search(self, request: SearchRequest) -> list[SearchResult]:
        """Query external sources and return raw results.

        No LLM call: the I/O team does not filter or rank results. Per-paper
        abstraction is a separate step via ingest_results().
        A failing source is skipped so the others can still contribute results.
        """

        selected = request.sources or ["arxiv", "nasa_ads", "web"]
        results: list[SearchResult] = []
        for source_name in selected:
            source = self.sources.get(source_name)
            if source is None:
                continue
            try:
                results.extend(await source.search(request.query, limit=request.max_results))
            except Exception:
                pass  # one source failing does not abort the others
        return results

    async def ingest_document(
        self,
        document: DocumentRef,
        *,
        peer_abstracts: Sequence[MemoryRecord] = (),
        paper_id: int | None = None,
        instructions: str | None = None,
    ) -> MemoryRecord:
        """Read a document attachment and return one memory abstraction."""

        payload = {
            "document": document.model_dump(mode="json"),
            "paper_id": paper_id,
            "instructions": instructions
            or (
                "Compose a durable memory abstraction: abstract, key points, key methods, "
                "data, and analysis results useful for future text search."
            ),
            "peer_abstracts": [a.model_dump(mode="json") for a in peer_abstracts],
        }
        return await self._call(
            payload,
            MemoryRecord,
            documents=[document],
            action="ingest_document",
        )

    async def abstract_document(
        self,
        document: DocumentRef,
        *,
        peer_abstracts: Sequence[MemoryRecord] = (),
    ) -> MemoryRecord:
        """Worker shim: produce one memory abstraction for IO debate."""

        return await self.ingest_document(document, peer_abstracts=peer_abstracts)

    async def judge_abstract_convergence(
        self,
        document: DocumentRef,
        abstracts: Sequence[MemoryRecord],
        *,
        round_index: int,
        max_rounds: int,
    ) -> ReviewVerdict:
        """Lead role: judge whether IO worker abstractions have converged."""

        payload = {
            "document": document.model_dump(mode="json"),
            "abstracts": [a.model_dump(mode="json") for a in abstracts],
            "round_index": round_index,
            "max_rounds": max_rounds,
            "decision_semantics": (
                "accepted=true means workers have converged to a consistent memory abstraction; "
                "accepted=false means another round would improve coverage or consistency."
            ),
        }
        return await self._call(
            payload,
            ReviewVerdict,
            documents=[document],
            action="judge_abstract_convergence",
        )

    async def adjudicate_abstract(
        self,
        document: DocumentRef,
        abstracts: Sequence[MemoryRecord],
    ) -> MemoryRecord:
        """Lead role: synthesize the final memory record from worker abstractions."""

        payload = {
            "document": document.model_dump(mode="json"),
            "worker_abstracts": [a.model_dump(mode="json") for a in abstracts],
            "instructions": (
                "Synthesize the worker abstractions into one final memory record. "
                "Produce a mem_abbrev that is a comprehensive, durable summary of the paper's "
                "key findings, methods, data analysis, and results."
            ),
        }
        return await self._call(
            payload,
            MemoryRecord,
            documents=[document],
            action="adjudicate_abstract",
        )

    async def ingest_results(self, results: list[SearchResult]) -> None:
        """Download, abstract, and store each search result in parallel.

        Papers already abstracted (mem_abbrev populated in memory table) are
        skipped — PDFs are re-downloaded only if the file is absent.
        Concurrency is capped at 4 simultaneous ingests to respect API rate limits.
        """

        consensus = IODebateToConverge(max_rounds=2, monitor=self.monitor)
        sem = asyncio.Semaphore(4)

        async def _store_metadata_only(result: SearchResult) -> None:
            """Record a no-PDF search result as an orphan memory note (never a paper row).

            A paper row requires a downloaded PDF (its sha256 is the identity); a result
            we could not fetch a PDF for is kept as a free-standing memory note (paper_id
            None) so its title/abstract stay searchable without polluting the paper table.
            """
            await self.storage.insert_memory(
                MemoryRecord(
                    paper_id=None,
                    summary=result.title,
                    tags=result.keywords,
                    mem_abbrev=result.abstract,
                )
            )

        async def _ingest_one(result: SearchResult) -> None:
            async with sem:
                try:
                    stored = await download_and_ingest_result(
                        self.storage, result, target_dir=papers_dir()
                    )
                    if stored is not None and stored.local_path is not None:
                        existing = await self.storage.get_memory_for_paper(stored.id)
                        if existing is not None and existing.mem_abbrev:
                            return
                        doc = document_from_file(stored.local_path)
                        memory = await consensus.run([self, self], self, doc)
                        await self.storage.replace_memory_for_paper(
                            MemoryRecord(
                                paper_id=stored.id,
                                summary=memory.summary,
                                tags=memory.tags,
                                mem_abbrev=memory.mem_abbrev,
                            )
                        )
                    else:
                        await _store_metadata_only(result)
                except Exception as exc:  # noqa: BLE001
                    # One paper's ingest failure (a document the model API rejects,
                    # a provider fault, an oversized PDF) must not abort the whole
                    # literature node. Degrade to metadata-only ingest and continue;
                    # a failed batch member is never allowed to propagate.
                    await self._record(
                        "librarian_io.ingest_document.error",
                        level=3,
                        summary=f"Ingest fell back to metadata: {result.title[:80]}",
                        data={"error": repr(exc)},
                    )
                    try:
                        await _store_metadata_only(result)
                    except Exception:  # noqa: BLE001
                        pass  # give up on this one paper; never fail the batch

        await asyncio.gather(*(_ingest_one(r) for r in results))

    async def evaluate_search_results(
        self,
        request: ResearchRequest,
        results: list[SearchResult],
    ) -> ReviewVerdict:
        """Phase 1 stub — IO search iteration is deferred. See CLAUDE.md §13."""

        _ = (request, results)
        return ReviewVerdict(
            accepted=True,
            rationale="IO search evaluation deferred — see CLAUDE.md §13",
        )


class AnalystWorkerAgent(AgentRole):
    """Analyst worker role: investigate with tools, then produce one report.

    Each ``analyze()`` runs an inner ReAct loop (:meth:`AgentRole._react`) over a
    ``SandboxToolProvider`` — calling toolkit/MCP tools in the attached
    ``PersistentSandbox``, seeing each result in-context, and chaining dependent
    calls (load → fit → run → plot) to completion — then emits an ``AnalystReport``
    grounded in those results (:meth:`AgentRole._call_over_messages`). The live
    conversation is the within-call working memory; ``memory_path`` is the durable
    cross-round/cross-worker store (prior results are read in and new results
    appended). ``last_tool_results`` exposes the loop's results for the consensus
    peer summary. With no sandbox/registry (e.g. Studio), it falls back to a plain
    tool-less structured call.
    """

    role = "analyst_worker"

    def __init__(
        self,
        *,
        available_toolkits: Sequence[str] = ("research_toolkit.xspec_utils",),
        sandbox: Any | None = None,  # PersistentSandbox | None — avoid circular import
        registry: Any | None = None,  # ToolkitRegistry | None
        memory_path: Path | None = None,
        worker_index: int = 0,
        human_handler: Any | None = None,
        model: CallableModel | None = None,
        model_group: ModelGroupHandle | None = None,
        model_name: str | None = None,
        prompt: str | None = None,
        monitor: RunMonitor | None = None,
    ):
        super().__init__(
            model=model,
            model_group=model_group,
            model_name=model_name,
            prompt=prompt,
            monitor=monitor,
        )
        self.available_toolkits = tuple(available_toolkits)
        self.sandbox = sandbox
        self.registry = registry
        self.memory_path = memory_path
        self.worker_index = worker_index
        # When wired, the worker can raise a clarification to a human via ask_human (§9.7).
        self.human_handler = human_handler
        self.last_tool_results: list[ToolResult] = []
        # V-4: this agent's stable working-folder key + report dir (set by runtime; defaults derive
        # from memory_path). agent_key is the figure-catalog source key the lead addresses it by.
        self.agent_key: str | None = None
        self.report_dir: Path | None = None
        # V-4: append-only figure catalog (source "self" = figures this worker's tools produced),
        # persisting across debate rounds and rework iterations so nothing is dropped; the per-round
        # report (ReAct-refined via edit_report) cites from it. Monotonic debate-round counter names
        # the per-round report file; last report seeds the next round's review_report(None).
        self._figure_catalog: dict[str, list[FigureRef]] = {"self": []}
        self._round = 0
        self._last_report: AnalystReport | None = None

    @property
    def is_alive(self) -> bool:
        """True while the attached sandbox is running (or when no sandbox is used)."""
        if self.sandbox is None:
            return True
        return bool(self.sandbox.is_alive)

    async def analyze(self, task: AnalysisTask, *, peer_reports: Sequence[AnalystReport] = ()) -> AnalystReport:
        """Investigate the task with tools (ReAct), then produce an AnalystReport.

        Reads private ``memory`` (durable prior results) into the seed; runs the
        ReAct loop to gather evidence; then composes the report over the gathered
        trajectory, with the loop's real figures attached. Falls back to a plain
        structured call when no sandbox+registry is attached.
        """
        self.last_tool_results = []
        self._figure_catalog.setdefault("self", [])
        memory = read_memory(self.memory_path) if self.memory_path else ""
        payload = {
            "task": task.model_dump(mode="json"),
            "peer_reports": [report.model_dump(mode="json") for report in peer_reports],
            "available_toolkits": list(self.available_toolkits),
            "memory": memory or None,
        }
        peer_figures = [figure for report in peer_reports for figure in report.figures]

        # No tool surface (e.g. Studio): plain tool-less structured report.
        if self.sandbox is None or self.registry is None:
            return await self._call(payload, AnalystReport, images=peer_figures, action="analyze")

        if not self.sandbox.is_alive:
            await self.sandbox.start()

        self._round += 1
        cwd = self.memory_path.parent if self.memory_path is not None else None
        workspace = self._make_report_workspace()
        provider = SandboxToolProvider(
            self.registry, self.sandbox, cwd=cwd,
            human_handler=self.human_handler, web_source=WebSource(),
            asked_by="analyst_worker",
            model=_model_label(self.model) or "",
            figure_sink=self._figure_catalog["self"],
            report_workspace=workspace,
        )
        seed = [
            ModelMessage(role="system", content=self.prompt),
            ModelMessage(role="user", content=_payload_text(payload)),
        ]
        # edit_report/review_report are budget-free (report authoring, not evidence gathering),
        # so refining figure placement never starves the tool-iteration budget (absolute_cap backstops).
        messages, react_figures, _ = await self._react(
            seed_messages=seed,
            tools=provider,
            action="analyze",
            cap=_react_loop_cap(),
            images=peer_figures,
            # turn_end is free too, by ruling: the editing burns budget, so the loop is bounded
            # by the work rather than by the validation (F5c/D3).
            free_tools=frozenset({EDIT_REPORT_TOOL, REVIEW_REPORT_TOOL, TURN_END_TOOL}),
            terminal_tools=frozenset({TURN_END_TOOL}),
            absolute_cap=_react_loop_absolute_cap(),
        )
        self.last_tool_results = provider.results
        if self.memory_path and provider.results:
            _append_tool_results_to_memory(self.memory_path, provider.results)

        # V-4: the report is what the worker persisted via edit_report this round (figures cited by
        # index from its catalog, resolved by the runtime).
        #
        # There is NO fallback compose (F5d). It used to compose an `AnalystReport` on the model's
        # behalf whenever the loop ended without an `edit_report` call, and that path was the only
        # one that sent the model the runtime-owned `figures`/`FigureRef` tree — `edit_report`'s
        # own schema has no `figures` property, because the runtime resolves figures from
        # citations. `_sanitize_strict` then promoted that optional, runtime-owned field to
        # *required*, two instructions told the model to fill every required field, and the field's
        # own description said "Resolved by the runtime from cited_figures; leave it alone". Slot 5
        # (2026-09-08) filled it with `placeholder_timeseries`/`_stokes`/`_chi2`/`_sf` and the run
        # died on `model_validate` — one line before `model_copy` would have replaced all four with
        # the real catalog. Not fabrication under pressure: 3 errors in 54 tool results, zero
        # rejections that round, the four real PNGs on disk, correct citations for two prior rounds,
        # and the inventions self-labelled. The harness asked for it.
        #
        # The loop now ends on `turn_end`, whose validator refuses to accept an unwritten report
        # and names why, so the model writes it with the tool that exists for that. This also
        # matches what the lead has always done: "A report it wrote but never filed is real
        # evidence it produced, so it is used rather than discarded; nothing is composed on its
        # behalf."
        report = workspace.written_report if workspace is not None else None
        if report is None:
            report = AnalystReport(
                summary=(
                    "This worker's loop ended without a saved report. No report was composed on "
                    "its behalf — the material a model did not write is not evidence."
                ),
                status="failed",
            )
            await self._record(
                "analyst_worker.analyze.no_report", level=3,
                summary="worker ended its loop with no report written; recorded as failed",
                data={"iterations": len(messages)},
            )
        self._last_report = report
        return report

    def _make_report_workspace(self) -> ReportWorkspace | None:
        """Build this round's report workspace (V-4), or None when no report dir is wired.

        The report dir is the agent's working folder (``report_dir``, else ``memory_path.parent``);
        the per-round JSONL path is derived from role + model + the monotonic debate round.
        """
        report_dir = self.report_dir or (
            self.memory_path.parent if self.memory_path is not None else None
        )
        if report_dir is None:
            return None
        return ReportWorkspace(
            report_path=round_report_path(
                report_dir,
                role=self.role,
                model=_model_label(self.model) or "model",
                round_n=self._round,
            ),
            report_model=AnalystReport,
            catalog=self._figure_catalog,
            own_source="self",
            prior_report=self._last_report,
        )


# Budget-free in the lead's cycle loop: the human design channel (§5.5), the two report-writing
# tools (V-4 — refining a report is not evidence-gathering), and the authoring tools that are
# separately bounded by the session's own submit/debate caps.
_CYCLE_FREE_TOOLS: frozenset[str] = frozenset(
    {
        PLAN_AND_DEBATE_TOOL,
        EDIT_REPORT_TOOL,
        REVIEW_REPORT_TOOL,
        SUBMIT_TOOL,
        RESPOND_TO_REVIEW_TOOL,
    }
)


def _cycle_instruction(cycle: LeadCycle, reports: Sequence[Any], rounds_left: int) -> str:
    """The one instruction that opens the cycle: what to settle, and which form settles it.

    Per-form detail lives in the form descriptions the model reads beside the schema, so this
    stays short — it names the stage the cycle opens at and the forms that leave it.
    """
    lines = [
        "This is your turn on the analysts' work. You investigate as long as you need "
        "(read_file, list_dir, web_search, data_catalog) and end the turn by FILING A FORM — "
        "the form you pick is your decision, and its own description says what it needs.",
    ]
    if cycle.stage == DECIDE_STAGE:
        raised = [
            r.tool_request.proposed_tool_name
            for r in reports
            if getattr(r, "tool_request", None) is not None
        ]
        lines.append(
            "A worker reports the toolkit lacks capability the analysis needs "
            f"({', '.join(raised) or 'see the reports'}). Settle that first: read the target "
            "lib/ source, then file author_chain with the ordered chain to write, or "
            "hold_authoring when the request resolves without new code. Judging the analysts "
            "comes after."
        )
    else:
        lines.append(
            "Judge the analysts' work: write your report with edit_report (citing figures by "
            "their [source:index] handle), check it with review_report, and file it with "
            "submit_adjudication — or send the analysts back with request_another_round "
            f"({rounds_left} round(s) still allowed)."
        )
    lines.append(
        "submit_terminal_report is open at every stage: an honest stop is a correct outcome "
        "with no quality penalty. Report real results, or report none — never fabricate a "
        "result to fill a gap."
    )
    return " ".join(lines)


def _stage_note(what_happened: str, cycle: LeadCycle) -> str:
    """A harness-voiced user turn marking a stage boundary, with the next step named.

    Written in the harness's own voice (a ``user`` turn) rather than as an assistant turn: a
    synthesized assistant turn would put words in the model's mouth in the durable record, which
    is the shape rejected on 2026-09-01.
    """
    return (
        f"[{what_happened}] Judge the analysts' work now: write your report with edit_report "
        "(citing figures by their [source:index] handle), check it with review_report, and file "
        "it with submit_adjudication — or send the analysts back with request_another_round while "
        f"the cap allows ({cycle.rounds_left} round(s) left). submit_terminal_report stays open."
    )


def _drop_coding_middle(messages: list, mark: int) -> list:
    """Drop a finished coding session's transcript, keeping its outcome anchor (§ step 6).

    The lead's context otherwise carries a whole ``drive_coder`` ⇄ coder transcript per authored
    tool; in one live authoring phase the provider-native valve cleared ~86,000 tokens on every
    turn. The cut is semantic rather than size-triggered:

        [… author_chain filed]  [drive_coder ⇄ coder × N]  [submit_for_review + its result …]
              kept (before mark)      DROPPED                        kept

    Both survivors are turns the lead actually produced or actually received — its own brief,
    and its own submit call whose result carries the real commit SHA, gate verdict and review
    verdict. Nothing is synthesized in their place.

    Three mechanical conditions, each from a rule already paid for: whole turns only (Anthropic
    documents *echo the assistant message exactly as received*, so filtering inside a turn is
    out); every ``tool_use`` leaves with its ``tool_result`` (a half-dropped pair is a 400); and
    the anchor is real, not written by us. Safe for this role because all of the lead's tools are
    resident, so no dropped turn can de-activate a discovered tool (§13 watch item (a)).

    Returns ``messages`` unchanged when the span holds no submit at all (the lead gave up without
    submitting): there is no anchor to keep, and dropping the span would leave the failure
    unexplained.
    """
    anchor = None
    for index in range(len(messages) - 1, mark - 1, -1):
        message = messages[index]
        calls = getattr(message, "tool_calls", None) or ()
        if any(c.name in (SUBMIT_TOOL, RESPOND_TO_REVIEW_TOOL) for c in calls):
            anchor = index
            break
    if anchor is None:
        return messages
    tail = [messages[anchor]]
    following = messages[anchor + 1] if anchor + 1 < len(messages) else None
    if isinstance(following, ToolResultMessage):
        tail.append(following)
    return messages[:mark] + tail


def _authoring_result(session: Any, spec: Any) -> MergeFlowResult:
    """The terminal ``MergeFlowResult`` for one authoring attempt, read off its session.

    A recorded ``last_commit_sha`` is the only success, and it is set by ``approve_edit`` inside
    ``submit_for_review`` on gate-pass ∧ review-pass — so this reads a fact, never a claim
    (§9.9: commit ⇔ gate ∧ review, and code issues the git).
    """
    spec_id = getattr(session.tool_spec, "id", None) or spec.proposed_tool_name
    if session.last_commit_sha is not None:
        return MergeFlowResult(
            tool_spec_id=spec_id, status="committed", branch=session.branch,
            commit_sha=session.last_commit_sha, gate_report=session.last_gate,
            review=session.last_review, reason="gate-pass and co-lead review-pass",
        )
    if session.last_review is not None and not session.last_review.approved:
        reason = f"co-lead review unresolved: {session.last_review.rationale}"
    elif session.last_gate is not None and not session.last_gate.passed:
        reason = f"gate unresolved: {session.last_gate.reason}"
    else:
        reason = "lead concluded authoring without a mergeable tool"
    return MergeFlowResult(
        tool_spec_id=spec_id, status="failed", branch=session.branch,
        gate_report=session.last_gate, review=session.last_review, reason=reason,
    )


class AnalystLeadAgent(AgentRole):
    """Analyst lead role: adjudicate worker reports into one report."""

    role = "analyst_lead"

    def __init__(
        self,
        *,
        available_toolkits: Sequence[str] = ("research_toolkit.xspec_utils",),
        model: CallableModel | None = None,
        model_group: ModelGroupHandle | None = None,
        model_name: str | None = None,
        prompt: str | None = None,
        monitor: RunMonitor | None = None,
    ):
        super().__init__(
            model=model,
            model_group=model_group,
            model_name=model_name,
            prompt=prompt,
            monitor=monitor,
        )
        self.available_toolkits = tuple(available_toolkits)
        # V-4: stable working-folder key + report dir (set by runtime). The lead's figure catalog is
        # keyed by *worker* source (one list per worker), accumulated append-only across debate rounds
        # AND rework iterations (the lead instance persists), so a figure produced any round stays
        # citable — closing the "last-round-only" leak. Monotonic debate-round counter names the
        # per-round report; last report seeds the next round's review_report(None).
        self.agent_key: str | None = None
        self.report_dir: Path | None = None
        self._figure_catalog: dict[str, list[FigureRef]] = {}
        self._round = 0
        self._last_report: AnalystReport | None = None

    def _make_report_workspace(
        self, source_reports: dict[str, AnalystReport]
    ) -> ReportWorkspace | None:
        """Build this adjudication's report workspace (V-4), or None with no report dir wired.

        The catalog is the lead's per-worker figure catalog (source = worker key); ``source_reports``
        lets ``review_report(worker_key)`` read that worker's finished report. ``own_source='self'``
        (the lead produces no figures) keeps ``review_report(worker_key)`` on the subordinate branch.
        """
        report_dir = self.report_dir
        if report_dir is None:
            return None
        return ReportWorkspace(
            report_path=round_report_path(
                report_dir,
                role=self.role,
                model=_model_label(self.model) or "model",
                round_n=self._round,
            ),
            report_model=AnalystReport,
            catalog=self._figure_catalog,
            own_source="self",
            source_reports=source_reports,
            prior_report=self._last_report,
        )

    async def clarify_for_author(self, message: str, *, kind: str = "clarify_code") -> str:
        """Answer a tool author's clarification message (the lead↔author channel, §9.3).

        The Sonnet author calls this via the ``ask_lead`` in-process tool when it hits a
        code/convention/which-``lib/``-function question, sending a single Markdown ``message``.
        When a read-only ToolProvider is attached (with ``web_search``), the lead may consult
        authoritative documentation before answering rather than guessing.
        """

        payload = {
            "request": "A tool author is asking you a clarification question while writing toolkit code.",
            "kind": kind,
            "message": message,
            "answer_semantics": (
                "Answer concisely and concretely. If you are unsure of an expert science "
                "point, consult authoritative documentation (web_search) before answering; "
                "do not give a confident wrong answer."
            ),
        }
        if self.tools is None:
            reply = await self._call(payload, _ClarifyReply, action="clarify_for_author")
            return reply.answer

        seed = [
            ModelMessage(role="system", content=self.prompt),
            ModelMessage(role="user", content=_payload_text(payload)),
        ]
        messages, _, _ = await self._react(
            seed_messages=seed, tools=self.tools, action="clarify_for_author",
            cap=_react_loop_cap(),
        )
        reply = await self._call_over_messages(
            messages, _ClarifyReply, action="clarify_for_author", tools=self.tools,
        )
        return reply.answer

    # ------------------------------------------------------------------
    # The cycle: ONE ReAct loop, ONE conversation, decisions filed as forms (§9.3).
    # ------------------------------------------------------------------

    def _accumulate_figures(
        self, reports: Sequence[AnalystReport], sources: Sequence[str] | None
    ) -> tuple[list[str], dict[str, AnalystReport], list[FigureRef]]:
        """Fold each worker's cited figures into its catalog source (append-only, deduped).

        The catalog is keyed by *worker*, accumulated across debate rounds AND rework iterations
        (this instance persists), so a figure produced in any round stays citable — that is what
        closed the "last-round-only" leak (V-4). Returns the source keys, the latest report per
        source (for ``review_report(source)``), and every accumulated figure.
        """
        source_keys = (
            list(sources)
            if sources is not None
            else [f"analyst_worker_{i}" for i in range(len(reports))]
        )
        source_reports: dict[str, AnalystReport] = {}
        for key, report in zip(source_keys, reports):
            slot = self._figure_catalog.setdefault(key, [])
            existing = {(str(f.path), f.sha256) for f in slot}
            for fig in report.figures:
                sig = (str(fig.path), fig.sha256)
                if sig not in existing:
                    existing.add(sig)
                    slot.append(fig)
            source_reports[key] = report
        all_figures = [fig for slot in self._figure_catalog.values() for fig in slot]
        return source_keys, source_reports, all_figures

    async def run_cycle(
        self,
        task: AnalysisTask,
        reports: Sequence[AnalystReport],
        *,
        sources: Sequence[str] | None = None,
        rounds_left: int = 0,
        author_budget: int = 0,
        toolkit_root: Path | None = None,
        worktree_base: Path | None = None,
        direct_branch: str | None = None,
        colead: Any | None = None,
        human_handler: Any | None = None,
        coder: Any | None = None,
        allow_correlated_review: bool = False,
    ) -> LeadCycleOutcome:
        """Run one analysis cycle as a single ReAct loop over a single conversation.

        This is the lead's whole turn. Where the role used to be six separate conversations —
        each re-seeded, each ending in a structured compose — it is now one loop in which the
        lead states every decision by **filing a form**: ``author_chain`` or ``hold_authoring``
        while a worker's tool request is open, then ``request_another_round`` or
        ``submit_adjudication``, with ``submit_terminal_report`` reachable throughout. Which form
        it picks *is* the decision, so the branch it takes and the fields it must fill arrive
        together, each carrying its own description.

        The topology is what fixes a real failure. Filing "author" with an empty chain used to be
        schema-valid output that a validator then killed the run over ($19.43, 2026-09-01), and
        the only rule saying otherwise lived where the model could not read it. ``author_chain``
        now requires at least one link in its own schema, so that state cannot be expressed.

        **Python still owns the counting.** ``rounds_left`` bounds the debate, ``author_budget``
        bounds authoring attempts, the session lifecycle and every git operation stay here
        (§9.9), and the caller reloads the registry and reruns the workers. The loop chooses
        *what* to do; this method decides *how many times*.

        **The coding middle is dropped at its own boundary.** After each chain link the
        ``drive_coder`` transcript between the filed brief and the ``submit_for_review`` result is
        removed from the conversation — whole turns only, no orphaned ``tool_use``, and nothing
        synthesized: what survives is the lead's own filed form and its own submit call with the
        real commit SHA on the result. Safe here specifically because the lead's tools are all
        resident (``ReadOnlyToolProvider`` sets ``defer_loading`` on nothing), so no dropped turn
        can de-activate a discovered tool (§13 watch item (a)).

        Returns a :class:`LeadCycleOutcome`; ``filed=None`` means the lead never filed a form,
        which the caller treats as the failure it is.
        """

        self._round += 1
        source_keys, source_reports, all_figures = self._accumulate_figures(reports, sources)
        workspace = self._make_report_workspace(source_reports)
        authoring_possible = (
            toolkit_root is not None
            and author_budget > 0
            and any(r.tool_request is not None for r in reports)
        )
        cycle = LeadCycle(
            stage=DECIDE_STAGE if authoring_possible else ADJUDICATE_STAGE,
            authoring_enabled=authoring_possible,
            rounds_left=rounds_left,
        )
        payload = {
            "task": task.model_dump(mode="json"),
            "worker_reports": [report.model_dump(mode="json") for report in reports],
            "worker_sources": source_keys,
            "available_toolkits": list(self.available_toolkits),
            "your_turn": _cycle_instruction(cycle, reports, rounds_left),
        }
        outcome = LeadCycleOutcome(filed=None, author_budget=author_budget)
        if self.tools is None:
            # No tool surface: there is no form to file and nothing to investigate with. The
            # honest answer is the failure, not a composed report — a runtime-synthesized report
            # sits closer to fabrication than the absence it papers over ([F6]).
            return outcome

        provider = self.tools
        previous_ws = getattr(provider, "report_workspace", None)
        previous_cycle = getattr(provider, "cycle", None)
        provider.report_workspace = workspace
        provider.cycle = cycle
        try:
            messages: list = [
                ModelMessage(role="system", content=self.prompt),
                ModelMessage(role="user", content=_payload_text(payload)),
            ]
            first = True
            while True:
                # The budgets each stage had before the cycle existed, kept: the deciding and
                # judging stages get `react_loop_cap` paid iterations under the absolute ceiling
                # (what `decide_authoring` had), while a coding link gets the absolute cap as its
                # own ceiling (what `drive_authoring` had, so inspecting a diff never starves it).
                # The cycle re-enters per stage, so a stage cannot spend another stage's budget.
                messages, _, _ = await self._react(
                    seed_messages=messages,
                    tools=provider,
                    action="run_cycle",
                    cap=_react_loop_cap(),
                    images=all_figures if first else (),
                    free_tools=_CYCLE_FREE_TOOLS,
                    absolute_cap=_react_loop_absolute_cap(),
                    terminal_tools=LEAD_FORM_TOOLS,
                )
                first = False
                filed = cycle.filed
                if filed is None:
                    # Budget spent, or the model stopped without filing. A report it wrote but
                    # never filed is real evidence it produced, so it is used rather than
                    # discarded; nothing is composed on its behalf.
                    outcome.report = _written_report(workspace)
                    break
                if filed in LEAD_CYCLE_TERMINALS:
                    outcome.filed = filed
                    outcome.report = cycle.report
                    outcome.verdict = cycle.verdict
                    break
                # The authoring branch: act on the filed decision, then re-enter the same
                # conversation for the judging stage.
                outcome.decision = cycle.decision
                cycle.filed = None
                if filed == HOLD_AUTHORING_TOOL:
                    cycle.stage = ADJUDICATE_STAGE
                    messages.append(
                        ModelMessage(
                            role="user",
                            content=_stage_note(
                                "No tool is authored for this request. Judge the analysts' work "
                                "now.",
                                cycle,
                            ),
                        )
                    )
                    continue
                messages = await self._author_chain(
                    messages,
                    provider,
                    cycle,
                    outcome,
                    toolkit_root=toolkit_root,
                    worktree_base=worktree_base,
                    direct_branch=direct_branch,
                    colead=colead,
                    human_handler=human_handler,
                    coder=coder,
                    allow_correlated_review=allow_correlated_review,
                )
                if cycle.filed in LEAD_CYCLE_TERMINALS:
                    # The lead stopped honestly mid-chain (§9.6) — a correct outcome.
                    outcome.filed = cycle.filed
                    outcome.report = cycle.report
                    break
                # The chain is authored, so the cycle ends here: a committed tool is evidence the
                # current reports predate, and the caller reruns the workers on it ONCE before the
                # lead judges a fresh set in a fresh cycle (A8). Authoring inside one cycle,
                # judging in the next — the boundary is new external evidence, not a token count.
                outcome.filed = AUTHOR_CHAIN_TOOL
                break
        finally:
            provider.report_workspace = previous_ws
            provider.cycle = previous_cycle

        if outcome.report is not None:
            self._last_report = outcome.report
        return outcome

    async def _author_chain(
        self,
        messages: list,
        provider: Any,
        cycle: LeadCycle,
        outcome: LeadCycleOutcome,
        *,
        toolkit_root: Path | None,
        worktree_base: Path | None,
        direct_branch: str | None,
        colead: Any | None,
        human_handler: Any | None,
        coder: Any | None,
        allow_correlated_review: bool,
    ) -> list:
        """Author the filed chain link by link, over the lead's own conversation (A8).

        Each link gets its own ``AuthoringSession``, swapped onto the shared provider so the
        declared tool set never changes; authoring stops at the first non-commit, so whatever
        committed is a complete, dependency-satisfied prefix. Every git operation is issued by
        ``build_authoring_session`` / ``approve_edit``, never by the model (§9.9).

        The cycle ends when this returns (the caller reruns the workers on what committed), so
        only the per-link notes are ever read by the model — a note appended on the way out would
        be written into a conversation nobody sees again. Returns the conversation with each
        finished link's coding middle dropped.
        """
        from auto_research.sandbox.merge_flow import ToolkitBranchUnclean

        specs = list(outcome.decision.tool_specs) if outcome.decision is not None else []
        for position, spec in enumerate(specs, 1):
            if outcome.author_budget <= 0:
                break  # credits spent — the caller reruns the workers on whatever committed
            outcome.author_budget -= 1
            cycle.stage = AUTHOR_STAGE
            messages.append(
                ModelMessage(
                    role="user",
                    content=(
                        f"Coding session {position} of {len(specs)} is open on "
                        f"'{spec.proposed_tool_name}'. The coder has your brief and has produced a "
                        "first attempt. Inspect it with read_file, steer it with drive_coder, and "
                        "finish it with submit_for_review. submit_terminal_report stays open if "
                        "this genuinely cannot be made mergeable — an honest stop is correct."
                    ),
                )
            )
            mark = len(messages)
            try:
                result = await self._drive_one_link(
                    messages,
                    provider,
                    spec,
                    toolkit_root=toolkit_root,
                    worktree_base=worktree_base,
                    direct_branch=direct_branch,
                    colead=colead,
                    human_handler=human_handler,
                    coder=coder,
                    allow_correlated_review=allow_correlated_review,
                )
            except ToolkitBranchUnclean as exc:
                # The authoring branch was dirty when this link began — a tree dirtied mid-run
                # (across-a-death is repaired by the resume-time heal). Degrade to an honest
                # failed result rather than crashing the run (an F6-class run-killer); the lead
                # reports it and the reviewer adjudicates downstream (A11).
                await self._record(
                    "analyst.tool.branch_unclean",
                    level=2,
                    summary=(
                        f"Authoring branch unclean — link stopped ({spec.proposed_tool_name})"
                    ),
                    data={"error": repr(exc)},
                )
                result = MergeFlowResult(
                    tool_spec_id=getattr(spec, "id", None) or spec.proposed_tool_name,
                    status="failed",
                    reason=f"authoring branch was not clean at attempt start: {exc}",
                )
            outcome.authored.append(result)
            if result.status == "committed":
                outcome.committed += 1
            await self._record(
                "analyst.tool.authored",
                level=5,
                summary=f"Tool authoring {result.status}: {spec.proposed_tool_name}",
                data={"result": result.model_dump(mode="json")},
            )
            if cycle.filed in LEAD_CYCLE_TERMINALS:
                # An honest terminal filed mid-session (§9.6). Its own turn lives inside the span
                # the drop would remove, and the cycle ends here anyway — so the conversation is
                # returned whole rather than cut.
                return messages
            messages = _drop_coding_middle(messages, mark)
            if result.status != "committed":
                # A non-committed link does NOT end the chain (F9). It used to `break`, on the
                # reasoning that a chain is ordered so the committed prefix is what later links
                # depend on. True, and it made a single failure end the whole chain with the
                # credits that would have paid for a retry still unspent: slot 3 (2026-09-08)
                # spent 2h43m and ~$18 on six cycles refused before a coder session even opened,
                # and slot 4 queued ~8 more of the same. Human ruling: the LEAD owns the stop
                # decision, and spending the whole authoring budget discovering a capability
                # cannot be built is an honest answer.
                #
                # So the loop continues, and the lead is told — in its own conversation, before
                # the next brief — what failed and why. That is what makes "the lead decides"
                # real: it can file `hold_authoring` or a terminal on the next turn, and it can
                # only do that over a reason it can read.
                messages.append(ModelMessage(role="user", content=(
                    f"Link '{spec.proposed_tool_name}' did not commit: {result.reason}\n"
                    "The chain continues to the next link. If a later link depends on what this "
                    "one would have built, or the reason above will recur, stop the chain by "
                    "filing hold_authoring or a terminal report instead of authoring into it."
                )))
        return messages

    async def _drive_one_link(
        self,
        messages: list,
        provider: Any,
        spec: ToolSpec,
        *,
        toolkit_root: Path | None,
        worktree_base: Path | None,
        direct_branch: str | None,
        colead: Any | None,
        human_handler: Any | None,
        coder: Any | None,
        allow_correlated_review: bool,
    ) -> MergeFlowResult:
        """Own one link's session lifecycle and run the lead's loop over it, in-conversation.

        The same lifecycle :meth:`drive_authoring` owns for a standalone attempt — build,
        connect, publish for interrupt cleanup, seed the coder, drive, read the terminal result,
        ``aclose`` — with the loop running over ``messages`` (the cycle's one conversation) and
        the session swapped onto the shared ``provider`` rather than a fresh one.
        """
        from auto_research.sandbox.merge_flow import build_authoring_session

        session = build_authoring_session(
            spec,
            lead=self,
            toolkit_root=toolkit_root,
            worktree_base=worktree_base or toolkit_root,
            colead=colead,
            human_handler=human_handler,
            coder=coder,
            allow_correlated_review=allow_correlated_review,
            direct_branch=direct_branch,
            monitor=self.monitor,
        )
        await session.connect()
        if self.run_control is not None:
            self.run_control.authoring_session = session
        previous_session = getattr(provider, "authoring_session", None)
        provider.authoring_session = session
        try:
            await session.drive(session.initial_message)
            new_messages, _, _ = await self._react(
                seed_messages=messages,
                tools=provider,
                action="drive_authoring",
                cap=_react_loop_absolute_cap(),
                free_tools=_CYCLE_FREE_TOOLS,
                absolute_cap=_react_loop_absolute_cap(),
                terminal_tools=LEAD_FORM_TOOLS,
            )
            messages[:] = new_messages
            return _authoring_result(session, spec)
        finally:
            provider.authoring_session = previous_session
            if self.run_control is not None and self.run_control.authoring_session is session:
                self.run_control.authoring_session = None
            await session.aclose()

    async def drive_authoring(
        self,
        tool_spec: ToolSpec,
        *,
        toolkit_root: Path,
        worktree_base: Path,
        direct_branch: str | None = None,
        colead: Any | None = None,
        human_handler: Any | None = None,
        coder: Any | None = None,
        allow_correlated_review: bool = False,
    ) -> MergeFlowResult:
        """Drive one tool through authoring as the plan owner (§9.3 B-topology, A1).

        Builds + connects an ``AuthoringSession`` for ``tool_spec``, then runs the lead's ReAct
        loop over it: the lead steers the Sonnet coder (``drive_coder``) and submits to the
        deterministic gate + GPT co-lead review (``submit_for_review``); the coder is seeded with
        the brief first. The commit happens inside ``submit`` on gate-pass ∧ review-pass (the lead
        never self-approves). Returns the terminal ``MergeFlowResult`` (committed → the strategy
        resumes; failed → it finalizes) read off the session. Owns the session lifecycle
        (``aclose`` in ``finally``); the LLM never issues git — ``build_authoring_session`` /
        ``approve_edit`` do (§9.9).

        ``coder`` overrides the author member (``None`` = the configured one, every production
        caller) and ``allow_correlated_review`` permits a same-provider co-lead; both exist so a
        benchmark can sweep the author and the reviewer with the rest of the loop byte-identical,
        and both are documented on ``merge_flow.build_authoring_session``.
        """
        from auto_research.sandbox.merge_flow import build_authoring_session

        session = build_authoring_session(
            tool_spec,
            lead=self,
            toolkit_root=toolkit_root,
            worktree_base=worktree_base,
            colead=colead,
            human_handler=human_handler,
            coder=coder,
            allow_correlated_review=allow_correlated_review,
            direct_branch=direct_branch,
            monitor=self.monitor,
        )
        await session.connect()
        # Publish the live session so a human interrupt can close it: this `finally` sits inside
        # the cancelled scope, where its awaits die before the branch reset runs, so the interrupt
        # path calls `aclose()` itself from shielded cleanup. Whichever gets there first clears the
        # handle, and `aclose` is idempotent.
        if self.run_control is not None:
            self.run_control.authoring_session = session
        try:
            return await self._drive_authoring_loop(session, tool_spec, human_handler)
        finally:
            if self.run_control is not None and self.run_control.authoring_session is session:
                self.run_control.authoring_session = None
            await session.aclose()

    async def _drive_authoring_loop(
        self, session: Any, tool_spec: ToolSpec, human_handler: Any | None
    ) -> MergeFlowResult:
        """The lead's ReAct loop over a connected session (split out so the lifecycle stays in a
        clean ``try/finally`` in :meth:`drive_authoring`)."""
        base = self.tools
        roots = list(getattr(base, "roots", []) or []) + [session.worktree]
        tools = ReadOnlyToolProvider(
            roots=roots,
            retrieval=getattr(base, "retrieval", None),
            web_source=getattr(base, "web_source", None),
            include_text_search=False,  # authoring steers code, not the papers KB
            include_plan_and_debate=human_handler is not None,
            authoring_session=session,
            human_handler=human_handler,
            asked_by="analyst_lead",
            model=getattr(base, "model", "") or "",
        )

        # Seed the coder with the brief settled in decide_authoring, then steer it.
        await session.drive(session.initial_message)

        payload = {
            "request": (
                "You are authoring the tool below into the toolkit. You are the plan owner driving "
                "a Sonnet coder — it executes, you decide. It has been given the brief and produced "
                "a first attempt."
            ),
            "tool_spec": session.tool_spec.model_dump(mode="json"),
            "loop_semantics": (
                "Inspect the coder's work with read_file/list_dir. Steer it with drive_coder, "
                "giving concrete algorithm-level instructions (signature, exact procedure, edge "
                "cases, the tests it must add). When you believe the tool and its tests are right, "
                "call submit_for_review: it runs the deterministic gate, then the independent GPT "
                "co-lead review, and commits automatically on gate-pass AND review-pass — you never "
                "self-approve. On a gate failure (mechanical), fix the underlying cause via "
                "drive_coder; never weaken a check to pass. On a review rejection, either steer the "
                "coder to address it (drive_coder) or, when you believe a challenge is unfounded or "
                "already handled, defend it with respond_to_review — the co-lead reconsiders and may "
                "concede to a sound argument (it commits if persuaded and the gate still passes). "
                "When the tool commits you are done; if it cannot be made mergeable within the "
                "budget, conclude honestly — an honest failure is a correct outcome, never fabricate "
                "a result to force a pass."
            ),
        }
        seed = [
            ModelMessage(role="system", content=self.prompt),
            ModelMessage(role="user", content=_payload_text(payload)),
        ]
        # Drive the lead's ReAct loop. The loop is drive → inspect (read_file) → submit, many
        # times: use the absolute cap as the iteration ceiling so inspecting the coder's diffs
        # never starves it. submit_for_review (expensive) is separately bounded by
        # session.submit_cap; the human design channel is budget-free; both stay out of the budget.
        #
        # Authoring finalizes ONLY through submit_for_review (commit ⇔ gate ∧ review). Its tool
        # description states this as the sole finalization path — stopping without it discards the
        # work — so a bare _react stop (no tool call) is a deliberate, honest give-up, not a missed
        # submit. (This replaced an earlier code-level nudge loop, dropped per CLAUDE.md §1 once a
        # live run showed the lead submits on its own and the backstop never fired.)
        free = frozenset({SUBMIT_TOOL, RESPOND_TO_REVIEW_TOOL, PLAN_AND_DEBATE_TOOL})
        await self._react(
            seed_messages=seed, tools=tools, action="drive_authoring",
            cap=_react_loop_absolute_cap(), free_tools=free,
        )

        return _authoring_result(session, tool_spec)

class StatusMonitorAgent(AgentRole):
    """The `/status` reporter: reads the run's own ledger and answers the human's question.

    A reporter, never a participant — it holds no run state, produces no artifact, and can only
    read. Its whole surface is :class:`MonitorToolProvider`, so "read-only" is a property of the
    tools rather than a promise in the prompt.

    It investigates rather than being handed a digest. The previous design flattened the last 80
    events into one line each and dropped every payload, which meant a question like "what did
    the reviewer decide?" could see *that* a verdict happened and never what it said — and, since
    the window was the tail at any level, it was mostly chatter (44 L7 + 18 L10 against 6
    significant events on a live run). Now it searches the index, narrows, and opens what it
    found, bounded by a deliberately tight loop cap because a human is waiting on a phone.
    """

    role = "status_monitor"

    async def report(self, question: str, *, jsonl_path: Path | None) -> str:
        """Answer ``question`` (or give a general status) from the run's event log."""
        self.tools = MonitorToolProvider(jsonl_path)
        ask = question.strip() or "Give a general status report on this run."
        seed = [
            ModelMessage(role="system", content=self.prompt),
            ModelMessage(role="user", content=f"Question: {ask}"),
        ]
        messages, _, _ = await self._react(
            seed_messages=seed, tools=self.tools, action="status",
            cap=_status_react_loop_cap(),
        )
        answer = ((self.last_response.content if self.last_response else "") or "").strip()
        if answer:
            return answer
        # The loop ended on its cap with the model still calling tools, so its last turn carried
        # no prose. Ask once more with no tools attached: it has read enough, and a human waiting
        # on an answer should not get silence because the investigation ran long.
        response = await self.model.call(
            [*messages, ModelMessage(
                role="user",
                content="Stop searching and answer now, from what you have already read.",
            )],
            # Declared-but-forbidden, not omitted: this replays a tool trajectory, and a
            # request that drops the declarations orphans the items referencing them.
            tools=self.tools.tool_definitions(),
            tool_choice="none",
        )
        return (response.content or "").strip() or "(no answer produced)"


class AnalystColeadAgent(AgentRole):
    """Analyst co-lead role: provider-decorrelated code review of an authored tool (§9.3).

    The §9.3 analyst lead is a *group*: the Claude science lead (adjudication, library
    discipline) and this GPT co-lead, whose Phase-2 job is **tool-code review only** —
    never the science finding, never a merge. It is the *second, independent* check on a
    merge candidate: the deterministic gate is the door, this review is the pre-filter the
    door also requires (commit ⇔ gate-pass ∧ co-lead review-pass). Routed to a model whose
    provider differs from the Claude author's, so a tool is not approved by its own model
    family (see ``review.select_decorrelated_member``).
    """

    role = "analyst_colead"

    async def review_tool(
        self,
        tool_spec: ToolSpec,
        *,
        diff: str,
        gate: GateReport,
        tools: Any | None = None,
        prior_review: ToolReviewVerdict | None = None,
        lead_rebuttal: str | None = None,
    ) -> ToolReviewVerdict:
        """Review an authored tool's diff against its brief and the gate report.

        ``diff`` is the bundle of new/changed ``*_utils`` source the author wrote;
        ``gate`` is the (already-passed) deterministic report — its ``findings`` are the
        literal-array intent calls the co-lead adjudicates (§9.4). When a read-only
        ``ToolProvider`` (the worktree + web) is supplied, the co-lead first investigates
        the surrounding code/conventions, then issues the verdict grounded in that
        investigation; otherwise it makes a plain structured call.

        ``lead_rebuttal`` (+ ``prior_review``) carries the analyst lead's response to a prior
        rejection (the §9.3 B-topology debate). The co-lead **reconsiders** in light of it and
        re-issues a verdict — conceding only when genuinely persuaded that a flagged issue was
        wrong or already addressed, and holding firm (with the same or sharpened issues) when the
        rebuttal does not actually resolve the substance. It is the *trust boundary*: the lead may
        persuade with a sound argument, never pressure the reviewer into approving.
        """
        payload = {
            "tool_spec": tool_spec.model_dump(mode="json"),
            "diff": diff,
            "gate_report": {
                "passed": gate.passed,
                "layer_a": [c.model_dump(mode="json") for c in gate.layer_a],
                "layer_b": [c.model_dump(mode="json") for c in gate.layer_b],
                "findings": [c.model_dump(mode="json") for c in gate.findings],
            },
            "review_semantics": (
                "Code review only — judge the tool code and its committed Layer-B tests, "
                "not the research finding. approved=true only if the code matches the brief, "
                "the tests are the right ones and strict enough, there is no fabrication, and "
                "no blocking bug remains. Otherwise approved=false with specific, actionable "
                "issues; set checks_adequate=false when the weakness is in the verification."
            ),
        }
        if lead_rebuttal is not None:
            payload["prior_review"] = (
                prior_review.model_dump(mode="json") if prior_review is not None else None
            )
            payload["lead_rebuttal"] = lead_rebuttal
            payload["reconsider_semantics"] = (
                "The analyst lead is responding to your prior rejection (above). Reconsider each "
                "of your issues against the rebuttal. Concede (drop an issue, or set approved=true) "
                "ONLY when the rebuttal genuinely shows the issue was wrong or is already handled in "
                "the code/tests — cite where. Hold firm (keep approved=false, keep or sharpen the "
                "issue) when the rebuttal argues around the substance, appeals to deadlines/effort, "
                "or asks you to relax a check without fixing the underlying gap. A sound argument may "
                "move you; pressure must not. Never approve fabrication or an inadequate test."
            )
        if tools is None:
            return await self._call(payload, ToolReviewVerdict, action="review_tool")

        seed = [
            ModelMessage(role="system", content=self.prompt),
            ModelMessage(role="user", content=_payload_text(payload)),
        ]
        messages, _, _ = await self._react(
            seed_messages=seed, tools=tools, action="review_tool", cap=_react_loop_cap(),
        )
        return await self._call_over_messages(
            messages, ToolReviewVerdict, action="review_tool", tools=tools,
        )


class _ClarifyReply(BaseModel):
    """Minimal structured reply for ``AnalystLeadAgent.clarify_for_author``.

    A lightweight internal helper, not a broad protocol message; the §9.7
    ``HumanQuery``/``HumanResponse`` schema is formalised in Phase 2 step 5.
    """

    answer: str


def _select_model(group: ModelGroupHandle, model_name: str | None) -> ModelHandle:
    if model_name is not None:
        return group.member(model_name)
    return group.handles[0]


def _model_label(model: CallableModel) -> str | None:
    config = getattr(model, "config", None)
    label = getattr(config, "model", None)
    if isinstance(label, str) and label:
        return label
    return None


def _dedup_results(results: Sequence[SearchResult]) -> list[SearchResult]:
    """Deduplicate search results by URL (falling back to title), first-seen order."""
    seen: set[str] = set()
    out: list[SearchResult] = []
    for result in results:
        key = str(result.url or result.title)
        if key not in seen:
            seen.add(key)
            out.append(result)
    return out


#: How many consecutive silent turns one loop will nudge through before giving up. Small on
#: purpose: a continuation is for a model that stopped by accident, and a model that keeps
#: returning nothing is not going to start. The loop's own budget still bounds everything else.
_SILENT_TURN_CAP = 2


def _continuation_for(response, terminal_tools: frozenset[str]) -> str | None:
    """What to say to a turn that carried no tool calls, or ``None`` to end the loop (F5b).

    Keyed on the normalised stop class, so each case gets the reply its vendor documents:

    - ``end_turn`` (and ``unknown``, judged identically) with (almost) nothing generated is the
      documented empty response. It is nudged, because the model did not mean to stop; a turn
      carrying a real answer is a genuine finish and ends the loop.
    - ``truncated`` ran out of output budget mid-answer, so it is told to finish more compactly
      rather than being read as done.
    - ``paused`` is a server-tool loop at its iteration limit, which the provider documents as
      "send the assistant content back to continue".
    - ``malformed`` is the vendor rejecting its own tool call; re-issuing it is the fix.
    - ``refusal`` and ``transient`` are **not** nudged: a refusal needs a different member and a
      transient needs the retry layer, and repeating the request achieves neither.
    """
    # No terminal tool means there is no other way for this loop to finish, so the absence of
    # tool calls IS its completion signal and nudging would be a change with nothing to gain.
    # Every role F5 is about (worker, lead, reviewer) declares one.
    if not terminal_tools:
        return None
    stop = getattr(response, "stop", "unknown")
    if stop in {"refusal", "transient"}:
        return None
    finish = f"Call `{sorted(terminal_tools)[0]}` when your work is complete."
    if stop == "truncated":
        return (
            "Your previous turn was cut off before it finished (the output budget ran out). "
            f"Continue from where it stopped, more compactly. {finish}"
        )
    if stop == "malformed":
        return (
            "Your previous turn contained a tool call the provider could not parse. Re-issue it "
            f"with arguments matching the tool's schema. {finish}"
        )
    if stop == "paused":
        return f"Continue from where you paused. {finish}"
    # `end_turn` and `unknown` are judged the same way, on the turn itself rather than on the
    # label: the reason to continue is that the turn looks accidental — no tool call and nothing
    # produced. A turn carrying a real answer answered, whether or not the vendor said so.
    if len((response.content or "").strip()) > 40:
        return None
    return (
        "Your previous turn was empty — no tool call and no answer. Proceed with the next step "
        f"using your best judgement. {finish}"
    )


@lru_cache(maxsize=1)
def _react_loop_cap() -> int:
    """Max tool iterations per agent ReAct loop (orchestration.yaml).

    Key: ``react_loop_cap``. Defaults to 12. The inner-loop backstop — distinct
    from ``debate_max_rounds`` (outer debate) and ``structured_retry_cap``.
    """
    return int(_orchestration_config().get("react_loop_cap", 12))


def _librarian_react_loop_cap() -> int:
    """Max tool iterations for the librarian literature ReAct loop.

    Key: ``librarian_react_loop_cap``. Defaults to 3 (falling back to
    ``react_loop_cap``). Tighter than the generic cap because each librarian
    search re-sends the growing 20-paper abstract context, so unbounded query
    refinement is the dominant token-cost driver of a run.
    """
    cfg = _orchestration_config()
    return int(cfg.get("librarian_react_loop_cap", cfg.get("react_loop_cap", 12)))


def _status_react_loop_cap() -> int:
    """Max tool iterations for the `/status` reporter (orchestration.yaml).

    Key: ``status_react_loop_cap``. Defaults to 6 — deliberately tight. A human is waiting on a
    phone for this answer, and the ledger it reads grows to thousands of events, so an
    open-ended investigation would be both slow to arrive and expensive to produce. Six turns is
    enough for the intended shape: look at the index, narrow once, open one or two events.
    """
    return int(_orchestration_config().get("status_react_loop_cap", 6))


def _react_loop_absolute_cap() -> int:
    """Hard ceiling on TOTAL ReAct iterations, incl. budget-free ones (orchestration.yaml).

    Key: ``react_loop_absolute_cap``. Defaults to 60. ``react_loop_cap`` bounds the
    budget-consuming iterations; budget-free tools (``plan_and_debate``, §5.5) are uncounted
    against that cap, so this larger ceiling backstops a runaway free-tool loop (the human
    ending the design debate is the normal stop). Only the analyst lead's design debate opts
    in; every other ReAct loop is bound by ``react_loop_cap`` alone.
    """
    return int(_orchestration_config().get("react_loop_absolute_cap", 60))


@lru_cache(maxsize=4)
def _load_orchestration(config_path: Path) -> dict:
    import yaml

    try:
        with config_path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _orchestration_config() -> dict:
    """Load and cache the configured ``orchestration.yaml`` (best-effort).

    Cached **on the resolved path** rather than on nothing: an override is a per-process decision
    set before the process starts, so in a real run the key never changes, but keying on it means a
    test that points the process elsewhere is not served the previous directory's answer.
    """
    return _load_orchestration(orchestration_yaml())


@lru_cache(maxsize=1)
def _structured_retry_cap() -> int:
    """Max attempts for a structured ``_call`` before raising (orchestration.yaml).

    Key: ``structured_retry_cap``. Defaults to 2 in code, **4 in config since F5g**: one
    adapter-shape failure used to leave a single attempt for any genuine schema error. Slot 5 hit
    exactly that — attempt 1 was consumed by the dashscope `tool_choice` inversion raising on a
    working tool call, so the placeholder-digest error had one attempt and the run died. Mirrors
    ``tool_retry_cap`` for
    the tool-call loop — a small bound so a transient off-schema result doesn't
    end a run, while cap exhaustion still fails loudly.
    """
    return int(_orchestration_config().get("structured_retry_cap", 2))


def _payload_text(payload: Mapping[str, Any]) -> str:
    """Serialize task data for the user message.

    The output schema is enforced by the provider (native structured output /
    function calling), so it is no longer injected into the prompt — only the
    task data is sent.
    """
    return json.dumps(payload, sort_keys=True)


def _team_for_role(role: PromptRole) -> str:
    if role == "reviewer":
        return "reviewer"
    if role == "librarian_io":
        return "librarian"
    return "analyst"


def _default_sources() -> dict[str, QuerySource]:
    return {
        "arxiv": ArxivSource(),
        "nasa_ads": NasaAdsSource(),
        "web": WebSource(),
    }


# Keys that belong to the transport protocol — not serialized as tool payload.
# Per-field character caps written to memory.md (crash-prevention).
_STDOUT_MEM_CAP = 8_000
_STDERR_MEM_CAP = 2_000

# Per-field caps for tool-call args/results recorded into the monitor JSONL (full-fidelity
# diagnosis without unbounded log growth — see monitor/DOCS.md). Read from config at call time
# (``ledger_arg_cap`` / ``ledger_result_cap``) rather than fixed here, because the ledger is the
# run's durable record and an offline scoring pass reads it long after: the value a batch used
# has to travel with that batch's config, not with whatever the source said at the time.
def _event_arg_cap() -> int:
    return ledger_caps().arg


def _event_result_cap() -> int:
    return ledger_caps().result


def _cap_text(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` chars with a count marker (for monitor event data)."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [+{len(text) - limit:,} chars]"


def _append_tool_results_to_memory(path: Path, results: Sequence[ToolResult]) -> None:
    """Append bare terminal recordings of tool calls to the worker's memory file.

    stdout and stderr are capped at write time so memory.md stays bounded.
    """
    import datetime
    lines: list[str] = [f"\n## Tool calls — {datetime.datetime.now(datetime.UTC).isoformat(timespec='seconds')}"]
    for r in results:
        status = "OK" if r.exit_code == 0 else f"FAILED (exit {r.exit_code})"
        lines.append(f"\n### {r.tool_name}  [{status}]  ({r.elapsed_s:.1f}s)")
        if r.stdout:
            text = r.stdout
            if len(text) > _STDOUT_MEM_CAP:
                text = text[:_STDOUT_MEM_CAP] + f"\n[stdout truncated — {len(r.stdout):,} chars total]"
            lines.append(f"stdout:\n{text}")
        if r.stderr:
            text = r.stderr
            if len(text) > _STDERR_MEM_CAP:
                text = text[:_STDERR_MEM_CAP] + f"\n[stderr truncated — {len(r.stderr):,} chars total]"
            lines.append(f"stderr:\n{text}")
        if r.figures:
            lines.append(f"figures: {[str(f.path) for f in r.figures]}")
    append_memory(path, "\n".join(lines) + "\n")
