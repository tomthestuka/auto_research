"""The sweep roster: which author, which reviewer, and what each leg is for.

One leg is one (author member, reviewer arrangement) pair, run K times against the same frozen
brief with everything else byte-identical. Two comparisons live here and they cannot share rows:

**Tier rows** hold the reviewer fixed and vary the author. The reviewer must therefore be the same
member in every tier row, which is why no tier row may be an OpenAI author: the configured co-lead
is OpenAI, so an OpenAI author would force the reviewer to change and confound the axis.

**Decorrelation rows** hold the author fixed at the campaign default and vary only the reviewer,
which is the comparison ``papers/FANGZ`` §6.2 asks for. The cross-provider arm is the production
shape and is already measured by the ``claude_sonnet_5`` tier row, so it is declared here as a
reuse rather than re-run.

Models are named per vendor at the tier each vendor's own lineup calls balanced, not at its
flagship, so the rows compare like with like. **One known exception, recorded rather than silently
carried:** ``glm-5.3`` sits at the flagship tier on public benchmarks (verified 2026-08-21: 31.4 per
cent against Claude Opus 4.8's 29.5 on Z.ai Code Bench at high effort, and the highest recorded
KingBench 3 coding score, ahead of Opus 5). So the Z.AI row is a **top-tier** author, and prose
comparing it to the others has to say so.

Dropping to ``glm-5.2`` does not fix that, and was considered and declined (2026-08-21). Z.AI
charges **the same** $1.40/$4.40 per million for both models on its own API, which is the endpoint we
author against, so the swap buys no cost saving; 5.2 is cheaper only through resellers. Nor does it
buy tier parity, since 5.2 sits within four points of Opus 4.8 on Terminal-Bench 2.1. The leg stays
defined for a future comparison, but the informative quantity there is **tokens per trial, not
rate**: the measured GLM trial cost several times a DeepSeek one at the same order of $/token, and
5.3's published advantage is token efficiency, so a less efficient 5.2 could cost *more* per trial. ``gpt-5.6-sol`` never appears as an author: it is the
co-lead's own model, and an author sharing it would confound the decorrelation rows with a
same-model effect on top of the same-provider one.

Prices are here only for vendors litellm's map does not carry. A hand-copied rate that shadows a
maintained one is a liability, so Anthropic, OpenAI and DeepSeek are deliberately absent.
"""

from __future__ import annotations

from dataclasses import dataclass

from auto_research.models.types import RoleModelConfig

# Vendor brand colours, for the sweep figure. Colour never carries meaning alone there (each
# vendor also gets its own terminal marker and a direct label), so these are identity, not scale.
BRAND_COLOURS: dict[str, str] = {
    "anthropic": "#d97757",   # clay orange
    "openai": "#0b0b0b",      # near-black
    "deepseek": "#4d6bfe",    # indigo
    "zai": "#12a594",         # teal
    "dashscope": "#7c5cd6",   # purple (absent from the roster; see QWEN_ABSENT)
}

# Published per-million-token rates for the models litellm does not price. Read from each vendor's
# own pricing page 2026-08-19; Qwen's page renders its table by script and could not be read
# directly, so its figures are aggregator numbers and it is not in the roster anyway.
_PRICES: dict[str, dict[str, float]] = {
    "glm-5.3": {"input": 1.4, "output": 4.4, "cache_read": 0.26},
    "glm-5.2": {"input": 1.4, "output": 4.4, "cache_read": 0.26},
}

# Codex settings a vendor requires. `glm-5.3` fails every turn without an explicit reasoning
# effort ("This model always engages in thinking and cannot be disabled; please use low, high, or
# max"), and Z.AI's own Codex page notes its default level is max.
_PARAMETERS: dict[str, dict[str, object]] = {
    "glm-5.3": {"model_reasoning_effort": "high"},
    "glm-5.2": {"model_reasoning_effort": "high"},
}

# Why the Qwen row is absent rather than substituted. Stated here because the paper reports the
# absence, and a reader has to be able to tell a skipped vendor from an unconsidered one.
QWEN_ABSENT = (
    "Alibaba Model Studio (qwen) is configured and wired but was never entitled: as of 2026-08-19 "
    "every attempt returned HTTP 400 AccessDenied.Unpurchased across three models and all three "
    "documented hosts, the pay-as-you-go host recognising the key while the others rejected it. "
    "That is a vendor billing-profile gate, not our wiring. No model is substituted for it."
)


@dataclass(frozen=True)
class Leg:
    """One row of the sweep: an author, a reviewer arrangement, and what it measures."""

    slug: str
    """Short identifier; names the leg's output directory and its column in the report."""

    provider: str
    harness: str
    model: str
    purpose: str
    """``tier`` (vary the author, reviewer fixed), ``decorrelation`` (vary the reviewer), or
    ``lead`` (vary the analyst lead, which panel (b) does to separate the lead's correlation with
    the reviewer from the author's)."""

    key_env: str | None = None
    base_url: str | None = None
    colead_provider: str | None = None
    """Provider the co-lead must come from. ``None`` = the configured decorrelated routing."""

    colead_model: str | None = None
    """Exact co-lead model, when the leg pins one rather than taking the role's first member."""

    lead_provider: str | None = None
    lead_model: str | None = None
    """The analyst lead this leg runs under. ``None`` takes the configured role, which is the
    campaign default and every panel-(a) row.

    The lead is a separate correlation axis from the author, and until it is varied the two cannot
    be told apart. The lead writes the instructions the coder executes, decides when the work is
    submitted, and argues the co-lead's verdict, so it has more influence over what reaches a
    review than the coder does. It is Anthropic in every panel-(a) leg, which means the arm whose
    co-lead is also Anthropic correlates the reviewer with the coder **and** the lead at once, and
    a change in its rejection count cannot be attributed to either alone."""

    reuse: str | None = None
    """Set when this row is already measured by another leg or an earlier batch, with the reason.
    A reuse row is reported but never run, which is what keeps the free arms free."""

    note: str = ""
    trials: int = 10

    in_default: bool = True
    """Whether a bare batch runs this leg. ``False`` keeps a leg addressable by ``--legs`` without
    adding its K trials, and their cost, to every default run."""

    max_concurrency: int | None = None
    """Ceiling on trials in flight for this leg, when the vendor imposes one the batch must
    respect. Set for the Claude harness because its author authenticates from a subscription whose
    session cap is shared across concurrent coders: four at once exhausted it after about two hours
    and contaminated six of ten trials in the first reliability batch, with the coder emitting the
    limit as ordinary text and the lead then submitting work that did not exist. Alt-vendor legs
    authenticate from their own API keys and do not share that cap."""

    @property
    def correlated(self) -> bool:
        """Whether this leg deliberately shares a provider between author and reviewer."""
        return self.colead_provider is not None and self.colead_provider == self.provider

    @property
    def lead_correlated(self) -> bool:
        """Whether the reviewer shares a provider with the lead that submits the work.

        Reported separately from :attr:`correlated` because §9.3's rule binds the author and says
        nothing about the lead, so a leg can pass the decorrelation preflight while the reviewer
        and the actor deciding submission come from one vendor.
        """
        lead = self.lead_provider or "anthropic"      # the configured lead is Anthropic
        colead = self.colead_provider or "openai"      # the configured co-lead is OpenAI
        return lead == colead

    @property
    def colour(self) -> str:
        return BRAND_COLOURS.get(self.provider, "#52514e")

    def member(self) -> RoleModelConfig:
        """The author member this leg hands to the authoring flow."""
        return RoleModelConfig(
            name=f"sweep_{self.slug}",
            provider=self.provider,
            model=self.model,
            harness=self.harness,
            base_url=self.base_url,
            key_env=self.key_env,
            vision=True,
            priceMTk=dict(_PRICES.get(self.model, {})),
            parameters=dict(_PARAMETERS.get(self.model, {})),
        )


ROSTER: tuple[Leg, ...] = (
    Leg(
        slug="claude_sonnet_5",
        provider="anthropic",
        harness="claude_sdk",
        model="claude-sonnet-5",
        purpose="tier",
        max_concurrency=2,
        note="the campaign default; also the cross-provider arm of the decorrelation panel",
        reuse=(
            "measured 2026-08-19: trials 01-04 of logs/dev/author-bench/20260818T214525Z and "
            "trials 05-10 of logs/dev/author-bench/20260819T011004Z-rerun"
        ),
    ),
    Leg(
        slug="deepseek_v4_flash",
        provider="deepseek",
        harness="codex",
        model="deepseek-v4-flash",
        purpose="tier",
        key_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com/",
        note="open-weight family, served by its own vendor rather than self-hosted",
    ),
    Leg(
        slug="glm_5_3",
        provider="zai",
        harness="codex",
        model="glm-5.3",
        purpose="tier",
        max_concurrency=2,
        key_env="ZAI_API_KEY",
        base_url="https://api.z.ai/api/v1",
        note="open-weight family, served by its own vendor rather than self-hosted",
    ),
    Leg(
        slug="glm_5_2",
        provider="zai",
        harness="codex",
        model="glm-5.2",
        purpose="tier",
        max_concurrency=2,
        key_env="ZAI_API_KEY",
        base_url="https://api.z.ai/api/v1",
        in_default=False,
        note=(
            "the balanced-tier Z.AI author. `glm-5.3` benchmarks at the flagship tier, so the row "
            "measured 2026-08-21 is not tier-matched to the other authors; this leg is the "
            "like-for-like re-run and is addressable by --legs without joining a default batch"
        ),
    ),
    Leg(
        slug="claude_sonnet_5_correlated",
        provider="anthropic",
        harness="claude_sdk",
        model="claude-sonnet-5",
        purpose="decorrelation",
        colead_provider="anthropic",
        colead_model="claude-opus-5",
        max_concurrency=2,
        note=(
            "the campaign author reviewed by its own provider. claude-opus-5 is chosen because the "
            "reviewer swap found it the strongest reviewer, so a cost to sharing the author's "
            "provider even there is a strong result and no cost is an honest negative one"
        ),
    ),
    # ── Panel (b): the same five arms under a GPT analyst lead ──────────────
    # Panel (a) holds the lead at the Anthropic default, so the reviewer's provider and the
    # lead's move together and neither can be credited. In panel (a)'s own data the coder's
    # provider tracks nothing (a GPT coder under a GPT reviewer drew the joint-highest rejection
    # count) while the single zero-rejection arm is the single arm whose reviewer shares the
    # LEAD's vendor. Swapping the lead is what tests that: under a GPT lead the prediction is the
    # mirror image, GPT-reviewed arms falling toward zero and the Anthropic-reviewed arm rising.
    Leg(
        slug="claude_sonnet_5_gptlead",
        provider="anthropic",
        harness="claude_sdk",
        model="claude-sonnet-5",
        purpose="lead",
        lead_provider="openai",
        lead_model="gpt-5.6-sol",
        max_concurrency=2,
        note="panel (b) twin of the campaign default: lead and reviewer share a vendor here",
    ),
    Leg(
        slug="claude_sonnet_5_gptlead_anthropic_review",
        provider="anthropic",
        harness="claude_sdk",
        model="claude-sonnet-5",
        purpose="lead",
        lead_provider="openai",
        lead_model="gpt-5.6-sol",
        colead_provider="anthropic",
        colead_model="claude-opus-5",
        max_concurrency=2,
        note=(
            "the cell panel (a) cannot reach: the coder shares the reviewer's vendor while the "
            "lead does not, which is the arm that separates the two axes"
        ),
    ),
    Leg(
        slug="deepseek_v4_flash_gptlead",
        provider="deepseek",
        harness="codex",
        model="deepseek-v4-flash",
        purpose="lead",
        key_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com/",
        lead_provider="openai",
        lead_model="gpt-5.6-sol",
        max_concurrency=4,
        note="panel (b) twin; its own vendor imposes no ceiling that matters at this size",
    ),
    Leg(
        slug="glm_5_3_gptlead",
        provider="zai",
        harness="codex",
        model="glm-5.3",
        purpose="lead",
        key_env="ZAI_API_KEY",
        base_url="https://api.z.ai/api/v1",
        lead_provider="openai",
        lead_model="gpt-5.6-sol",
        max_concurrency=2,
        note=(
            "panel (b) twin. Held at two in flight: Z.AI throttled 87 to 97 per cent of author "
            "turns at three and four, which reaches the lead as an author that did nothing"
        ),
    ),
    Leg(
        slug="gpt_5_6_terra_gptlead",
        provider="openai",
        harness="codex",
        model="gpt-5.6-terra",
        purpose="lead",
        key_env="OPENAI_API_KEY",
        lead_provider="openai",
        lead_model="gpt-5.6-sol",
        colead_provider="openai",
        max_concurrency=4,
        note="panel (b) twin; coder, lead and reviewer all one vendor, the fully correlated cell",
    ),
    Leg(
        slug="gpt_5_6_terra_correlated",
        provider="openai",
        harness="codex",
        model="gpt-5.6-terra",
        purpose="decorrelation",
        key_env="OPENAI_API_KEY",
        colead_provider="openai",
        note=(
            "an OpenAI author reviewed by the OpenAI co-lead. Not a tier row: with the reviewer "
            "fixed at the configured OpenAI co-lead this author is provider-correlated, so it "
            "measures the same-provider arrangement rather than a capability tier"
        ),
    ),
)

BY_SLUG: dict[str, Leg] = {leg.slug: leg for leg in ROSTER}


def resolve(slugs: str | None) -> list[Leg]:
    """The legs a batch runs: every runnable leg by default, or exactly the ones named.

    Reuse rows are excluded from a default run because they are already measured; naming one
    explicitly still runs it, which is what a re-measurement needs.
    """
    if not slugs:
        return [leg for leg in ROSTER if leg.reuse is None and leg.in_default]
    wanted = [s.strip() for s in slugs.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in BY_SLUG]
    if unknown:
        raise SystemExit(
            f"unknown leg(s): {', '.join(unknown)}. "
            f"Known legs: {', '.join(BY_SLUG)}."
        )
    return [BY_SLUG[s] for s in wanted]
