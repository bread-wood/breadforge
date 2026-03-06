"""Extended assessor tests — Assessor, assess_from_plan_artifact, assess_and_allocate."""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from breadforge.agents.assessor import (
    Assessor,
    ComplexityEstimate,
    ComplexityTier,
    AllocationResult,
    assess_from_plan_artifact,
    assess_and_allocate,
)
from breadforge.beads.types import PlanArtifact


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_artifact(
    confidence: float = 0.9,
    risk_flags: list[str] | None = None,
    modules: list[str] | None = None,
) -> PlanArtifact:
    return PlanArtifact(
        milestone="v1.0",
        modules=modules or ["core"],
        files_per_module={"core": ["src/core.py"]},
        approach="standard impl",
        confidence=confidence,
        risk_flags=risk_flags or [],
    )


# ---------------------------------------------------------------------------
# Assessor.estimate — mocked LLM paths
# ---------------------------------------------------------------------------


class TestAssessorEstimate:
    def test_estimate_success(self) -> None:
        assessor = Assessor()
        response_json = json.dumps({"tier": "medium", "confidence": 0.8, "reasoning": "standard feature"})

        async def fake_call(self_inner, prompt: str) -> str:
            return response_json

        with patch.object(Assessor, "_call_llm", fake_call):
            result = asyncio.run(assessor.estimate("Fix bug", "Some body"))

        assert result.tier == ComplexityTier.MEDIUM
        assert result.confidence == 0.8
        assert result.reasoning == "standard feature"
        assert result.model_used == "claude-haiku-4-5-20251001"

    def test_estimate_with_code_fence_stripped(self) -> None:
        assessor = Assessor()
        response = "```json\n{\"tier\": \"high\", \"confidence\": 0.9, \"reasoning\": \"complex\"}\n```"

        async def fake_call(self_inner, prompt: str) -> str:
            return response

        with patch.object(Assessor, "_call_llm", fake_call):
            result = asyncio.run(assessor.estimate("Title", "Body"))

        assert result.tier == ComplexityTier.HIGH

    def test_estimate_fallback_on_bad_json(self) -> None:
        assessor = Assessor()

        async def fake_call(self_inner, prompt: str) -> str:
            return "not valid json"

        with patch.object(Assessor, "_call_llm", fake_call):
            result = asyncio.run(assessor.estimate("Title", "Body"))

        assert result.tier == ComplexityTier.MEDIUM
        assert result.confidence == 0.5
        assert "estimator fallback" in result.reasoning

    def test_estimate_fallback_on_exception(self) -> None:
        assessor = Assessor()

        async def fake_call(self_inner, prompt: str) -> str:
            raise RuntimeError("LLM error")

        with patch.object(Assessor, "_call_llm", fake_call):
            result = asyncio.run(assessor.estimate("Title", "Body"))

        assert result.tier == ComplexityTier.MEDIUM
        assert "estimator fallback" in result.reasoning

    def test_estimate_fallback_on_missing_key(self) -> None:
        assessor = Assessor()

        async def fake_call(self_inner, prompt: str) -> str:
            return json.dumps({"confidence": 0.5})  # missing 'tier'

        with patch.object(Assessor, "_call_llm", fake_call):
            result = asyncio.run(assessor.estimate("Title", "Body"))

        assert result.tier == ComplexityTier.MEDIUM

    def test_estimate_issue_body_truncated(self) -> None:
        """Issue body is truncated at 2000 chars in the prompt."""
        assessor = Assessor()
        captured: list[str] = []

        async def fake_call(self_inner, prompt: str) -> str:
            captured.append(prompt)
            return json.dumps({"tier": "low", "confidence": 0.9, "reasoning": "ok"})

        long_body = "x" * 5000
        with patch.object(Assessor, "_call_llm", fake_call):
            asyncio.run(assessor.estimate("Title", long_body))

        # Prompt should not contain more than 2000 chars of the body
        assert "x" * 2001 not in captured[0]

    def test_custom_estimator_model(self) -> None:
        assessor = Assessor(estimator_model="claude-opus-4-6")

        async def fake_call(self_inner, prompt: str) -> str:
            return json.dumps({"tier": "low", "confidence": 0.9, "reasoning": "ok"})

        with patch.object(Assessor, "_call_llm", fake_call):
            result = asyncio.run(assessor.estimate("Title", "Body"))

        assert result.model_used == "claude-opus-4-6"


# ---------------------------------------------------------------------------
# Assessor._call_llm — anthropic SDK path (breadmin not available)
# ---------------------------------------------------------------------------


class TestAssessorCallLLM:
    def test_call_llm_uses_anthropic_sdk_when_breadmin_absent(self) -> None:
        """When breadmin_llm is not importable, falls back to anthropic SDK."""
        assessor = Assessor()

        mock_response = AsyncMock()
        mock_response.content = [AsyncMock(text='{"tier":"low","confidence":0.9,"reasoning":"ok"}')]

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch.dict("sys.modules", {"breadmin_llm": None, "breadmin_llm.registry": None, "breadmin_llm.types": None}):
            with patch("anthropic.AsyncAnthropic", return_value=mock_client):
                result = asyncio.run(assessor.estimate("Title", "Body"))

        assert result.tier == ComplexityTier.LOW


# ---------------------------------------------------------------------------
# assess_from_plan_artifact
# ---------------------------------------------------------------------------


class TestAssessFromPlanArtifact:
    def test_high_risk_flag_returns_opus(self) -> None:
        artifact = make_artifact(confidence=0.9, risk_flags=["security"])
        result = assess_from_plan_artifact(artifact, "auth")
        assert result.model == "claude-opus-4-6"
        assert result.tier == ComplexityTier.HIGH
        assert result.upgraded is True

    def test_novel_domain_returns_opus(self) -> None:
        artifact = make_artifact(confidence=0.9, risk_flags=["novel-domain"])
        result = assess_from_plan_artifact(artifact, "parser")
        assert result.model == "claude-opus-4-6"

    def test_multi_module_coord_returns_opus(self) -> None:
        artifact = make_artifact(confidence=0.9, risk_flags=["multi-module-coordination"])
        result = assess_from_plan_artifact(artifact, "core")
        assert result.model == "claude-opus-4-6"

    def test_low_confidence_returns_opus(self) -> None:
        artifact = make_artifact(confidence=0.5)
        result = assess_from_plan_artifact(artifact, "core")
        assert result.model == "claude-opus-4-6"

    def test_high_confidence_no_flags_returns_sonnet(self) -> None:
        artifact = make_artifact(confidence=0.9)
        result = assess_from_plan_artifact(artifact, "core")
        assert result.model == "claude-sonnet-4-6"
        assert result.tier == ComplexityTier.MEDIUM

    def test_env_override_takes_precedence(self, monkeypatch) -> None:
        monkeypatch.setenv("BREADFORGE_MODEL", "my-custom-model")
        artifact = make_artifact(confidence=0.1, risk_flags=["security"])
        result = assess_from_plan_artifact(artifact, "core")
        assert result.model == "my-custom-model"
        assert result.overridden is True

    def test_explicit_override_param(self) -> None:
        artifact = make_artifact(confidence=0.1, risk_flags=["security"])
        result = assess_from_plan_artifact(artifact, "core", override_model="forced-model")
        assert result.model == "forced-model"
        assert result.overridden is True

    def test_low_risk_module_ignores_risk_flags(self) -> None:
        """Infra/docs modules are never upgraded even with high risk flags."""
        artifact = make_artifact(confidence=0.1, risk_flags=["security", "novel-domain"])
        for module in ("infra", "docs", "readme", "ci", "scaffold", "config"):
            result = assess_from_plan_artifact(artifact, module)
            assert result.model == "claude-sonnet-4-6", f"Expected sonnet for {module}"

    def test_low_risk_module_keyword_partial_match(self) -> None:
        artifact = make_artifact(confidence=0.1, risk_flags=["security"])
        result = assess_from_plan_artifact(artifact, "mod:docs")
        assert result.model == "claude-sonnet-4-6"

    def test_mixed_flags_only_needs_one_high_risk(self) -> None:
        artifact = make_artifact(confidence=0.9, risk_flags=["security", "unknown-flag"])
        result = assess_from_plan_artifact(artifact, "auth")
        assert result.model == "claude-opus-4-6"

    def test_no_risk_flags_and_high_confidence_returns_sonnet(self) -> None:
        artifact = make_artifact(confidence=0.8, risk_flags=[])
        result = assess_from_plan_artifact(artifact, "api")
        assert result.model == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# assess_and_allocate
# ---------------------------------------------------------------------------


class TestAssessAndAllocate:
    def test_env_override_skips_llm(self, monkeypatch) -> None:
        monkeypatch.setenv("BREADFORGE_MODEL", "override-model")
        alloc, estimate = asyncio.run(
            assess_and_allocate("Title", "Body")
        )
        assert alloc.model == "override-model"
        assert alloc.overridden is True
        assert estimate.reasoning == "model override — estimation skipped"
        assert estimate.tier == ComplexityTier.MEDIUM

    def test_explicit_override_param_skips_llm(self) -> None:
        alloc, estimate = asyncio.run(
            assess_and_allocate("Title", "Body", override_model="param-override")
        )
        assert alloc.model == "param-override"
        assert alloc.overridden is True

    def test_no_override_runs_estimation(self) -> None:
        async def fake_call(self_inner, prompt: str) -> str:
            return json.dumps({"tier": "high", "confidence": 0.95, "reasoning": "complex"})

        with patch.object(Assessor, "_call_llm", fake_call):
            alloc, estimate = asyncio.run(
                assess_and_allocate("Security feature", "Implements auth")
            )

        assert estimate.tier == ComplexityTier.HIGH
        assert alloc.tier == ComplexityTier.HIGH
        assert alloc.overridden is False

    def test_custom_tier_models_passed_through(self) -> None:
        custom_models = {
            ComplexityTier.LOW: "fast",
            ComplexityTier.MEDIUM: "mid",
            ComplexityTier.HIGH: "slow",
        }

        async def fake_call(self_inner, prompt: str) -> str:
            return json.dumps({"tier": "low", "confidence": 0.9, "reasoning": "simple"})

        with patch.object(Assessor, "_call_llm", fake_call):
            alloc, estimate = asyncio.run(
                assess_and_allocate("Simple task", "Body", tier_models=custom_models)
            )

        assert alloc.model == "fast"
