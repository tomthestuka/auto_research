"""Async role wrappers for the Phase 1 agent team."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from auto_research.agents.prompts import PromptRole, load_prompt
from auto_research.agents.react import (
    DRIVE_CODER_TOOL,
    EDIT_REPORT_TOOL,
    MonitorToolProvider,
    PLAN_AND_DEBATE_TOOL,
    REVIEW_REPORT_TOOL,
    RESPOND_TO_REVIEW_TOOL,
    ReadOnlyToolProvider,
    ReportWorkspace,
    SUBMIT_TOOL,
    SandboxToolProvider,
    SourceToolProvider,
)
from auto_research.consensus import IODebateToConverge
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
        budget_cap = max(1, cap)
        absolute_ceiling = max(budget_cap, absolute_cap) if absolute_cap is not None else budget_cap
        budget_used = 0
        iteration = 0
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
                        data={"message": _cap_text(steer, _EVENT_ARG_CAP)},
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
                break
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
                            _EVENT_ARG_CAP,
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
                        "text": _cap_text(outcome.text, _EVENT_RESULT_CAP),
                    },
                )
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
                    data={"message": _cap_text(steer, _EVENT_ARG_CAP)},
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

    async def compose_latex_report(
        self,
        request: ResearchRequest,
        report: AnalystReport,
        staged_figures: "list[tuple]",
        staged_papers: "list[tuple]",
        verdict: ReviewVerdict,
        *,
        feedback: list[str] | None = None,
    ) -> str:
        """Compose a LaTeX research report (human handle).

        Returns the raw LaTeX string. ``staged_figures`` is a list of
        (FigureRef, relpath) pairs used verbatim in \\includegraphics{}.
        ``staged_papers`` is a list of (path, relpath) pairs for the cited PDFs; the
        References section is a list of \\href links to those relpaths (no BibTeX).
        ``feedback`` carries compile/link errors from the prior attempt for
        self-correction.
        """
        from auto_research.schemas.report import LatexReport

        fig_descriptions = [
            {"relpath": relpath, "caption": figref.caption, "sha256": figref.sha256}
            for figref, relpath in staged_figures
        ]
        paper_links = [relpath for _, relpath in staged_papers]
        payload = {
            "output_format": "tex",
            "output_format_instructions": (
                "You are composing a LaTeX research report. Provide the complete LaTeX "
                "document as the 'latex' field, written as ordinary LaTeX source "
                "(e.g. \\section{}, \\includegraphics{}). Do NOT add any JSON or backslash "
                "escaping — the response encoding is handled for you, so a single backslash "
                "is a single backslash. "
                "Load graphicx, amsmath, amssymb and hyperref in the preamble, plus any "
                "further package a symbol you use requires — amssymb carries the relation "
                "symbols science prose reaches for (\\lesssim, \\gtrsim, \\simeq), and a "
                "symbol whose package is absent is a fatal compile error that costs the "
                "whole PDF. Use \\includegraphics{<relpath>} "
                "verbatim for each figure relpath supplied below. "
                "Cite papers in prose in the body. The References section is a list of "
                "hyperlinks to the local PDFs you used: for each supplied paper relpath emit "
                "\\href{<relpath>}{<relpath>} using the relpath verbatim. Use ONLY the supplied "
                "paper relpaths; add no \\cite, no \\begin{thebibliography}, and no invented "
                "author/title text."
            ),
            "request": request.model_dump(mode="json"),
            "analysis_report": report.model_dump(mode="json"),
            "verdict_rationale": verdict.rationale,
            "figures": fig_descriptions,
            "paper_links": paper_links,
        }
        if feedback:
            payload["feedback"] = (
                "The previous attempt had these errors — fix them exactly:\n"
                + "\n".join(f"  - {e}" for e in feedback)
            )
        result: LatexReport = await self._call(
            payload,
            LatexReport,
            images=report.figures,
            action="compose_latex_report",
        )
        return result.latex


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
            free_tools=frozenset({EDIT_REPORT_TOOL, REVIEW_REPORT_TOOL}),
            absolute_cap=_react_loop_absolute_cap(),
        )
        self.last_tool_results = provider.results
        if self.memory_path and provider.results:
            _append_tool_results_to_memory(self.memory_path, provider.results)

        # V-4: the report is what the worker persisted via edit_report this round (figures cited by
        # index from its catalog, resolved by the runtime). Safety net: a loop that ended without an
        # edit_report call gets one terminal structured compose, with its catalog figures attached.
        report = workspace.written_report if workspace is not None else None
        if report is None:
            report = await self._call_over_messages(
                messages, AnalystReport, action="analyze", tools=provider,
            )
            if self._figure_catalog["self"]:
                report = report.model_copy(
                    update={"figures": _dedup_figures(self._figure_catalog["self"]), "cited_figures": []}
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

    async def adjudicate(
        self,
        task: AnalysisTask,
        reports: Sequence[AnalystReport],
        *,
        sources: Sequence[str] | None = None,
    ) -> AnalystReport:
        """Synthesize a final analysis report from worker reports (V-4).

        Each worker's cited figures are accumulated into a per-worker catalog source (append-only,
        across rounds/iterations), so the lead can cite any worker's figure from any round. With a
        read-only ToolProvider attached, the lead ReAct-refines the report through edit_report /
        review_report (citing figures by ``{worker, index}``); the report it persists is returned.
        Otherwise it makes a plain structured call and attaches the accumulated figures directly.
        """

        self._round += 1
        source_keys = list(sources) if sources is not None else [
            f"analyst_worker_{i}" for i in range(len(reports))
        ]
        # Accumulate each worker's cited figures into its catalog source (append-only, deduped).
        source_reports: dict[str, AnalystReport] = {}
        for key, report in zip(source_keys, reports):
            slot = self._figure_catalog.setdefault(key, [])
            existing = {(str(f.path), f.sha256) for f in slot}
            for fig in report.figures:
                sig = (str(fig.path), fig.sha256)
                if sig not in existing:
                    existing.add(sig)
                    slot.append(fig)
            source_reports[key] = report  # latest report per source (for review_report(source))

        payload = {
            "task": task.model_dump(mode="json"),
            "worker_reports": [report.model_dump(mode="json") for report in reports],
            "worker_sources": source_keys,
            "available_toolkits": list(self.available_toolkits),
        }
        all_figures = [fig for slot in self._figure_catalog.values() for fig in slot]
        if self.tools is None:
            report = await self._call(payload, AnalystReport, images=all_figures, action="adjudicate")
            return report.model_copy(
                update={"figures": _dedup_figures(all_figures), "cited_figures": []}
            )

        workspace = self._make_report_workspace(source_reports)
        seed = [
            ModelMessage(role="system", content=self.prompt),
            ModelMessage(role="user", content=_payload_text(payload)),
        ]
        # Wire the per-round report workspace onto the shared provider ONLY for this adjudication
        # (cleared in finally, so decide_authoring/drive_authoring never see edit_report/review_report).
        previous_ws = getattr(self.tools, "report_workspace", None)
        try:
            if workspace is not None:
                self.tools.report_workspace = workspace
            messages, _, _ = await self._react(
                seed_messages=seed, tools=self.tools, action="adjudicate",
                cap=_react_loop_cap(),
                free_tools=frozenset({EDIT_REPORT_TOOL, REVIEW_REPORT_TOOL}),
                absolute_cap=_react_loop_absolute_cap(),
            )
            report = workspace.written_report if workspace is not None else None
            if report is None:
                # Safety net: no edit_report written — compose terminally, attaching the catalog
                # figures. This sits INSIDE the try on purpose: the compose declares
                # `self.tools`, and the `finally` below detaches the workspace, which would
                # remove edit_report/review_report from the declared set while the trajectory
                # being replayed can still name them — the "Tool reference 'x' not found in
                # available tools" shape (run-killer 2026-07-29). The declarations must match
                # the history they replay, so the compose runs while the loop's set is attached.
                report = await self._call_over_messages(
                    messages, AnalystReport, action="adjudicate", images=all_figures,
                    tools=self.tools,
                )
                report = report.model_copy(
                    update={"figures": _dedup_figures(all_figures), "cited_figures": []}
                )
        finally:
            if hasattr(self.tools, "report_workspace"):
                self.tools.report_workspace = previous_ws

        self._last_report = report
        return report

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

    async def judge_convergence(
        self,
        task: AnalysisTask,
        reports: Sequence[AnalystReport],
        *,
        round_index: int,
        max_rounds: int,
    ) -> ReviewVerdict:
        """Judge whether worker reports have converged."""

        payload = {
            "task": task.model_dump(mode="json"),
            "worker_reports": [report.model_dump(mode="json") for report in reports],
            "round_index": round_index,
            "max_rounds": max_rounds,
            "decision_semantics": (
                "accepted=true means workers have converged enough for final synthesis; "
                "accepted=false means another debate round is useful if the cap allows it."
            ),
        }
        figures = [figure for report in reports for figure in report.figures]
        return await self._call(
            payload,
            ReviewVerdict,
            images=figures,
            action="judge_convergence",
        )

    async def decide_authoring(
        self, reports: Sequence[AnalystReport]
    ) -> AuthoringDecision:
        """Decide whether to author a tool now for the workers' ``tool_request``s (§9.3, §9.10).

        Called by ``DebateToConverge`` when ≥1 worker report carries a ``tool_request``. The
        lead browses the **full** ``lib/`` source read-only (its ``ReadOnlyToolProvider`` is
        rooted at ``toolkit_root``) to set the architectural disposition — *capability exists
        but is unexposed* (``extend_existing``) vs *genuinely absent* (``new_package``) — and
        to refine the workers' advisory specs. It returns the **ordered dependency chain**
        (``tool_specs``: acquire → reduce → analyse → plot) the analysis needs — planned up front
        from the full prescription, not just the first gap a worker surfaced — or
        ``should_author=false`` when the "gap" was really a mis-called existing tool. Consensus —
        not this method — authors the chain sequentially and reruns workers once (A8); it issues
        the ``merge_flow.propose_and_pr`` calls (§9.9: code issues git, never the model).
        """
        requests = [r.tool_request for r in reports if r.tool_request is not None]
        payload = {
            "request": (
                "Analyst workers report the toolkit lacks capability the analysis needs. Decide "
                "whether to author now, and if so, finalise the ORDERED CHAIN of ToolSpecs the "
                "whole analysis needs (tool_specs)."
            ),
            "tool_requests": [spec.model_dump(mode="json") for spec in requests],
            "worker_reports": [report.model_dump(mode="json") for report in reports],
            "decision_semantics": (
                "A worker surfaces only the FIRST gap it hits (it cannot run past a missing step), "
                "but you read the full analysis prescription in the reports — so plan the WHOLE "
                "dependency chain the analysis needs, not just that first gap. Browse the toolkit's "
                "lib/ source (read_file/list_dir) before deciding, then return tool_specs as an "
                "ordered list in dependency order (acquire before reduce before analyse before "
                "plot). For each ToolSpec set the target package, the disposition ('extend_existing' "
                "when the capability already exists in lib/ and only needs surfacing as a skills/ "
                "tool; 'new_package' when it is genuinely absent), and the verification checks. The "
                "tools are authored back-to-back, each seeing the prior tool's committed code, and "
                "the workers rerun once after the whole chain — so keep the data/analysis tool and "
                "its companion plotter as separate entries in the SAME chain. should_author=false "
                "(empty tool_specs) when the worker simply mis-called an existing tool. Never "
                "fabricate; escalate via ask_human if blocked."
            ),
        }
        if self.tools is None:
            return await self._call(payload, AuthoringDecision, action="decide_authoring")

        seed = [
            ModelMessage(role="system", content=self.prompt),
            ModelMessage(role="user", content=_payload_text(payload)),
        ]
        messages, _, _ = await self._react(
            seed_messages=seed, tools=self.tools, action="decide_authoring",
            cap=_react_loop_cap(),
            # §5.5: the lead settles the toolkit design with the human via the budget-free
            # plan_and_debate channel; those rounds don't count against react_loop_cap, only
            # against the larger react_loop_absolute_cap backstop.
            free_tools=frozenset({PLAN_AND_DEBATE_TOOL}),
            absolute_cap=_react_loop_absolute_cap(),
        )
        return await self._call_over_messages(
            messages, AuthoringDecision, action="decide_authoring", tools=self.tools,
        )

    async def drive_authoring(
        self,
        tool_spec: ToolSpec,
        *,
        toolkit_root: Path,
        worktree_base: Path,
        direct_branch: str | None = None,
        colead: Any | None = None,
        human_handler: Any | None = None,
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
        """
        from auto_research.sandbox.merge_flow import build_authoring_session

        session = build_authoring_session(
            tool_spec,
            lead=self,
            toolkit_root=toolkit_root,
            worktree_base=worktree_base,
            colead=colead,
            human_handler=human_handler,
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

        spec_id = session.tool_spec.id
        if session.committed:
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

    async def finalize_authoring_outcome(
        self,
        task: AnalysisTask,
        reports: Sequence[AnalystReport],
        tool_spec: ToolSpec,
        result: MergeFlowResult,
    ) -> AnalystReport:
        """Compose the terminal analyst report after an authoring attempt did not commit (§9.6).

        Called by ``DebateToConverge`` when ``propose_and_pr`` returns a non-committed result
        (gate/review rework cap exhausted, or blocked on an absent human). The lead synthesizes
        the workers' partial findings **with** the honest authoring failure into one terminal
        :class:`AnalystReport` — documenting what was attempted, the capability that is
        unavailable under the current construction and why — and sets ``status``:
        ``needs_human`` when the flow blocked on a human, ``failed`` when the missing capability
        blocks the request, or ``ok`` when partial results still answer it. There is no resume
        and no re-authoring; the reviewer adjudicates this report downstream (the reviewer is the
        sole I/O). Never fabricate a result to cover the gap.
        """
        payload = {
            "task": task.model_dump(mode="json"),
            "failed_tool": tool_spec.model_dump(mode="json"),
            "authoring_result": {
                "status": result.status,
                "reason": result.reason,
                "review": result.review.model_dump(mode="json") if result.review else None,
            },
            "worker_reports": [report.model_dump(mode="json") for report in reports],
            "semantics": (
                "An attempt to author the toolkit capability the analysis needs did NOT succeed "
                f"(authoring status '{result.status}'). This is a correct outcome with no quality "
                "penalty — compose ONE terminal report. Document what was attempted, the "
                "capability that is unavailable under the current construction and why, and any "
                "partial findings that genuinely hold. Set status='needs_human' when blocked on a "
                "human, 'failed' when the missing capability blocks the request, or 'ok' when "
                "partial results still answer it. Never fabricate a result to cover the gap; "
                "report real results, or report none. Do not request the tool again."
            ),
        }
        # Honest-failure terminal report: attach the workers' real (cited) figures directly and
        # clear cited_figures — the model can't reproduce FigureRefs, and this path does not run the
        # citation catalog (it composes terminally rather than via edit_report). _dedup_figures keeps
        # the reviewer from a duplicate/garbage figure set (V-4 keeps the failure path honest too).
        figures = _dedup_figures([figure for report in reports for figure in report.figures])
        if self.tools is None:
            report = await self._call(
                payload, AnalystReport, images=figures, action="finalize_authoring_outcome"
            )
            return report.model_copy(update={"figures": figures, "cited_figures": []})
        seed = [
            ModelMessage(role="system", content=self.prompt),
            ModelMessage(role="user", content=_payload_text(payload)),
        ]
        messages, _, _ = await self._react(
            seed_messages=seed, tools=self.tools, action="finalize_authoring_outcome",
            cap=_react_loop_cap(), images=figures,
        )
        report = await self._call_over_messages(
            messages, AnalystReport, action="finalize_authoring_outcome", images=figures,
            tools=self.tools,
        )
        return report.model_copy(update={"figures": figures, "cited_figures": []})


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


def _dedup_figures(figures: Sequence[FigureRef]) -> list[FigureRef]:
    """Deduplicate figures by path, preserving first-seen order."""
    seen: set[str] = set()
    out: list[FigureRef] = []
    for figure in figures:
        key = str(figure.path)
        if key not in seen:
            seen.add(key)
            out.append(figure)
    return out


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


@lru_cache(maxsize=1)
def _orchestration_config() -> dict:
    """Load and cache ``config/orchestration.yaml`` (best-effort)."""
    import yaml

    config_path = Path(__file__).resolve().parents[3] / "config" / "orchestration.yaml"
    try:
        with config_path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


@lru_cache(maxsize=1)
def _structured_retry_cap() -> int:
    """Max attempts for a structured ``_call`` before raising (orchestration.yaml).

    Key: ``structured_retry_cap``. Defaults to 2. Mirrors ``tool_retry_cap`` for
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
# diagnosis without unbounded log growth — see monitor/DOCS.md).
_EVENT_ARG_CAP = 2_000
_EVENT_RESULT_CAP = 4_000


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
