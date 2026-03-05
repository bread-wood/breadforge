"""Unit tests for LLM assessor and allocator."""

from breadforge.assessor import (
    CONFIDENCE_UPGRADE_THRESHOLD,
    Allocator,
    ComplexityEstimate,
    ComplexityTier,
    _upgrade_tier,
)


class TestUpgradeTier:
    def test_low_upgrades_to_medium(self) -> None:
        assert _upgrade_tier(ComplexityTier.LOW) == ComplexityTier.MEDIUM

    def test_medium_upgrades_to_high(self) -> None:
        assert _upgrade_tier(ComplexityTier.MEDIUM) == ComplexityTier.HIGH

    def test_high_stays_high(self) -> None:
        assert _upgrade_tier(ComplexityTier.HIGH) == ComplexityTier.HIGH


class TestAllocator:
    def test_allocate_low_confidence_upgrades(self) -> None:
        allocator = Allocator()
        estimate = ComplexityEstimate(
            tier=ComplexityTier.LOW,
            confidence=0.4,  # below threshold
            reasoning="test",
            model_used="test-model",
        )
        result = allocator.allocate(estimate)
        assert result.tier == ComplexityTier.MEDIUM
        assert result.upgraded is True

    def test_allocate_high_confidence_no_upgrade(self) -> None:
        allocator = Allocator()
        estimate = ComplexityEstimate(
            tier=ComplexityTier.LOW,
            confidence=0.9,
            reasoning="test",
            model_used="test-model",
        )
        result = allocator.allocate(estimate)
        assert result.tier == ComplexityTier.LOW
        assert result.upgraded is False

    def test_override_skips_estimation(self, monkeypatch) -> None:
        monkeypatch.setenv("BREADFORGE_MODEL", "custom-model")
        allocator = Allocator()
        estimate = ComplexityEstimate(
            tier=ComplexityTier.HIGH,
            confidence=0.9,
            reasoning="test",
            model_used="test-model",
        )
        result = allocator.allocate(estimate)
        assert result.model == "custom-model"
        assert result.overridden is True

    def test_explicit_override_param(self) -> None:
        allocator = Allocator()
        estimate = ComplexityEstimate(
            tier=ComplexityTier.LOW,
            confidence=0.9,
            reasoning="test",
            model_used="test-model",
        )
        result = allocator.allocate(estimate, override_model="explicit-model")
        assert result.model == "explicit-model"
        assert result.overridden is True

    def test_custom_tier_models(self) -> None:
        custom_models = {
            ComplexityTier.LOW: "fast-model",
            ComplexityTier.MEDIUM: "mid-model",
            ComplexityTier.HIGH: "big-model",
        }
        allocator = Allocator(tier_models=custom_models)
        estimate = ComplexityEstimate(
            tier=ComplexityTier.HIGH,
            confidence=1.0,
            reasoning="test",
            model_used="test-model",
        )
        result = allocator.allocate(estimate)
        assert result.model == "big-model"

    def test_threshold_boundary(self) -> None:
        allocator = Allocator(confidence_threshold=CONFIDENCE_UPGRADE_THRESHOLD)
        # Exactly at threshold → no upgrade
        estimate = ComplexityEstimate(
            tier=ComplexityTier.LOW,
            confidence=CONFIDENCE_UPGRADE_THRESHOLD,
            reasoning="test",
            model_used="test-model",
        )
        result = allocator.allocate(estimate)
        assert result.upgraded is False

        # Just below → upgrade
        estimate2 = ComplexityEstimate(
            tier=ComplexityTier.LOW,
            confidence=CONFIDENCE_UPGRADE_THRESHOLD - 0.01,
            reasoning="test",
            model_used="test-model",
        )
        result2 = allocator.allocate(estimate2)
        assert result2.upgraded is True
