"""Async role wrappers for the Phase 1 agent team."""

from __future__ import annotations

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
    AUTHOR_CHAIN_TOOL,
    EDIT_REPORT_TOOL,
    LEAD_FORM_TOOLS,
    LEAD_REPORT_FIELDS,
    LEAD_STATUS_VALUES,
    WORKER_REPORT_FIELDS,
    WORKER_STATUS_VALUES,
    LeadCycle,
    LeadCycleOutcome,
    MonitorToolProvider,
    REVIEW_REPORT_TOOL,
    RESPOND_TO_REVIEW_TOOL,
    ReadOnlyToolProvider,
    ReportWorkspace,
    lead_turn_problems,
    SUBMIT_TOOL,
    WAIT_FOR_REPLY_TOOL,
    SandboxToolProvider,
    ToolOutcome,
    LIST_PEERS_TOOL,
    MESSAGE_TOOL,
    VIEW_RECEIPT_TOOL,
    execute_coder_message,
    execute_list_peers,
    execute_view_receipt,
    list_peers_tool_def,
    execute_wait_for_reply,
    message_tool_def,
    view_receipt_tool_def,
    wait_for_reply_tool_def,
    _written_report,
)
from auto_research.env import react_grace_rounds, ledger_caps
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
from auto_research.sandbox.workspace import (
    append_memory,
    read_memory,
    report_record_path,
    round_report_path,
)
from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    AuthoringDecision,
    DocumentRef,
    FigureRef,
    GateReport,
    MergeFlowResult,
    ResearchRequest,
    ToolResult,
    ToolReviewVerdict,
    ToolSpec,
)
from auto_research.sources import ArxivSource, NasaAdsSource, QuerySource, WebSource
from auto_research.env import papers_dir
from auto_research.storage import MemoryRecord, Storage
from auto_research.schemas.literature import LiteratureReport
from auto_research.provenance import ReceiptStore, ScopedLookup, parse_bindings
from auto_research.schemas.artifacts import Receipt


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
        # The quick channel (E12): the run's SeatRouter, set by run_research on the graph seats
        # only (never the co-lead or the status monitor), and the last finished turn kept so a
        # message can resume it.
        self.channel: Any | None = None
        self._kept_turn: dict[str, Any] | None = None

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
        tools: Any,
        resumed: bool = False,
        declared: list[dict] | None = None,
        **kwargs: Any,
    ) -> tuple[list, list[FigureRef], list[DocumentRef]]:
        """Run :meth:`_react_loop`, as a seat of the quick channel when one is wired (E12).

        With a router the provider is wrapped in :class:`ChannelTools`, the seat is marked live
        for the loop's duration (so a text to it lands in its inbox rather than resuming it), and
        a turn that ends on its terminal tool is kept, so a later message can resume it. Without
        one this is exactly the loop.
        """
        if getattr(self, "channel", None) is None and _has_authoring_surface(tools):
            # The lead reaches its coder only through `message(to="coder")`, so a lead that runs
            # authoring must be on a channel however it was built — a one-seat channel (no human,
            # no peers) when run_research did not put it on the team's.
            from auto_research.hitl.router import SeatRouter

            self.channel = SeatRouter(self.ensure_run_control(), monitor=self.monitor)
            self.channel.register(self.agent_key, self)
        if getattr(self, "channel", None) is None:
            return await self._react_loop(tools=tools, **kwargs)
        wrapped = ChannelTools(tools, role=self, resumed=resumed, declared=declared)
        kwargs["free_tools"] = frozenset(kwargs.get("free_tools", frozenset())) | {
            WAIT_FOR_REPLY_TOOL, LIST_PEERS_TOOL
        }
        control = getattr(self, "run_control", None)
        if control is not None:
            control.set_live(self.agent_key, True)
        try:
            result = await self._react_loop(tools=wrapped, **kwargs)
        finally:
            if control is not None:
                control.set_live(self.agent_key, False)
        if self.last_filed_form is not None and kwargs.get("terminal_tools"):
            self._kept_turn = {
                "messages": result[0],
                "tools": tools,
                "state": {
                    name: getattr(tools, name)
                    for name in _KEPT_PROVIDER_STATE
                    if hasattr(tools, name)
                },
                "declared": wrapped.tool_definitions(),
                "kwargs": {
                    k: v for k, v in kwargs.items()
                    if k in ("cap", "free_tools", "absolute_cap", "terminal_tools")
                },
            }
        return result

    def has_kept_turn(self) -> bool:
        """Whether this seat finished a turn a message can resume (E12)."""
        return getattr(self, "_kept_turn", None) is not None

    async def resume(self) -> None:
        """Resume the kept turn to read what reached the inbox: the seat's normal loop, normal
        tools and cap, exit only by ``turn_end`` — with the filed report final (E12)."""
        kept = getattr(self, "_kept_turn", None)
        if kept is None:
            return
        tools = kept["tools"]
        sandbox = getattr(tools, "sandbox", None)
        if sandbox is not None and hasattr(sandbox, "is_alive") and not sandbox.is_alive:
            await sandbox.start()
        restore = {name: getattr(tools, name) for name in kept["state"]}
        for name, value in kept["state"].items():
            setattr(tools, name, value)
        saved_form, saved_response = self.last_filed_form, self.last_response
        try:
            await self._react(
                seed_messages=kept["messages"], share_messages=True, tools=tools,
                action="resume", resumed=True, declared=kept["declared"],
                require_terminal=True, **kept["kwargs"],
            )
        finally:
            for name, value in restore.items():
                setattr(tools, name, value)
            self.last_filed_form, self.last_response = saved_form, saved_response

    def ensure_run_control(self) -> Any:
        """This seat's RunControl, creating one when the role runs without a run (a benchmark's
        lead), so the coder's messages still reach its inbox."""
        if self.run_control is None:
            from auto_research.run_control import RunControl

            self.run_control = RunControl()
        if not self.agent_key:
            self.agent_key = self.role
        return self.run_control

    async def _react_loop(
        self,
        *,
        seed_messages: Sequence[Any],
        tools: Any,  # ToolProvider — duck-typed to avoid import cycle
        action: str,
        cap: int,
        images: Sequence[FigureRef] = (),
        on_result=None,
        share_messages: bool = False,
        require_terminal: bool = False,
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
        # Normally a copy: a caller's seed is its own. `share_messages` hands the loop the
        # caller's actual list instead, which is what lets the lead's chain driver drop a finished
        # link's coding transcript from the LIVE trajectory (A7). The alternative was a nested
        # per-link loop, and that is the topology whose only exits were whole-cycle terminals.
        messages: list = seed_messages if share_messages else list(seed_messages)
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
        # Grace (§2b): budget exhaustion is not an exit. When the budget runs out and there IS a
        # terminal tool to reach, the role is told so and gets a bounded window to compose and
        # file. A loop with no terminal tool has no exit to force, so its exhaustion stays a
        # legitimate end and this never opens.
        grace_total = react_grace_rounds() if terminal_tools else 0
        # The ceiling must leave room for the grace window, or the window is cut short and the
        # loop returns unfiled with nothing recorded — which the live adversarial cell found on
        # 2026-09-15 (grace opened, the ceiling ended the loop two rounds later, silently). Grace
        # is a window the role is PROMISED; a bound that can retract it is not a window.
        if grace_total:
            absolute_ceiling = max(absolute_ceiling, budget_cap + grace_total + 1)
        grace_used = 0
        in_grace = False
        # Injected turns waiting for a legal boundary (A2). Anything the harness wants to say to
        # the model mid-loop lands here first — a human steer, and from B onward a background
        # coder event — rather than being appended wherever the code happened to be.
        injections: list[tuple[str, str]] = []  # (kind, text)
        while iteration < absolute_ceiling:
            if budget_used >= budget_cap and not in_grace:
                if grace_total <= 0:
                    break  # no terminal tool to reach, or grace disabled: the old behaviour
                in_grace = True
                await self._record(
                    f"{self.role}.{action}.grace_opened", level=3,
                    summary=f"{self.role} ran out of budget; {grace_total} grace round(s) to file",
                    data={"cap": budget_cap, "grace": grace_total, "iteration": iteration},
                )
                injections.append(("grace", _GRACE_OPENED.format(grace=grace_total)))
            if in_grace and grace_used >= grace_total:
                await self._record(
                    f"{self.role}.{action}.grace_exhausted", level=1,
                    summary=f"{self.role} did not file within its grace window — fatal",
                    data={"cap": budget_cap, "grace": grace_total, "iteration": iteration},
                )
                raise ReactBudgetExhausted(
                    f"{self.role}.{action}: used its full budget ({budget_cap}) and all "
                    f"{grace_total} grace rounds without filing a terminal form. Exhausting a "
                    "cap is not an exit (CLAUDE.md §2b); nothing is composed on a role's behalf."
                )
            # Cooperative HITL steer (§12): before the next model turn, collect any human steer
            # for this role. A no-op when run_control is unset. A /stop is NOT read here: it
            # cancels the run task outright (see ``RunControl.interrupt``), so there is nothing
            # for this loop to notice.
            # Every text this seat receives — a human steer, the human's reply, another seat's
            # message, the coder's question, a runtime notice — arrives through its inbox (E12).
            if self.run_control is not None:
                for entry in self.run_control.drain_inbox(self.agent_key):
                    injections.append((entry.kind, entry.render()))
            # B4: the coder runs beside this loop, so when it stops the lead is TOLD rather than
            # having to poll. Same queue and the same hold rule as a human steer — a pushed event
            # is exactly what made the A2 race reachable, so it goes through the same gate.
            coder_note = _coder_event(tools)
            if coder_note is not None:
                injections.append(("coder_event", coder_note))
            # ONE drain, here, and only at a legal boundary. An injected user turn is legal after
            # a COMPLETE assistant turn; it is not legal after one that left a provider-executed
            # tool call unresolved, where Anthropic documents that the following message "must
            # contain nothing except the `tool_result` blocks" and that anything after them ends
            # the turn — the 400 that killed run 8c75990e at $42.11 with 4 tools committed.
            #
            # So when the turn we are answering has an unresolved server tool, everything queued
            # is HELD for one iteration. Nothing is dropped; delivery moves to the next boundary,
            # which the resolving turn provides. The previous code appended in two places (here,
            # and again mid-batch right after the tool results), neither of which checked.
            held = _last_turn_has_unresolved_server_tool(messages)
            if injections and not held:
                for kind, text in injections:
                    messages.append(ModelMessage(role="user", content=text))
                    await self._record(
                        f"{self.role}.{action}.{kind}", level=3,
                        summary=f"{self.role} received a human steer"
                        if kind == "steer" else f"{self.role} was told: {text[:60]}",
                        data={"message": _cap_text(text, _event_arg_cap())},
                    )
                injections.clear()
            elif injections:
                await self._record(
                    f"{self.role}.{action}.injection_held", level=4,
                    summary=(
                        f"{self.role} held {len(injections)} injected turn(s) — the previous "
                        "turn has an unresolved server-tool call"
                    ),
                    data={"held": len(injections), "kinds": sorted({k for k, _ in injections})},
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
                if (
                    require_terminal
                    and nudge is None
                    and getattr(response, "stop", None) not in ("refusal", "transient")
                ):
                    # No escape path. For a role that writes its artifact inside the loop,
                    # `turn_end` is the submission, so any other ending throws the work away —
                    # and `_continuation_for`'s length test waved exactly that through (58
                    # characters of text over 799 output tokens, the rest reasoning it cannot
                    # see). A refusal or a transient still ends the loop: repeating the request
                    # cannot fix either, and re-injecting into one would spin to the ceiling.
                    nudge = _TERMINAL_REQUIRED_NUDGE
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
                if nudge is None or (
                    silent_turns >= _SILENT_TURN_CAP and not require_terminal
                ):
                    break
                silent_turns += 1
                # The whole turn is replayed, then the nudge as its own NEW user message — never
                # appended to a tool-result turn, which is the arrangement Anthropic names as the
                # cause of the empty response in the first place.
                messages.append(response)
                if response.has_unresolved_server_tool:
                    # The one case where the nudge itself is the hazard (A2). `paused` is the
                    # provider's server-side tool loop hitting its iteration limit, and the turn
                    # it returns may carry a server call with no result — the documented
                    # continuation is "send the assistant content back to continue", as-is. A user
                    # message appended here is "a block added after the results", which ends the
                    # turn and strands the call. So: replay the turn and say nothing.
                    #
                    # Measured dead at the time of writing — 184 silent turns across 221 runs,
                    # `stop == "end_turn"` on every one, zero `paused` ever — and guarded anyway,
                    # because the state becomes reachable the day any hosted vendor tool is
                    # declared, and these are the lines that would have to change.
                    await self._record(
                        f"{self.role}.{action}.paused_resend", level=4,
                        summary=(
                            f"{self.role} paused mid-server-tool; re-sending the turn as-is "
                            "with no continuation message"
                        ),
                        data={"stop": response.stop, "iteration": iteration},
                    )
                    iteration += 1
                    continue
                # A silent turn IS a model call, so it advances the absolute ceiling even though
                # it consumes no tool budget (`consumed_budget` is False with no tool calls).
                # This is what bounds `require_terminal`: with `_SILENT_TURN_CAP` no longer
                # ending that loop, the ceiling is the only remaining bound, and without this
                # line there is none — the first version of the no-escape rule span forever
                # against a model that kept answering in prose (caught by its own test, 2026-09-15).
                iteration += 1
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
            batch_steers: list[Any] = []  # InboxEntry
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
                if filed_terminal:
                    # The turn ended at an accepted terminal earlier in this batch: what follows
                    # is answered, never run, so nothing replaces the report the validator
                    # accepted (found 2026-09-25: an `edit_report` after `turn_end` overwrote it).
                    outcome = ToolOutcome(
                        text=(
                            f"{call.name} was not run: your turn already ended with an accepted "
                            f"{self.last_filed_form[0]}, and the report it accepted stands."
                        ),
                        is_error=True,
                    )
                elif call.arguments_error:
                    # A garbled call is answered, never run on empty arguments ([I4]).
                    outcome = ToolOutcome(
                        text=(
                            f"{call.name} was not run: its arguments were not valid JSON "
                            f"({call.arguments_error}). Re-issue the call with valid JSON arguments."
                        ),
                        is_error=True,
                    )
                else:
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
                    batch_steers.extend(self.run_control.drain_inbox(self.agent_key))
            if in_grace:
                # The countdown rides the tool results rather than a separate user turn: a turn
                # appended after tool results is the one position a provider forbids when the
                # answered turn left a server tool unresolved (A2), and a model reads what comes
                # back from its own call.
                remaining = max(0, grace_total - grace_used - 1)
                note = _GRACE_REMAINING.format(remaining=remaining)
                result_parts = [
                    part.model_copy(update={"content": part.content + note})
                    for part in result_parts
                ]
            messages.append(ToolResultMessage(results=result_parts))
            # Queued rather than appended here (A2). This drain used to write a user turn
            # immediately after the tool results it had just built, which is the one position the
            # provider forbids when the assistant turn being answered left a server tool
            # unresolved. It now joins the single top-of-iteration drain, which runs after the
            # hold check — and by then this batch's results are complete, so an injected turn can
            # never split a `tool_use` from its `tool_result`.
            injections.extend((entry.kind, entry.render()) for entry in batch_steers)
            # Budget accounting: an iteration consumes budget unless EVERY tool it called is
            # free (e.g. plan_and_debate). Free-only iterations advance `iteration` (so the
            # absolute backstop still bounds them) but not `budget_used`.
            consumed_budget = any(call.name not in free_tools for call in response.tool_calls)
            if consumed_budget:
                if in_grace:
                    grace_used += 1
                else:
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
    """Reviewer role: the sole I/O — one loop per turn, one report, filed through ``turn_end`` (§2b).

    Every turn is a single ReAct loop whose only exit is ``turn_end``. The reviewer writes its
    :class:`ReviewerReport` inside the loop with ``edit_report`` and names who receives it
    (``assign_to``, human 2026-09-25): the librarian or the analysts get its ``follow_up`` as their
    task, and a report to the human is rendered to LaTeX and compiled by the ``turn_end`` check
    itself, so it is validated at the role's edge rather than composed after it. Nothing is
    composed on the reviewer's behalf: a loop that ends without an accepted ``turn_end`` raises.

    What this replaced: 2026-09-24, a verdict from a structured call after an unvalidated loop;
    2026-09-25, two stage-bound reviews (literature, analysis) each routing on its own flag.
    """

    role = "reviewer"
    #: The run's receipt store and this role's work folder; the runtime sets both.
    receipts: Any | None = None
    report_dir: Path | None = None

    async def review(
        self,
        request: ResearchRequest,
        *,
        arrived: str = "request",
        literature: Sequence[LiteratureReport] = (),
        literature_receipts: Mapping[str, Any] | None = None,
        report: AnalystReport | None = None,
        previous: Sequence["ReviewerReport"] = (),
        human_nudges: Sequence[str] = (),
        human_files: Sequence[str] = (),
        budgets: Mapping[str, int] | None = None,
        human_attended: bool = False,
        render: Any | None = None,
    ) -> "ReviewerReport":
        """Run one reviewer turn and return the report it filed.

        ``arrived`` names what came back since the last turn (``request``, ``librarian``,
        ``analysts`` or ``human``); ``literature`` is every literature report of the run and
        ``literature_receipts`` the documents they read; ``previous`` is the reviewer's own earlier
        reports; ``human_nudges`` the human's replies (guidance, never receipts) and
        ``human_files`` the handles of files the human sent; ``budgets`` the passes left per capped
        team; ``render`` (a ``ReviewerRender``) makes a report to the human part of the
        ``turn_end`` check. The reviewer may cite its own receipts, the librarian's, every receipt
        the lead's report cites with their ancestors, and — only where it cites them — its inputs
        (the request and the human's files).
        """
        from auto_research.agents.reviewer_tools import (
            REVIEWER_EDIT_DESCRIPTION,
            REVIEWER_REPORT_FIELDS,
            REVIEWER_SOURCE,
            REVIEWER_STATUS_VALUES,
            reviewer_claims,
            reviewer_draft_view,
            reviewer_scope,
            reviewer_text_view,
            reviewer_turn_problems,
        )
        from auto_research.agents.paper_tools import PaperReader
        from auto_research.provenance import ScopedLookup, check_report_fields
        from auto_research.schemas.review import ReviewerReport

        if self.tools is None or self.receipts is None or self.report_dir is None:
            raise RuntimeError(
                "the reviewer needs its tools, the run's receipt store and a report folder; "
                "the runtime wires all three (graph/runtime.py)"
            )
        store = self.receipts
        figure_handles = _receipt_handles_for_figures(store, report.figures if report else ())
        request_handle = _request_receipt(store, request)
        admitted = [
            request_handle,
            *(literature_receipts or {}),
            *_cited_handles(report, REVIEWER_SOURCE),
            *figure_handles,
            *human_files,
        ]
        budgets = dict(budgets or {})
        lookup = ScopedLookup(store, own_source=REVIEWER_SOURCE, admitted=admitted)
        self._reviews = getattr(self, "_reviews", 0) + 1
        workspace = ReportWorkspace(
            report_path=report_record_path(Path(self.report_dir), f"review{self._reviews}.jsonl"),
            report_model=ReviewerReport,
            catalog={},
            own_source=REVIEWER_SOURCE,
            prose_fields=REVIEWER_REPORT_FIELDS,
            status_values=REVIEWER_STATUS_VALUES,
            receipts=lookup,
            receipt_source=REVIEWER_SOURCE,
            itemised=(),
            provenance_check=lambda r: check_report_fields(
                reviewer_claims(r), lookup, own_source=REVIEWER_SOURCE, itemised=(),
                scope=reviewer_scope(lookup),
            ).problems,
            draft_view=(
                reviewer_draft_view(render.report_dir, render.run_id, lookup)
                if render is not None and "tex" in render.formats
                else reviewer_text_view(lookup)
            ),
            edit_description=REVIEWER_EDIT_DESCRIPTION,
        )
        accepted: list[bool] = []

        def validator() -> list[str]:
            problems = reviewer_turn_problems(
                workspace.written_report, lookup=lookup, scope=reviewer_scope(lookup),
                budgets=budgets, render=render,
            )
            if not problems:
                accepted.append(True)
            return problems

        payload = {
            "request": request.model_dump(mode="json"),
            "request_receipt": request_handle,
            "arrived": arrived,
            "your_turn": _review_instruction(arrived, budgets, human_attended),
            "your_earlier_reports": [_earlier_report(r) for r in previous],
            "literature_reports": [lit.model_dump(mode="json") for lit in literature],
            "literature_documents": [
                {"handle": h, "title": getattr(r, "title", None), "ids": getattr(r, "ids", {})}
                for h, r in (literature_receipts or {}).items()
            ],
            "analysis_report": report.model_dump(mode="json") if report else None,
            "human_nudges": list(human_nudges),
            "human_files": [_human_file(store, h) for h in human_files],
            "figures_you_can_place": [
                {"handle": h, "caption": store.get(h).caption} for h in figure_handles
            ],
        }
        seed = [
            ModelMessage(role="system", content=self.prompt),
            ModelMessage(role="user", content=_payload_text(payload)),
        ]
        provider = self.tools
        saved = {
            name: getattr(provider, name, None)
            for name in ("report_workspace", "turn_end_validator", "receipts", "receipt_source")
        }
        saved_extra = list(getattr(provider, "extra_tools", ()) or ())
        # The knowledge base the reader takes titles and identifiers from: the provider's, or the
        # default database `text_search` itself falls back to, so the two never disagree.
        storage = getattr(getattr(provider, "retrieval", None), "storage", None) or Storage()
        from auto_research.env import papers_dir as _papers_dir

        papers = _papers_dir().resolve()
        reader = PaperReader(
            # A paper receipt is evidence: only a PDF in the papers folder becomes one, never a file
            # a run wrote (a worker's scratch PDF had read as a paper).
            store, lookup, source=REVIEWER_SOURCE,
            allowed=lambda p: provider._allowed(p) and Path(p).resolve().is_relative_to(papers),
            storage=storage,
            unreadable_action="state it as a limitation if the review depended on it",
        )
        provider.report_workspace = workspace
        provider.turn_end_validator = validator
        provider.receipts = store
        provider.receipt_source = REVIEWER_SOURCE
        provider.extra_tools = [*saved_extra, reader.tool()]
        try:
            await self._react(
                seed_messages=seed,
                tools=provider,
                action="review",
                cap=_react_loop_cap(),
                images=report.figures if report else (),
                free_tools=frozenset({TURN_END_TOOL, EDIT_REPORT_TOOL, REVIEW_REPORT_TOOL}),
                absolute_cap=_react_loop_absolute_cap(),
                require_terminal=True,  # turn_end is the only exit
                terminal_tools=frozenset({TURN_END_TOOL}),
            )
        finally:
            for name, value in saved.items():
                setattr(provider, name, value)
            provider.extra_tools = saved_extra
        filed = workspace.written_report
        if not accepted or filed is None:
            # A refusal or a transient provider stop ends the loop without a filed report; the
            # budget case already raised inside `_react`. Either way nothing is composed here.
            raise RuntimeError("the reviewer's turn ended without an accepted turn_end")
        await self._record(
            "reviewer.review.filed", level=2,
            summary=f"reviewer filed its report to the {filed.assign_to}",
            data={"report": filed.model_dump(mode="json"), "path": str(workspace.report_path)},
        )
        return filed


def _request_receipt(store: Any, request: ResearchRequest) -> str:
    """The research request as an input receipt (``input:hN``), one per distinct text.

    The request's own numbers — an ObsID, an energy band the human named — reach every role's
    task, and a role that repeats them grounds them like any other number.
    """
    text = request.question + (f"\n\n{request.context}" if request.context else "")
    return store.mint_input(text, kind="human", produced_by="research_request").handle


_ARRIVED = {
    "request": (
        "A new research request. Scope it and plan the work, then assign the first step."
    ),
    "librarian": "The librarian's report on your last questions has arrived (the newest in "
                 "literature_reports).",
    "analysts": "The analysts' report has arrived (analysis_report).",
    "human": (
        "The human replied to your report (the newest in human_nudges). Discuss it with them where "
        "useful, then decide the next step."
    ),
}


def _review_instruction(arrived: str, budgets: Mapping[str, int], human_attended: bool) -> str:
    """The reviewer's task for this turn, with the budgets the graph actually has left."""
    lit, ana = budgets.get("librarian", 0), budgets.get("analysts", 0)
    human = (
        "A report to the human is sent to them as a PDF, and their reply comes back to you."
        if human_attended else
        "No human is attending this run: a report to the human is published and ends the run."
    )
    return (
        f"{_ARRIVED[arrived]} Write your report with edit_report and set assign_to: 'librarian' or "
        "'analysts' with follow_up as their task, or 'human' with the full report (title, "
        "abstract, sections, key_findings, figures and tables; use review_report to see it "
        f"typeset and polish it once). {human} {lit} literature pass(es) and {ana} analysis "
        "pass(es) remain in this run. Then call turn_end."
    )


def _earlier_report(report: "ReviewerReport") -> dict[str, Any]:
    """One of the reviewer's own earlier reports, as its next turn reads it."""
    entry: dict[str, Any] = {"assign_to": report.assign_to, "rationale": report.rationale}
    if report.assign_to == "human":
        entry.update(title=report.title, abstract=report.abstract, key_findings=report.key_findings)
    else:
        entry["follow_up"] = report.follow_up
    return entry


def _human_file(store: Any, handle: str) -> dict[str, Any]:
    """A file the human sent, as the reviewer reads it: its handle, kind and caption."""
    receipt = store.get(handle)
    if receipt is None:
        return {"handle": handle, "missing": True}
    return {"handle": handle, "kind": receipt.kind, "caption": receipt.caption}


def _receipt_handles_for_figures(store: Any, figures: Sequence[FigureRef]) -> list[str]:
    """The receipt handle of each figure, matched by content hash (the lead cites FigureRefs)."""
    by_sha = {r.sha256: r.handle for r in store.receipts(kind="figure")}
    return list(dict.fromkeys(by_sha[f.sha256] for f in figures if f.sha256 in by_sha))


def _cited_handles(report: AnalystReport | None, own_source: str) -> list[str]:
    """Every receipt handle the lead's report cites in its prose."""
    from auto_research.provenance import parse_bindings

    if report is None:
        return []
    texts = [report.summary or "", report.uncertainty or "", report.rationale or "",
             *(report.findings or [])]
    return list(dict.fromkeys(
        b.handle for text in texts for b in parse_bindings(text) if not b.handle.startswith("self:")
    ))


class LibrarianIOAgent(AgentRole):
    """Librarian: research the literature for a request in one loop, and file what it found.

    One ReAct loop that ends only through ``turn_end`` (CLAUDE.md §2b), the same unit as every
    other graph role. It plans what to look for, searches, opens papers and pages in full, and
    writes a :class:`LiteratureReport` inside the loop whose findings cite what it read by handle.
    Each document it opens is minted as a paper/web receipt in the run's receipt store
    (:mod:`auto_research.sources.receipts`), and those receipts are handed on with the report.
    """

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
        # This librarian's working folder (set by the runtime): its per-pass report JSONL and the
        # fetched web pages live here. Papers go to the shared papers dir, where the knowledge
        # base points at them.
        self.report_dir: Path | None = None
        self.agent_key: str | None = None
        self._pass = 0
        # The run's ONE receipt store (the runtime sets it: `ReceiptStore.for_run(run_dir)`). Unwired
        # — a standalone call with no run — the working folder stands in for the run directory, so
        # the call still mints and checks real receipts, beside that folder and outside it.
        self.receipts: ReceiptStore | None = None
        # What the last pass read, by handle: the receipts its report cites.
        self.last_receipts: dict[str, Receipt] = {}

    async def research(
        self,
        request: ResearchRequest,
        *,
        follow_up: str | None = None,
    ) -> LiteratureReport:
        """Research the literature for ``request`` and return the report the librarian filed.

        ``follow_up`` is the reviewer's questions for this pass (every pass has them). The loop
        either files a report through ``turn_end`` or raises ``ReactBudgetExhausted``; nothing is
        composed on the librarian's behalf.
        """
        import tempfile

        from auto_research.agents.librarian_tools import (
            LIBRARIAN_FREE_TOOLS,
            LibrarianToolProvider,
        )

        self._pass += 1
        work_dir = self.report_dir or Path(tempfile.mkdtemp(prefix="librarian_"))
        if self.receipts is None:
            self.receipts = ReceiptStore.for_run(work_dir)
        provider = LibrarianToolProvider(
            self.sources,
            storage=self.storage,
            store=self.receipts,
            report_path=report_record_path(work_dir, f"literature_pass{self._pass}.jsonl"),
            papers_dir=papers_dir(),
            record=self._record,
        )
        payload = {
            "research_request": request.model_dump(mode="json"),
            "follow_up": follow_up,
        }
        seed = [
            ModelMessage(role="system", content=self.prompt),
            ModelMessage(role="user", content=_payload_text(payload)),
        ]
        await self._react(
            seed_messages=seed,
            tools=provider,
            action="research",
            cap=_librarian_react_loop_cap(),
            free_tools=LIBRARIAN_FREE_TOOLS,
            require_terminal=True,
            terminal_tools=frozenset({TURN_END_TOOL}),
            absolute_cap=_react_loop_absolute_cap(),
        )
        report = provider.written_report
        if report is None:  # pragma: no cover — _react raises before an unfiled return
            raise RuntimeError("librarian loop returned without a filed report")
        # The verify-against-the-PDF list: every value the report attributes to a paper, collected by
        # the runtime so a person can check each one (papers are cited, never text-matched).
        report = report.model_copy(update={"attributed_values": provider.attributed_values(report)})
        provider.report_path.write_text(report.model_dump_json() + "\n", encoding="utf-8")
        self.last_receipts = dict(provider.captures)
        await self._record(
            "librarian_io.research.filed",
            level=3,
            summary=(
                f"Librarian filed {len(report.findings)} finding(s) over "
                f"{len(provider.captures)} document(s); status {report.status}"
            ),
            data={
                "report": report.model_dump(mode="json"),
                "receipts": {h: r.model_dump(mode="json") for h, r in provider.captures.items()},
            },
        )
        await self._lay_down_memory(report, provider.paper_ids)
        return report

    async def _lay_down_memory(self, report: LiteratureReport, paper_ids: Mapping[str, int]) -> None:
        """Record each finding as memory on the papers it cites, so later runs retrieve it.

        Appended, never replacing: an earlier run's notes on the same paper answer a different
        question and stay searchable beside these.
        """
        for handle, paper_id in paper_ids.items():
            claims = [f.claim for f in report.findings if handle in f.support]
            if not claims:
                continue
            try:
                await self.storage.insert_memory(
                    MemoryRecord(
                        paper_id=paper_id,
                        summary=claims[0][:300],
                        tags=[],
                        mem_abbrev="\n".join(claims),
                    )
                )
            except Exception as exc:  # noqa: BLE001 — memory is a convenience, not the record
                await self._record(
                    "librarian_io.memory.error", level=3,
                    summary=f"could not store memory for {handle}", data={"error": repr(exc)},
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
        # Provenance: the run's ReceiptStore (set by runtime). Every tool call this worker makes
        # mints receipts under its agent_key, and its report's numbers are checked against them.
        self.receipts: ReceiptStore | None = None
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
            # The software environment its tools run in, named up front (env plan B2a); `list_env`
            # shows its packages.
            "environment": _environment_brief(self.sandbox),
        }
        peer_figures = [figure for report in peer_reports for figure in report.figures]

        # No tool surface (e.g. Studio): plain tool-less structured report.
        if self.sandbox is None or self.registry is None:
            return await self._call(payload, AnalystReport, images=peer_figures, action="analyze")

        if not self.sandbox.is_alive:
            await self.sandbox.start()

        self._round += 1
        # The agent's own folder (report_dir), never memory.md's: memory lives beside the ledger,
        # out of the jail's reach (F22).
        cwd = self.report_dir or (self.memory_path.parent if self.memory_path is not None else None)
        _mint_task_input(self.receipts, task)
        workspace = self._make_report_workspace(peer_reports)
        provider = SandboxToolProvider(
            self.registry, self.sandbox, cwd=cwd, web_source=WebSource(),
            figure_sink=self._figure_catalog["self"],
            report_workspace=workspace,
            receipts=self.receipts,
            receipt_source=self.agent_key if self.receipts is not None else None,
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
            # No escape path (CLAUDE.md §2b). The worker writes its report INSIDE this loop and
            # `turn_end` submits it, so the lead's rule is this loop's rule — and all three
            # measured losses are the worker's, not the lead's: a completed 14-quantity
            # reproduction, a comparison CSV and two more analyses, each discarded because a
            # 58-to-66-character turn read as a finish.
            require_terminal=True,
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

    #: Which report fields this role may write, and which statuses it may declare (A3). One report
    #: class serves the worker and the lead; the SURFACE is what keeps them apart, so it is named
    #: on the role rather than defaulted in the shared builder below.
    report_fields: tuple[str, ...] = WORKER_REPORT_FIELDS
    report_status_values: tuple[str, ...] = WORKER_STATUS_VALUES

    def _make_report_workspace(
        self, peer_reports: Sequence[AnalystReport] = ()
    ) -> ReportWorkspace | None:
        """Build this round's report workspace (V-4), or None when no report dir is wired.

        The report dir is the agent's working folder (``report_dir``, else ``memory_path.parent``);
        the per-round JSONL path is derived from role + model + the monotonic debate round.

        The surface comes from the ROLE (``report_fields`` / ``report_status_values``). It used to
        default to the worker's here for every caller, which silently denied the lead the decision
        fields its whole contract rests on — found live by a lead that said so in its own report:
        *"the schema I am given has no rework field, so the decision is carried in prose."* A
        decision carried in prose is a decision the harness cannot act on, which is the exact
        failure class A3 exists to remove.
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
            prose_fields=self.report_fields,
            status_values=self.report_status_values,
            receipts=_receipt_scope(self.receipts, self.agent_key, peer_reports),
            receipt_source=self.agent_key,
        )


def _mint_task_input(store: ReceiptStore | None, task: Any) -> None:
    """Record the task a unit was handed. A brief grounds nothing (``is_evidence``); the request's
    own numbers are restated by citing the request's receipt."""
    if store is None or task is None:
        return
    text = "\n".join(filter(None, [getattr(task, "question", ""), getattr(task, "instructions", "")]))
    if text.strip():
        store.mint_input(text)


def _receipt_scope(
    store: ReceiptStore | None, own_source: str | None, reports: Any = ()
) -> ScopedLookup | None:
    """What a role may cite: its own receipts, plus those the reports it received cite.

    ``None`` when no store is wired (Studio), which leaves the report's numbers unchecked.
    """
    if store is None or not own_source:
        return None
    cited: list[str] = []
    for report in reports:
        texts = [getattr(report, "summary", "") or "", getattr(report, "uncertainty", "") or ""]
        texts.extend(getattr(report, "findings", None) or [])
        for text in texts:
            cited.extend(b.handle for b in parse_bindings(text))
    return ScopedLookup(store, own_source=own_source, admitted=cited)


# Budget-free in the lead's cycle loop: the human design channel (§5.5), the two report-writing
# tools (V-4 — refining a report is not evidence-gathering), and the authoring tools that are
# separately bounded by the session's own submit/debate caps.
_CYCLE_FREE_TOOLS: frozenset[str] = frozenset(
    {
        # Free on every graph loop, so the rule is one rule (human ruling 2026-09-15). The
        # worker and reviewer already had it; the lead did not, which made its own tool
        # description ("Free: calling it costs no tool budget") false for one caller of four and
        # made a REFUSED turn_end cost a grace round — charging a role for trying to file inside
        # the window it was given to file in. The absolute ceiling still bounds the retries.
        TURN_END_TOOL,
        EDIT_REPORT_TOOL,
        REVIEW_REPORT_TOOL,
        SUBMIT_TOOL,
        RESPOND_TO_REVIEW_TOOL,
        # Waiting is not evidence-gathering, and it cannot be spammed: a short timeout is raised
        # to the configured floor, so the absolute ceiling bounds it at ceiling x floor seconds.
        WAIT_FOR_REPLY_TOOL,
    }
)


def _cycle_instruction(cycle: LeadCycle, reports: Sequence[Any], rounds_left: int) -> str:
    """The one instruction that opens the cycle: what the turn is for, and how it ends.

    Short on purpose — each tool's own description says what it needs, which is the channel a
    Pydantic ``#`` comment never reached and whose absence killed a launch at $19.43.
    """
    lines = [
        "This is your turn on the analysts' work. Investigate as long as you need (read_file, "
        "list_dir, web_search, data_catalog), then write YOUR REPORT with edit_report — your "
        "report is your decision — check it with review_report, and end the turn with turn_end. "
        "The harness checks the report before accepting and tells you exactly what is missing, "
        "so a rejection is a fix, not a failure.",
        "Set rework=true in the report to send the analysts back for one more round, saying why "
        f"(rationale) and what to look at (what_to_probe) — {rounds_left} round(s) still "
        "allowed. Leave rework=false and the report IS the analysis result: summary, findings "
        "and a declared status.",
    ]
    if cycle.authoring_enabled:
        raised = [
            r.tool_request.proposed_tool_name
            for r in reports
            if getattr(r, "tool_request", None) is not None
        ]
        lines.append(
            "A worker reports the toolkit lacks capability the analysis needs "
            f"({', '.join(raised) or 'see the reports'}). Read the target lib/ source first, "
            "then brief the ordered chain with author_chain — one link is one capability is one "
            "commit. Briefing does not end your turn: the first coding session opens, you steer "
            "it with message to the coder and submit_for_review, and when a link finishes the next "
            "one "
            "opens automatically. A link that cannot be made mergeable simply ends and the chain "
            "moves on; you decide when to stop and write your report."
        )
    lines.append(
        "An honest stop is a correct outcome with no quality penalty: report real results, or "
        "report none — never fabricate a result to fill a gap."
    )
    return " ".join(lines)



_CHAIN_DRIVING_TOOLS = frozenset({AUTHOR_CHAIN_TOOL, SUBMIT_TOOL, RESPOND_TO_REVIEW_TOOL})


def _unanswered_call_ids(messages: list) -> list[str]:
    """Ids of tool calls in ``messages`` that no turn answers.

    Every vendor enforces the same thing in its own vocabulary: a call replayed without its
    result is a malformed request, not a stale detail. This is that rule written as a query, so
    a conversation edit can be checked where it happens instead of being discovered as a 400 on
    the next model call.

    A trailing assistant turn whose results the loop has not appended yet reports here too, and
    legitimately so — the caller knows which turn that is; this function only states the fact.
    """
    answered = {
        part.tool_call_id
        for message in messages
        if isinstance(message, ToolResultMessage)
        for part in message.results
    }
    return [
        call.id
        for message in messages
        for call in (getattr(message, "tool_calls", None) or ())
        if call.id and call.id not in answered
    ]


def _render_span(span: list, *, per_message: int = 4000) -> str:
    """The dropped coding span as plain text for the compactor to read.

    Turns are labelled by who produced them, because the distinction is the substance: the
    lead's steering and the coder's narration read very differently and the summary has to keep
    them apart. Each turn is capped so one enormous tool result cannot crowd out the rest of a
    session that the summary is supposed to cover end to end.
    """
    out: list[str] = []
    for message in span:
        if isinstance(message, ToolResultMessage):
            for part in message.results:
                label = f"result of {part.name}" + (" (error)" if part.is_error else "")
                out.append(f"[{label}]\n{(part.content or '')[:per_message]}")
            continue
        calls = getattr(message, "tool_calls", None) or ()
        text = (getattr(message, "content", "") or "")[:per_message]
        if text:
            out.append(f"[lead]\n{text}")
        for call in calls:
            args = json.dumps(dict(call.arguments or {}), default=str)[:per_message]
            out.append(f"[lead calls {call.name}]\n{args}")
    return "\n\n".join(out)


def _compact_coding_span(messages: list, mark: int, summary: str) -> list:
    """Replace a finished link's coding span with the outcome its own opening call returns.

    A coding session is opened by a tool call — ``author_chain`` for the first link, the
    ``submit_for_review`` that closed the previous one thereafter — and ``mark`` is where that
    call's result lands. The whole span after it is the coder transcript, which the lead has no
    further use for once the link is decided::

        [… opening CALL]  [its ANSWER]  [drive/check/read × N]  [the CALL that closed the link]
             kept           REWRITTEN            dropped                kept, result pending

    The answer is rewritten rather than removed, which is what keeps the pair closed: the
    opening call keeps an answer, and that answer now carries the outcome instead of a sentence
    about a session that no longer exists. Only the parts belonging to a chain-driving tool are
    rewritten, so a turn that batched ``author_chain`` with a file read keeps the read's result.

    Nothing is synthesized as somebody else's words: a tool result is the harness's own to
    write, and this is the harness reporting what its tool did.

    Returns ``messages`` unchanged when there is nothing to compact — no answer has landed yet,
    or the message at ``mark`` is not a chain-driving tool's result.
    """
    if mark <= 0 or mark >= len(messages):
        return messages
    answer = messages[mark]
    if not isinstance(answer, ToolResultMessage):
        return messages
    if not any(part.name in _CHAIN_DRIVING_TOOLS for part in answer.results):
        return messages
    rebuilt = ToolResultMessage(
        results=[
            part.model_copy(update={"content": summary})
            if part.name in _CHAIN_DRIVING_TOOLS
            else part
            for part in answer.results
        ]
    )
    # The turn that triggered this close is still waiting for its own result, so it has to
    # survive the cut — dropping it would strand the result the loop is about to append, which
    # is the same malformed request one message later.
    pending = set(_unanswered_call_ids(messages))
    last = messages[-1]
    tail = (
        [last]
        if pending and any(c.id in pending for c in (getattr(last, "tool_calls", None) or ()))
        else []
    )
    return messages[:mark] + [rebuilt] + tail

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


class _ChainDriver:
    """Owns the authoring chain's sessions for one cycle, advancing on a tool result (A7).

    The lead's loop never ends to move between links. When a link finishes — committed, or its CI
    budget spent — the handler calls :meth:`advance`, which closes that session, drops its coding
    transcript, and opens the next link's session, returning the sentence the lead reads next.

    Every session lifecycle and every git operation lives here, in Python, exactly as before
    (§9.9): the model asks for the next link, it never builds one. ``drive_authoring`` is
    untouched and still owns its own single session — it backs the paper's published authoring
    measurements, so its path stays byte-identical.
    """

    def __init__(
        self,
        *,
        lead: "AnalystLeadAgent",
        provider: Any,
        cycle: LeadCycle,
        outcome: LeadCycleOutcome,
        messages: list,
        toolkit_root: Path | None,
        worktree_base: Path | None,
        direct_branch: str | None,
        colead: Any | None,
        human_handler: Any | None,
        coder: Any | None,
        allow_correlated_review: bool,
    ) -> None:
        self.lead = lead
        self.provider = provider
        self.cycle = cycle
        self.outcome = outcome
        self.messages = messages
        self.toolkit_root = toolkit_root
        self.worktree_base = worktree_base
        self.direct_branch = direct_branch
        self.colead = colead
        self.human_handler = human_handler
        self.coder = coder
        self.allow_correlated_review = allow_correlated_review
        self.index = 0            # links opened so far
        self.session: Any = None  # the live one, if any
        self.spec: Any = None
        self.mark = 0             # where the current link's coding transcript starts

    async def advance(self) -> str:
        """Close the current link and open the next; return what to tell the lead."""
        await self._close_current()
        specs = list(self.cycle.brief.tool_specs) if self.cycle.brief is not None else []
        if self.index >= len(specs):
            return (
                "The chain is finished — every link you briefed has been attempted. Write your "
                "report with edit_report and call turn_end. You may also brief a new chain with "
                "author_chain if the analysis still needs a capability and credits remain "
                f"({self.outcome.author_budget} left)."
            )
        if self.outcome.author_budget <= 0:
            return (
                "No author credits remain, so no further link can be opened. Write your report "
                "with edit_report — say what capability is still missing — and call turn_end. An "
                "honest stop is a correct outcome with no quality penalty (§9.6)."
            )
        spec = specs[self.index]
        self.index += 1
        self.outcome.author_budget -= 1
        try:
            await self._open(spec)
        except Exception as exc:  # noqa: BLE001 — an unopenable link is a result, not a crash
            # A dirty authoring branch is the known case (ToolkitBranchUnclean); anything else
            # that stops a session from opening is treated the same way, because an F6-class
            # crash here would kill a run that still has a report to write.
            await self.lead._record(
                "analyst.tool.branch_unclean", level=2,
                summary=f"Could not open a coding session ({spec.proposed_tool_name})",
                data={"error": repr(exc)},
            )
            result = MergeFlowResult(
                tool_spec_id=getattr(spec, "id", None) or spec.proposed_tool_name,
                status="failed",
                reason=f"the coding session could not be opened: {exc}",
            )
            self.outcome.authored.append(result)
            await self.lead._record(
                "analyst.tool.authored", level=5,
                summary=f"Tool authoring failed: {spec.proposed_tool_name}",
                data={"result": result.model_dump(mode="json")},
            )
            return await self.advance()
        return (
            f"Coding session {self.index} of {len(specs)} is open on "
            f"'{spec.proposed_tool_name}'. The coder has your brief and is working on it now — "
            "you keep your turn, and it will take minutes rather than seconds. Spend them on "
            "the library it is extending, the next link, or your report, and when you have "
            "nothing else to do for this link call wait_for_reply: it returns the moment the coder "
            "stops or asks you something, and says how long it has been working. Messages you "
            "send the coder while it works are queued for its next turn. "
            "submit_for_review waits for the coder to stop before the gate runs. If it cannot be "
            "made mergeable, let "
            "its submissions run out and the next link opens automatically — an honest failure "
            "is correct."
        )

    async def _build_session(self, spec: Any) -> Any:
        """Construct this link's authoring session.

        **This is the seam a test replaces**, and its altitude is the point. Everything else in
        `_open` — the mark, the provider swap, the background seed — is free bookkeeping that a
        test must exercise rather than stand in for; only building, connecting and driving a
        real coder costs money. A double installed one layer up (over `_open` itself) removes
        the very bookkeeping under test, which is how a mark recorded one message too early
        reached production with a green suite.
        """
        from auto_research.sandbox.merge_flow import build_authoring_session

        return build_authoring_session(
            spec,
            lead=self.lead,
            toolkit_root=self.toolkit_root,
            worktree_base=self.worktree_base or self.toolkit_root,
            colead=self.colead,
            human_handler=self.human_handler,
            coder=self.coder,
            allow_correlated_review=self.allow_correlated_review,
            direct_branch=self.direct_branch,
            monitor=self.lead.monitor,
        )

    async def _open(self, spec: Any) -> None:
        session = await self._build_session(spec)
        await session.connect()
        if self.lead.run_control is not None:
            self.lead.run_control.authoring_session = session
        self.provider.authoring_session = session
        self.session, self.spec = session, spec
        self.cycle.session_open = True
        # A message for the lead (a human steer, the coder's question) wakes a pending `wait_for_reply` at once, rather than at its
        # timeout; the steer itself is still delivered by the loop's own drain (A2 hold rule).
        control, key = self.lead.run_control, getattr(self.lead, "agent_key", None)
        if control is not None and key and hasattr(session, "watch_steers"):
            session.watch_steers(control.steer_event(key))
        # Where this link's OPENING CALL will be answered. The loop appends the assistant
        # turn, runs the tools (we are inside one of them now), then appends one results turn —
        # so `len(messages)` is the index that results turn takes. `_compact_coding_span`
        # rewrites the answer there rather than cutting from it, which is what keeps the
        # opening call paired; a cut starting here orphaned it and 400'd the next call.
        self.mark = len(self.messages)
        # The brief goes to the coder in the BACKGROUND, so filing `author_chain` returns at once
        # and the lead keeps its turn — which is what §9.3 promises and what this line used to
        # break. Measured 2026-09-12: a blocking seed held the lead still for 263 s, and because
        # no background task existed the status tool reported "you have not sent the coder anything
        # yet" to a lead whose coder had just worked for four minutes — advice that would have had
        # it re-send the whole brief. The first turn's narration was discarded here too, so the
        # lead re-read five files by hand to learn what its own coder had already told it.
        # A session predating B (a benchmark's, a test double's) has no `start_drive`; it keeps
        # the blocking call, as `execute_coder_message` does for the same reason.
        if hasattr(session, "start_drive"):
            session.start_drive(session.initial_message)
        else:
            await session.drive(session.initial_message)

    async def _close_current(self) -> None:
        """Record the finished link's result, compact its coding span, release the session."""
        if self.session is None:
            return
        session, spec = self.session, self.spec
        self.session = self.spec = None
        self.cycle.session_open = False
        result = _authoring_result(session, spec)
        self.outcome.authored.append(result)
        if result.status == "committed":
            self.outcome.committed += 1
        # Both arms compact. A rejection used to be kept whole on the grounds that it is what
        # the next brief has to be written against (B6) — true, and it is the summary's job to
        # carry it, which is why the failure wording ends by handing the choice back to the
        # lead. The raw transcript stays in the run ledger for a human.
        span = self.messages[self.mark + 1:]
        summary = await self._outcome_for(spec, result, span, session)
        self.messages[:] = _compact_coding_span(self.messages, self.mark, summary)
        await self.lead._record(
            "analyst.tool.compacted", level=6,
            summary=f"Coding span compacted: {spec.proposed_tool_name}",
            data={
                "tool": spec.proposed_tool_name,
                "status": result.status,
                "turns_dropped": len(span),
                "outcome": summary,
            },
        )
        await self.lead._record(
            "analyst.tool.authored", level=5,
            summary=f"Tool authoring {result.status}: {spec.proposed_tool_name}",
            data={"result": result.model_dump(mode="json")},
        )
        self.provider.authoring_session = None
        if (
            self.lead.run_control is not None
            and self.lead.run_control.authoring_session is session
        ):
            self.lead.run_control.authoring_session = None
        await session.aclose()

    async def _outcome_for(self, spec: Any, result: Any, span: list, session: Any) -> str:
        """What the opening call answers with once its link is decided.

        Two halves, and the split is the whole safety argument. The **facts** — outcome, commit
        identifier, submissions spent, credits left — are read off the session and the result
        and written here, in Python, because this text is the lead's only view of the link and
        its next brief is written against it. The **narrative** is a summary of the coding span
        by a cheap model, which can therefore be thin or wrong without putting a false commit or
        a false verdict in front of the lead.
        """
        name = spec.proposed_tool_name
        narrative = await self._narrative_for(span)
        used, cap = self._submissions_spent(session)
        spent = f" Review submissions used: {used} of {cap}." if cap else ""
        credits = f" Author credits left: {self.outcome.author_budget}."
        if result.status == "committed":
            sha = (result.commit_sha or "")[:10]
            return (
                f"You dispatched {name}. {narrative} COMMITTED {sha} — the gate passed and the "
                "independent reviewer approved, so the tool is on the authoring branch and "
                f"becomes callable once the workers rerun.{spent}{credits}"
            ).replace("  ", " ")
        reason = (result.reason or "no mergeable tool was produced").rstrip(".")
        return (
            f"You dispatched {name}. {narrative} NOT MERGEABLE: {reason}.{spent}{credits} With "
            "demonstrated failure modes in mind, you may choose to either proceed without this "
            "tool, or re-investigate, re-plan and retry with caution."
        ).replace("  ", " ")

    @staticmethod
    def _submissions_spent(session: Any) -> tuple[int, int]:
        """Submissions used and the cap, read off the session this link ran on."""
        return (
            int(getattr(session, "submits_used", 0) or 0),
            int(getattr(session, "submit_cap", 0) or 0),
        )

    async def _narrative_for(self, span: list) -> str:
        """One paragraph on the coding span, from the `compactor` role; "" if it cannot be had.

        A compaction that fails leaves the facts standing alone rather than leaving the opening
        call unanswered — an empty narrative costs the lead detail, an absent answer costs the
        run (the malformed-request class this whole cut is shaped around).
        """
        rendered = _render_span(span)
        if not rendered.strip():
            return ""
        try:
            from auto_research.models import resolve

            handle = resolve("compactor").handles[0]
            response = await handle.call(
                [
                    ModelMessage(role="system", content=load_prompt("compactor")),
                    ModelMessage(role="user", content=rendered),
                ]
            )
            return " ".join((response.content or "").split())
        except Exception as exc:  # noqa: BLE001 — a thin summary beats a dead run
            await self.lead._record(
                "analyst.tool.compactor_failed", level=3,
                summary="Compacting the coding span failed; reporting the facts alone",
                data={"error": repr(exc)},
            )
            return ""

    async def close(self) -> None:
        """Release whatever is still open when the cycle ends, however it ended."""
        await self._close_current()


class AnalystLeadAgent(AgentRole):
    """Analyst lead role: adjudicate worker reports into one report."""

    role = "analyst_lead"
    # A3: the lead's report carries its decision, so its surface adds `rework`/`rationale`/
    # `what_to_probe` and the `needs_human` status, and drops `tool_request` (it authors rather
    # than asks). Declared here because the shared workspace builder reads it off the role.
    report_fields = LEAD_REPORT_FIELDS
    report_status_values = LEAD_STATUS_VALUES

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
        # Provenance: the run's ReceiptStore (set by runtime). The lead may cite its own reads and
        # every receipt a worker report it received cites (with their ancestors).
        self.receipts: ReceiptStore | None = None
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
            # A3 — and the reason this is spelled out on BOTH builders: the lead overrides the
            # base one, so setting the surface only there left the lead with the worker's fields
            # and no way to express its own decision. A live lead found it and said so in its own
            # report: "the schema I am given has no rework field, so the decision is carried in
            # prose." Read off the role, so the two builders cannot disagree.
            prose_fields=self.report_fields,
            status_values=self.report_status_values,
            receipts=_receipt_scope(
                self.receipts, self.agent_key or "analyst_lead", source_reports.values()
            ),
            receipt_source=self.agent_key or "analyst_lead",
        )

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
        """Run one analysis cycle as ONE ReAct loop over one conversation (A7).

        This is the lead's whole turn, and it has no stages. The lead investigates, briefs a coder
        chain if the analysts hit a missing capability, drives and submits each link, writes its
        report, and ends by calling ``turn_end`` — all in a single loop over a single
        conversation. **Its report is its decision**, the same contract an analyst already writes:
        ``rework`` sends the analysts back, otherwise the report is the analysis result.

        What this replaced, and why. The lead used to state decisions by *filing a form*, and a
        filed form ended the round. While authoring, the only two forms it could file were both
        whole-cycle terminals — so "this link is dead, try the next" was inexpressible, and a lead
        that ran a link's CI budget out had no action that meant anything else. Slot 1 of the
        2026-09-11 batch ended a whole analysis with **11 of 12 author credits unspent**; slot 2,
        from the identical position, spent a debate round and a full worker rerun to say the same
        thing. One missing action, two different scars. The chain now advances on a *tool result*
        (``LeadCycle.advance_chain``), so a dead link is a sentence in the conversation rather than
        the end of the turn.

        **Python still owns the counting.** ``rounds_left`` bounds the debate and is enforced at
        ``turn_end``; ``author_budget`` bounds authoring attempts; every session lifecycle and
        every git operation stay here (§9.9); the caller reloads the registry and reruns the
        workers. The loop chooses *what* to do; this decides *how many times*.

        **The coding middle is dropped at its own boundary**, per link, keeping the brief and the
        submit result with its real commit SHA — whole turns only, nothing synthesized. Safe here
        because the lead's tools are all resident (``ReadOnlyToolProvider`` defers none), so no
        dropped turn can de-activate a discovered tool (§13 watch item (a)).

        Returns a :class:`LeadCycleOutcome`. ``report=None`` means the lead never wrote one, which
        the caller treats as the failure it is — nothing is composed on its behalf ([F6]).
        """
        self._round += 1
        source_keys, source_reports, all_figures = self._accumulate_figures(reports, sources)
        _mint_task_input(self.receipts, task)
        workspace = self._make_report_workspace(source_reports)
        authoring_possible = (
            toolkit_root is not None
            and author_budget > 0
            and any(r.tool_request is not None for r in reports)
        )
        cycle = LeadCycle(authoring_enabled=authoring_possible, rounds_left=rounds_left)
        outcome = LeadCycleOutcome(filed=None, author_budget=author_budget)
        if self.tools is None:
            # No tool surface: nothing to investigate with and no way to write a report. The
            # honest answer is the failure, not a composed report ([F6]).
            return outcome

        payload = {
            "task": task.model_dump(mode="json"),
            "worker_reports": [report.model_dump(mode="json") for report in reports],
            "worker_sources": source_keys,
            "available_toolkits": list(self.available_toolkits),
            "your_turn": _cycle_instruction(cycle, reports, rounds_left),
        }
        messages: list = [
            ModelMessage(role="system", content=self.prompt),
            ModelMessage(role="user", content=_payload_text(payload)),
        ]
        provider = self.tools
        previous_ws = getattr(provider, "report_workspace", None)
        previous_cycle = getattr(provider, "cycle", None)
        previous_validator = getattr(provider, "turn_end_validator", None)
        provider.report_workspace = workspace
        provider.cycle = cycle
        provider.turn_end_validator = lambda: lead_turn_problems(cycle, workspace)

        driver = _ChainDriver(
            lead=self,
            provider=provider,
            cycle=cycle,
            outcome=outcome,
            messages=messages,
            toolkit_root=toolkit_root,
            worktree_base=worktree_base,
            direct_branch=direct_branch,
            colead=colead,
            human_handler=human_handler,
            coder=coder,
            allow_correlated_review=allow_correlated_review,
        )
        cycle.advance_chain = driver.advance

        try:
            await self._react(
                seed_messages=messages,
                share_messages=True,
                tools=provider,
                action="run_cycle",
                cap=_react_loop_cap(),
                images=all_figures,
                free_tools=_CYCLE_FREE_TOOLS,
                absolute_cap=_react_loop_absolute_cap(),
                require_terminal=True,  # no escape path: turn_end is the only exit
                terminal_tools=frozenset({TURN_END_TOOL}),
            )
        finally:
            await driver.close()
            provider.report_workspace = previous_ws
            provider.cycle = previous_cycle
            provider.turn_end_validator = previous_validator

        report = _written_report(workspace)
        outcome.report = report
        outcome.filed = TURN_END_TOOL if report is not None else None
        outcome.decision = cycle.brief
        if report is not None:
            self._last_report = report
        return outcome

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
        loop over it: the lead steers the Sonnet coder (``message`` to ``coder``) and submits to the
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
            authoring_session=session,
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
                "Inspect the coder's work with read_file/list_dir. Steer it with message to the coder, "
                "giving concrete algorithm-level instructions (signature, exact procedure, edge "
                "cases, the tests it must add). When you believe the tool and its tests are right, "
                "call submit_for_review: it runs the deterministic gate, then the independent GPT "
                "co-lead review, and commits automatically on gate-pass AND review-pass — you never "
                "self-approve. On a gate failure (mechanical), fix the underlying cause by messaging "
                "the coder; never weaken a check to pass. On a review rejection, either steer the "
                "coder to address it (message to the coder) or, when you believe a challenge is "
                "unfounded or "
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
        free = frozenset({SUBMIT_TOOL, RESPOND_TO_REVIEW_TOOL, WAIT_FOR_REPLY_TOOL})
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


#: How many consecutive silent turns one loop will nudge through before giving up. Small on
#: purpose: a continuation is for a model that stopped by accident, and a model that keeps
#: returning nothing is not going to start. The loop's own budget still bounds everything else.
#: What a role is told when it stops calling tools without having finished.
#:
#: States the channel rule first, because that is the actual mistake: a turn with no tool call
#: reaches the harness as nothing at all, however much text it carried. Then the three routes
#: out, so the model is told what to DO rather than only what it failed to do. Human wording,
#: 2026-09-15, after a worker lost a completed 14-quantity reproduction to a 58-character turn:
#: **the normal exit is the only allowed exit.**
_TERMINAL_REQUIRED_NUDGE = (
    "Your actions and conclusions only reach the harness as tool calls — a turn without one "
    "arrives as nothing, whatever text it carried, and cannot end your turn. To investigate "
    "further, call the tool you need. To record or revise your conclusions, call edit_report, "
    "and review_report to read back what you have written. When your report is complete, end "
    "your turn with turn_end: composing the report and submitting it that way is the only way "
    "your work is concluded, and nothing is written on your behalf if you stop any other way."
)

class ReactBudgetExhausted(RuntimeError):
    """A role used its whole budget AND its grace window without filing a terminal form.

    Fatal by design (CLAUDE.md §2b): exhausting a cap is not an exit. The alternative — returning
    an unfiled result — is indistinguishable in the ledger from a role that was never asked to
    run, and it is how a completed 14-quantity reproduction was discarded on 2026-09-15.
    """


#: Said once, at budget exhaustion, before the grace window opens.
_GRACE_OPENED = (
    "You have run out of rounds. Summarise your work within {grace} rounds, using edit_report "
    "and then turn_end. Report what you actually have, including what is incomplete — an honest "
    "partial is a correct outcome. Work that is not filed this way is not recorded at all."
)

#: Appended to every tool result inside the grace window, so the countdown is unmissable.
_GRACE_REMAINING = "\n\n[{remaining} grace round(s) left — file with edit_report then turn_end.]"

_SILENT_TURN_CAP = 2


def _coder_event(tools: Any) -> str | None:
    """A one-line notice that the background coder stopped, or ``None`` while it works (B4).

    Read from the provider's live session rather than pushed by it, because the loop is the only
    place that knows when a turn boundary is safe. The narration itself is left for ``wait_for_reply``
    (which returns it at once when it is unread) — a notice is a nudge to look, not the looking —
    which also keeps a long coder transcript out of the injected turn. A lead parked in
    ``wait_for_reply`` consumes the narration itself, so this fires only for a lead busy elsewhere.
    """
    session = getattr(tools, "authoring_session", None)
    if session is None or getattr(session, "drive_status", None) != "stopped":
        return None
    if getattr(session, "_drive_result", None) is None and getattr(session, "_drive_error", None) is None:
        return None  # already reported; nothing new to say
    name = getattr(getattr(session, "tool_spec", None), "proposed_tool_name", "the tool")
    failed = getattr(session, "_drive_error", None) is not None
    if failed:
        return (
            f"[coder] the session on '{name}' FAILED and is not usable. Call wait_for_reply for the "
            "fault, then write your report and say what happened."
        )
    return (
        f"[coder] the coder working on '{name}' has stopped. Call wait_for_reply to read what it "
        "did (it returns at once), then inspect the tree or submit_for_review."
    )


#: Provider fields a role installs for one loop and resets after it; a resumed turn reinstalls the
#: values its loop ended with (the lead and reviewer restore them in a ``finally``).
_KEPT_PROVIDER_STATE = (
    "report_workspace", "turn_end_validator", "receipts", "receipt_source", "extra_tools", "cycle",
)
#: Tools a resumed turn answers without running: the report is filed and final, and the cycle a
#: lead's authoring tools belonged to has ended.
_RESUME_CLOSED = frozenset(
    {EDIT_REPORT_TOOL, "file_edit", AUTHOR_CHAIN_TOOL, SUBMIT_TOOL,
     RESPOND_TO_REVIEW_TOOL}
)


def _has_authoring_surface(tools: Any) -> bool:
    """Whether this loop's provider can author: a live session, or a cycle that enables it."""
    declared = getattr(tools, "_authoring_declared", None)
    return getattr(tools, "authoring_session", None) is not None or bool(
        callable(declared) and declared()
    )


def _seat_scope(tools: Any) -> Any | None:
    """The receipts this seat may cite — the librarian's own scope, else its report workspace's."""
    scope = getattr(tools, "scope", None)
    if scope is not None:
        return scope
    return getattr(getattr(tools, "report_workspace", None), "receipts", None)


class ChannelTools:
    """A seat's tool provider with the quick channel added (E12).

    Wraps the provider for one loop: declares ``message``, ``view_receipt`` and (when the provider
    does not already) ``wait_for_reply``; holds ``turn_end`` while a member this seat resumed is
    still in its turn; and, in a **resumed** turn, declares exactly the set the finished turn ended
    with (a declared set never shrinks mid-conversation) while answering the tools that would
    re-file. Everything else is the provider's, read through unchanged.
    """

    def __init__(
        self, base: Any, *, role: "AgentRole", resumed: bool = False,
        declared: list[dict] | None = None,
    ) -> None:
        self._base = base
        self._role = role
        self.resumed = resumed
        self._declared = declared

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    def _coder_declared(self) -> bool:
        """The lead's ``coder`` address: declared for the whole cycle authoring is enabled in (or
        the whole standalone authoring loop), never with the live session — a declared set that
        shrinks mid-conversation is the 2026-07-29 run-killer."""
        declared = getattr(self._base, "_authoring_declared", None)
        return getattr(self._base, "authoring_session", None) is not None or bool(
            callable(declared) and declared()
        )

    def _directory(self) -> list[str]:
        directory = self._role.channel.directory(self._role.agent_key)
        return directory + (["coder"] if self._coder_declared() else [])

    def tool_definitions(self) -> list[dict]:
        if self._declared is not None:
            return list(self._declared)
        defs = list(self._base.tool_definitions())
        names = {d.get("name") for d in defs}
        router = self._role.channel
        defs.append(message_tool_def(self._directory()))
        defs.append(list_peers_tool_def())
        defs.append(view_receipt_tool_def())
        if WAIT_FOR_REPLY_TOOL not in names:
            defs.append(wait_for_reply_tool_def())
        return defs

    async def _to_coder(self, arguments: Mapping[str, Any]) -> ToolOutcome:
        """The lead's text to its coder: the session's one send path, labelled with the sender."""
        session = getattr(self._base, "authoring_session", None)
        if self.resumed or not self._coder_declared():
            return ToolOutcome(
                text="The coder is reachable only from your own authoring turn.", is_error=True
            )
        if session is None:
            no_session = getattr(self._base, "_no_session_text", lambda: "No coding session is open.")
            return ToolOutcome(text=no_session(), is_error=True)
        key = self._role.agent_key or self._role.role
        text = str(arguments.get("text") or "")
        outcome = await execute_coder_message(
            session, f"[Message from {key}] {text}" if text.strip() else "",
            mid_turn=bool(arguments.get("mid_turn")),
        )
        await self._role.channel.record_message(
            key, "coder", text, route="coder", is_error=outcome.is_error
        )
        return outcome

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        role, router = self._role, self._role.channel
        key, control = role.agent_key, role.run_control
        if name == MESSAGE_TOOL and str(arguments.get("to") or "") == "coder":
            return await self._to_coder(arguments)
        if name == MESSAGE_TOOL:
            scope = _seat_scope(self._base)
            text, is_error = await router.send(
                key or role.role, str(arguments.get("to") or ""), str(arguments.get("text") or ""),
                handles=[str(h) for h in (arguments.get("attachments") or [])],
                scope=scope,
                receipt_source=getattr(scope, "own_source", None)
                or getattr(self._base, "receipt_source", None),
                reply_dir=_reply_dir_for(getattr(self._base, "cwd", None) or getattr(role, "report_dir", None)),
                kind=str(arguments.get("kind") or "clarify_science"),
                model=_model_label(role.model) or "",
            )
            return ToolOutcome(text=text, is_error=is_error)
        if name == LIST_PEERS_TOOL:
            listed = execute_list_peers(router, key, _model_label)
            if not self._coder_declared():
                return listed
            status = getattr(getattr(self._base, "authoring_session", None), "drive_status", None)
            state = f"its session is {status}" if status else "no coding session is open yet"
            return ToolOutcome(text=listed.text + (
                f"\n- coder — the coding agent writing the tool you briefed; {state}."
            ))
        if name == VIEW_RECEIPT_TOOL:
            return await execute_view_receipt(
                _seat_scope(self._base), router.viewable(key), router.store, arguments,
                images_ok=bool(getattr(getattr(role.model, "config", None), "vision", True)),
            )
        if name == WAIT_FOR_REPLY_TOOL:
            session = getattr(self._base, "authoring_session", None)
            if not self.resumed and session is not None and hasattr(session, "wait_for_event"):
                return await self._base.execute(name, arguments)  # the coder is working: its wait
            return await execute_wait_for_reply(
                control, key, arguments, outstanding=lambda: router.outstanding(key)
            )
        if name == TURN_END_TOOL:
            pending = control.resumed_by(key) if control is not None else []
            if pending:
                return ToolOutcome(
                    text=(
                        f"turn_end was not accepted yet: {', '.join(pending)} is still working on "
                        "your message. Call wait_for_reply, then turn_end once it has ended."
                    ),
                    is_error=True,
                )
            if self.resumed:
                return ToolOutcome(text="Turn ended. Your filed report stands as filed.")
        if self.resumed and name in _RESUME_CLOSED:
            return ToolOutcome(
                text=(
                    f"{name} is closed in this turn: your report is filed and final, and this turn "
                    "is for reading and answering messages. Answer with message, then call "
                    "turn_end."
                ),
                is_error=True,
            )
        return await self._base.execute(name, arguments)


def _last_turn_has_unresolved_server_tool(messages: list) -> bool:
    """Is the most recent provider-authored turn mid-server-tool? (A2)

    Scans backwards for the last :class:`ModelResponse` — our own ``ModelMessage`` and
    ``ToolResultMessage`` turns sit between it and here — and reads the flag its adapter
    normalised. The provider-specific fact stays in the provider; this only asks.

    ``False`` when no provider turn exists yet (the first iteration, where the conversation is
    still the seed) and for any turn an adapter did not capture, which is the honest answer for a
    turn nobody received.
    """
    for message in reversed(messages):
        if isinstance(message, ModelResponse):
            return bool(getattr(message, "has_unresolved_server_tool", False))
    return False


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
    """Tool budget (model turns) for the librarian's research loop.

    Key: ``librarian_react_loop_cap``, falling back to ``react_loop_cap``. Reading is paged, so a
    paper read in full costs several turns; report authoring and ``turn_end`` are budget-free.
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


def _reply_dir_for(agent_folder):
    """Files a human sends land beside the ledger, never in the agent's writable folder (F22)."""
    if agent_folder is None:
        return None
    from auto_research.sandbox.workspace import human_reply_dir

    return human_reply_dir(agent_folder)


def _environment_brief(sandbox) -> dict | None:
    """Name, Python version and fingerprint of the environment a worker's tools run in."""
    environment = getattr(sandbox, "environment", None)
    if environment is None:
        return None
    try:
        from auto_research.sandbox.envs import describe

        return describe(environment)
    except Exception:  # noqa: BLE001 — an unreadable env leaves the brief out, never the turn
        return {"name": environment.name}
