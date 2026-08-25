"""Which author harness may drive which vendor, and whether this machine can run it.

The ``coder`` role names both a model and the harness that drives it (CLAUDE.md §6/§9.3). A
harness speaks exactly one wire dialect, so not every pairing exists: the Claude Agent SDK talks
the Anthropic ``/v1/messages`` dialect, the Codex app-server talks the OpenAI Responses API, and a
vendor is reachable by a harness only if it publishes an endpoint in that dialect. The legal pairs
are therefore a table, with each row's endpoint taken from that vendor's own documentation, and
both checks below run at launch before any model is constructed.

Verified against vendor documentation on 2026-08-19:

* DeepSeek, Responses — ``https://api.deepseek.com/``
  (api-docs.deepseek.com/guides/responses_api and its Codex integration page, which specifies
  ``wire_api = "responses"``; stateless, so no ``previous_response_id``).
* Z.AI GLM, Responses — ``https://api.z.ai/api/v1`` (docs.z.ai/devpack/tool/codex, which states
  Codex requires a dedicated Responses endpoint).
* Alibaba Model Studio (Qwen), Responses — ``https://dashscope-intl.aliyuncs.com/compatible-mode/v1``
  (the Singapore pay-as-you-go host of ``.../model-studio/base-url``). **Confirmed correct
  2026-08-24** after being doubted: the key class is what decides the host, and only a
  pay-as-you-go key belongs here. The Coding Plan lives at
  ``coding-intl.dashscope.aliyuncs.com/v1``, where ``/responses`` **404s** — that plan serves Chat
  Completions only, so it can drive neither this harness nor the chat adapter, which generalises
  the ``sk-sp-`` trap below to its ``sk-mr-`` keys.

Two absences are structural: Anthropic publishes no Responses endpoint, and OpenAI no
Anthropic-dialect one.

Not every present row is proven: :data:`UNVERIFIED_PAIRINGS` marks the ones this project has
never run against the real service, and ``check_coder_harness_supported`` warns at launch when the
configured coder is one of them.

**Three further pairs are documented by their vendors but deliberately absent here** (2026-08-19):
DeepSeek, Z.AI and Alibaba Model Studio each publish an **Anthropic-dialect** endpoint intended for
Claude Code (``/anthropic``, ``/api/anthropic``, ``/apps/anthropic``), so ``claude_sdk`` could in
principle drive those models. The backend does not wire it: ``claude_sdk`` sets no
``ANTHROPIC_BASE_URL``/``ANTHROPIC_AUTH_TOKEN`` and no ``ANTHROPIC_DEFAULT_*_MODEL`` mapping, so a
member naming one of those pairs would silently author on Claude instead. The table lists only what
the code can do; add the row together with the wiring, never before it.
"""

from __future__ import annotations

import os

from auto_research.models.types import RoleModelConfig


# (provider, harness) -> the endpoint that vendor documents for that harness, or None when the
# harness is native to it and needs no override. A pair absent from this table does not exist.
HARNESS_ENDPOINTS: dict[tuple[str, str], str | None] = {
    ("anthropic", "claude_sdk"): None,
    ("openai", "codex"): None,
    ("deepseek", "codex"): "https://api.deepseek.com/",
    ("zai", "codex"): "https://api.z.ai/api/v1",
    # NOT LIVE TESTED — see UNVERIFIED_PAIRINGS below.
    ("dashscope", "codex"): "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
}

# Pairings the vendor documents and this project has **never exercised live**. Configuring one is
# allowed — the endpoint and the wire dialect are right — but nothing here has been proven against
# the real service, so the launch says so rather than implying coverage it does not have.
#
# ``("dashscope", "codex")``: every attempt returned HTTP 400 ``AccessDenied.Unpurchased``
# ("Access to app denied. Please make sure you are eligible for using the app") on 2026-08-19,
# across three models and all three documented hosts. The key was recognised by the
# pay-as-you-go host (the CN and token-plan hosts answered ``InvalidApiKey``), so the wiring was
# right and the account was not entitled: QwenCloud gates pay-as-you-go on a *complete* billing
# profile (payment method **plus** name, address and phone verification), not on buying credits.
# Two traps for whoever finishes this: the Coding Plan issues ``sk-sp-…`` keys on an endpoint that
# speaks Chat Completions only, which current Codex versions refuse; and the Token Plan uses both
# a different key and a different host, so its ``base_url`` differs from the row above.
UNVERIFIED_PAIRINGS: dict[tuple[str, str], str] = {
    ("dashscope", "codex"): (
        "never exercised live: as of 2026-08-19 the account was not entitled "
        "(HTTP 400 AccessDenied.Unpurchased on every model and host). The wiring is the endpoint "
        "Alibaba Model Studio documents for Codex, but nothing has run through it"
    ),
}

HARNESS_DIALECT: dict[str, str] = {
    "claude_sdk": "the Anthropic /v1/messages dialect",
    "codex": "the OpenAI Responses API",
}


def coder_member() -> RoleModelConfig:
    """The single configured ``coder`` member.

    One harness drives one model, so the role carries exactly one member; a second would be
    silently ignored by everything that reads ``handles[0]``.
    """
    from auto_research.models import resolve

    handles = resolve("coder").handles
    if len(handles) != 1:
        raise ValueError(
            f"the coder role has {len(handles)} members; it takes exactly one, because one "
            "author harness drives one model. Keep the member you want in "
            "`config/models.yaml: roles.coder.members` and remove the rest."
        )
    return handles[0].config


def check_coder_harness_supported() -> None:
    """Launch-time preflight: the coder's (provider, harness) pair exists and is configured.

    A pure config check, so a run authoring with an impossible pairing (or with an endpoint or
    API-key variable missing) fails at launch instead of when the author process is spawned.
    """
    member = coder_member()
    harness = member.harness
    if harness is None:
        legal = ", ".join(sorted({h for _, h in HARNESS_ENDPOINTS}))
        raise ValueError(
            f"the coder member {member.name!r} names no harness. Add `harness: <{legal}>` to it "
            "in `config/models.yaml`: the harness is what drives the author process, and it is "
            "not inferable from the model id."
        )

    key = (member.provider, harness)
    if key not in HARNESS_ENDPOINTS:
        supported = ", ".join(
            f"{provider}+{name}" for provider, name in sorted(HARNESS_ENDPOINTS)
        )
        raise ValueError(
            f"the coder pairing {member.provider}+{harness} does not exist: {harness} speaks "
            f"{HARNESS_DIALECT.get(harness, 'one wire dialect')} and {member.provider} publishes "
            f"no endpoint in it. Supported pairings: {supported}."
        )

    documented = HARNESS_ENDPOINTS[key]
    if documented is not None and not member.base_url:
        raise ValueError(
            f"the coder member {member.name!r} pairs {member.provider} with {harness}, which "
            f"needs an explicit endpoint. Add `base_url: {documented}` (the endpoint "
            f"{member.provider} documents for {harness}) to it in `config/models.yaml`."
        )

    if member.key_env and not os.environ.get(member.key_env):
        raise ValueError(
            f"the coder member {member.name!r} authenticates from ${member.key_env}, which is "
            f"unset. Add {member.key_env} to `.env` (and to `.env.example` as a documented "
            "requirement) before authoring with this member."
        )

    # Configured correctly, but never proven against the real service. The launch continues and
    # says so, so a first-time failure reads against a known gap instead of looking like a defect
    # in the authoring flow.
    unverified = UNVERIFIED_PAIRINGS.get(key)
    if unverified:
        from auto_research.preflight import PreflightWarning

        raise PreflightWarning(
            f"the coder pairing {member.provider}+{harness} is {unverified}. Authoring on it is "
            "a first run, not a repeat of a tested path."
        )


def check_coder_harness_available() -> None:
    """Launch-time preflight: the configured harness can actually be launched here.

    Both harnesses drive a CLI subprocess, so a missing binary or an uninstalled SDK is a
    launch-time fact. Discovering it mid-run costs an authoring attempt; discovering it in a
    batch of trials costs one dead session per trial.
    """
    member = coder_member()
    if member.harness == "claude_sdk":
        # Ask the SDK where its own CLI is rather than probing PATH: recent versions ship a
        # bundled binary that `which` cannot see, so a PATH probe rejects a working setup.
        try:
            from claude_agent_sdk._internal.transport.subprocess_cli import (
                SubprocessCLITransport,
            )

            SubprocessCLITransport._find_cli(
                SubprocessCLITransport.__new__(SubprocessCLITransport)
            )
        except Exception as exc:  # noqa: BLE001 — the SDK's own message names the fix
            raise ValueError(f"Claude Agent SDK CLI unavailable: {exc}") from exc
        return

    if member.harness == "codex":
        try:
            import openai_codex  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                "the Codex author harness needs the `openai-codex` SDK: "
                f"{exc}. It is pinned in environment.yml's pip section; run "
                "`conda env update -n auto_research -f environment.yml`."
            ) from exc
        from auto_research.sandbox.coder.codex_bin import resolve_codex_bin

        resolve_codex_bin()  # raises with the fix when the binary is absent
