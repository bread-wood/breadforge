"""LLM assessor and allocator — dynamic model tier selection.

Before dispatching an agent, the assessor reads the issue or plan artifact and
estimates task complexity. The allocator maps the complexity tier to a concrete
model, applying confidence-based tier upgrades and respecting overrides.

Tiers (cheapest → most capable):
  LOW    — routine tasks: formatting, docs, simple bug fixes
  MEDIUM — standard feature work: new endpoints, data models, tests
  HIGH   — complex tasks: security changes, multi-module coordination,
           cross-repo deps, novel algorithms
"""

from __future__ import annotations

import os
from enum import StrEnum

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class ComplexityTier(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ComplexityEstimate(BaseModel):
    tier: ComplexityTier
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    model_used: str


class AllocationResult(BaseModel):
    model: str
    tier: ComplexityTier
    overridden: bool = False
    """True when BREADFORGE_MODEL env var forced the selection."""
    upgraded: bool = False
    """True when low confidence caused a tier upgrade."""


# ---------------------------------------------------------------------------
# Tier → model mapping
# ---------------------------------------------------------------------------

_DEFAULT_TIER_MODELS: dict[ComplexityTier, str] = {
    ComplexityTier.LOW: "claude-haiku-4-5-20251001",
    ComplexityTier.MEDIUM: "claude-sonnet-4-6",
    ComplexityTier.HIGH: "claude-opus-4-6",
}

OPUS = "claude-opus-4-6"
SONNET = "claude-sonnet-4-6"

_TIER_ORDER = [ComplexityTier.LOW, ComplexityTier.MEDIUM, ComplexityTier.HIGH]

CONFIDENCE_UPGRADE_THRESHOLD = 0.6


def _upgrade_tier(tier: ComplexityTier) -> ComplexityTier:
    idx = _TIER_ORDER.index(tier)
    return _TIER_ORDER[min(idx + 1, len(_TIER_ORDER) - 1)]


# ---------------------------------------------------------------------------
# Estimator
# ---------------------------------------------------------------------------

_ESTIMATOR_PROMPT_TEMPLATE = """You are a task complexity estimator. Read the following GitHub issue and estimate its implementation complexity.

Issue title: {title}
Issue body:
{body}

Respond with a JSON object (no markdown, no explanation outside JSON):
{{
  "tier": "low" | "medium" | "high",
  "confidence": 0.0-1.0,
  "reasoning": "one sentence"
}}

Guidance:
- low: formatting, docs, simple data model changes, config tweaks
- medium: new feature endpoints, standard CRUD, unit tests, refactoring
- high: security-critical changes, multi-module coordination, novel algorithms,
        cross-repo dependencies, architectural changes, performance optimization
"""


class Assessor:
    """Estimates task complexity using a lightweight LLM call."""

    def __init__(
        self,
        estimator_model: str = "claude-haiku-4-5-20251001",
        tier_models: dict[ComplexityTier, str] | None = None,
    ) -> None:
        self._estimator_model = estimator_model
        self._tier_models = tier_models or _DEFAULT_TIER_MODELS.copy()

    async def estimate(
        self,
        issue_title: str,
        issue_body: str,
    ) -> ComplexityEstimate:
        """Estimate complexity via a cheap LLM call. Falls back to MEDIUM on error."""
        import json

        prompt = _ESTIMATOR_PROMPT_TEMPLATE.format(title=issue_title, body=issue_body[:2000])

        try:
            result = await self._call_llm(prompt)
            text = result.strip()
            if text.startswith("```"):
                lines = text.splitlines()
                text = "\n".join(line for line in lines if not line.startswith("```")).strip()
            data = json.loads(text)
            return ComplexityEstimate(
                tier=ComplexityTier(data["tier"]),
                confidence=float(data["confidence"]),
                reasoning=data.get("reasoning", ""),
                model_used=self._estimator_model,
            )
        except Exception as e:
            return ComplexityEstimate(
                tier=ComplexityTier.MEDIUM,
                confidence=0.5,
                reasoning=f"estimator fallback: {e}",
                model_used=self._estimator_model,
            )

    async def _call_llm(self, prompt: str) -> str:
        """Call LLM via breadmin-llm if available, else direct Anthropic SDK."""
        try:
            from breadmin_llm.registry import ProviderRegistry
            from breadmin_llm.types import LLMCall, LLMMessage, MessageRole

            registry = ProviderRegistry.default()
            call = LLMCall(
                model=self._estimator_model,
                messages=[LLMMessage(role=MessageRole.USER, content=prompt)],
                max_tokens=200,
                caller="breadforge.assessor",
            )
            result = await registry.complete(call)
            return result.content
        except ImportError:
            pass

        import anthropic

        client = anthropic.AsyncAnthropic()
        response = await client.messages.create(
            model=self._estimator_model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Plan artifact-based assessment (used by BuildHandler)
# ---------------------------------------------------------------------------


def assess_from_plan_artifact(
    artifact: PlanArtifact,
    module: str,
    override_model: str | None = None,
) -> AllocationResult:
    """Assess model tier from a PlanArtifact rather than raw issue text.

    HIGH_RISK signals → opus; low confidence → opus; otherwise sonnet.
    """

    env_override = override_model or os.environ.get("BREADFORGE_MODEL")
    if env_override:
        return AllocationResult(
            model=env_override,
            tier=ComplexityTier.MEDIUM,
            overridden=True,
        )

    # Low-risk module heuristic: infra/scaffold/docs modules don't need opus
    # even when the artifact has global risk flags.
    _LOW_RISK_KEYWORDS = {"infra", "scaffold", "docs", "readme", "ci", "config"}
    module_lower = module.lower().removeprefix("mod:").strip()
    is_low_risk_module = any(k in module_lower for k in _LOW_RISK_KEYWORDS)

    if not is_low_risk_module:
        high_risk = {"novel-domain", "security", "multi-module-coordination"}
        if high_risk & set(artifact.risk_flags):
            return AllocationResult(model=OPUS, tier=ComplexityTier.HIGH, upgraded=True)
        if artifact.confidence < 0.7:
            return AllocationResult(model=OPUS, tier=ComplexityTier.HIGH, upgraded=True)

    return AllocationResult(model=SONNET, tier=ComplexityTier.MEDIUM)


# Keep type hint importable without circular issue
from breadforge.beads.types import PlanArtifact  # noqa: E402 — after function definition

# ---------------------------------------------------------------------------
# Allocator
# ---------------------------------------------------------------------------


class Allocator:
    """Maps a complexity estimate to a concrete model string."""

    def __init__(
        self,
        tier_models: dict[ComplexityTier, str] | None = None,
        confidence_threshold: float = CONFIDENCE_UPGRADE_THRESHOLD,
    ) -> None:
        self._tier_models = tier_models or _DEFAULT_TIER_MODELS.copy()
        self._threshold = confidence_threshold

    def allocate(
        self, estimate: ComplexityEstimate, override_model: str | None = None
    ) -> AllocationResult:
        env_override = override_model or os.environ.get("BREADFORGE_MODEL")
        if env_override:
            return AllocationResult(
                model=env_override,
                tier=estimate.tier,
                overridden=True,
            )

        tier = estimate.tier
        upgraded = False

        if estimate.confidence < self._threshold:
            new_tier = _upgrade_tier(tier)
            upgraded = new_tier != tier
            tier = new_tier

        model = self._tier_models.get(tier, self._tier_models[ComplexityTier.HIGH])
        return AllocationResult(model=model, tier=tier, upgraded=upgraded)


# ---------------------------------------------------------------------------
# Convenience: assess_and_allocate
# ---------------------------------------------------------------------------


async def assess_and_allocate(
    issue_title: str,
    issue_body: str,
    *,
    override_model: str | None = None,
    estimator_model: str = "claude-haiku-4-5-20251001",
    tier_models: dict[ComplexityTier, str] | None = None,
) -> tuple[AllocationResult, ComplexityEstimate]:
    """Full assess + allocate pipeline. Returns (allocation, estimate)."""
    env_override = override_model or os.environ.get("BREADFORGE_MODEL")
    if env_override:
        dummy = ComplexityEstimate(
            tier=ComplexityTier.MEDIUM,
            confidence=1.0,
            reasoning="model override — estimation skipped",
            model_used="none",
        )
        alloc = AllocationResult(model=env_override, tier=ComplexityTier.MEDIUM, overridden=True)
        return alloc, dummy

    assessor = Assessor(estimator_model=estimator_model, tier_models=tier_models)
    allocator = Allocator(tier_models=tier_models)

    estimate = await assessor.estimate(issue_title, issue_body)
    allocation = allocator.allocate(estimate, override_model=override_model)
    return allocation, estimate
