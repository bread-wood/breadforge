"""Integration tests for rolling dispatch."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from breadforge.beads import BeadStore, WorkBead
from breadforge.config import Config
from breadforge.dispatch import RollingDispatcher
from breadforge.logger import Logger
from breadforge.runner import RunResult


def _make_config(tmp_path: Path, concurrency: int = 2) -> Config:
    return Config(
        repo="owner/repo",
        concurrency=concurrency,
        model="claude-sonnet-4-6",
        agent_timeout_minutes=60,
        watchdog_interval_seconds=5,
        max_retries=2,
        beads_dir=tmp_path / "beads",
    )


def _make_store(config: Config) -> BeadStore:
    return BeadStore(config.beads_dir, config.repo)


def _make_logger(tmp_path: Path) -> Logger:
    return Logger(tmp_path / "test.jsonl")


def _seed_issues(store: BeadStore, issue_numbers: list[int], repo: str = "owner/repo") -> None:
    for n in issue_numbers:
        bead = WorkBead(issue_number=n, repo=repo, title=f"Issue {n}")
        store.write_work_bead(bead)


def _make_run_result(exit_code: int = 0, pr_number: int | None = None) -> RunResult:
    return RunResult(exit_code=exit_code, stdout="", stderr=None, duration_ms=100.0)


class TestRollingDispatcher:
    @pytest.mark.asyncio
    async def test_successful_dispatch_creates_pr_bead(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        store = _make_store(config)
        logger = _make_logger(tmp_path)

        _seed_issues(store, [1, 2])

        with (
            patch("breadforge.dispatch._get_issue") as mock_issue,
            patch("breadforge.dispatch._get_pr_number") as mock_pr,
            patch("breadforge.dispatch._claim_issue"),
            patch("breadforge.dispatch._unclaim_issue"),
            patch("breadforge.dispatch.run_agent") as mock_run,
            patch("breadforge.dispatch.assess_and_allocate") as mock_assess,
        ):
            mock_issue.return_value = {"title": "Test Issue", "body": "Do the thing", "labels": []}
            mock_pr.return_value = 100
            mock_run.return_value = _make_run_result(exit_code=0)
            mock_run = AsyncMock(return_value=_make_run_result(exit_code=0))
            from breadforge.assessor import AllocationResult, ComplexityEstimate, ComplexityTier

            mock_assess.return_value = (
                AllocationResult(model="claude-sonnet-4-6", tier=ComplexityTier.MEDIUM),
                ComplexityEstimate(
                    tier=ComplexityTier.MEDIUM, confidence=0.8, reasoning="test", model_used="haiku"
                ),
            )

            with patch("breadforge.dispatch.run_agent", new_callable=lambda: lambda: mock_run):
                dispatcher = RollingDispatcher(config, store, logger)
                await dispatcher.run([1, 2])

        # Both issues should have PR beads
        pr_beads = store.list_pr_beads()
        assert len(pr_beads) == 2

    @pytest.mark.asyncio
    async def test_failed_agent_increments_retry(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, concurrency=1)
        store = _make_store(config)
        logger = _make_logger(tmp_path)

        _seed_issues(store, [5])

        with (
            patch("breadforge.dispatch._get_issue") as mock_issue,
            patch("breadforge.dispatch._get_pr_number") as mock_pr,
            patch("breadforge.dispatch._claim_issue"),
            patch("breadforge.dispatch._unclaim_issue"),
            patch("breadforge.dispatch._post_comment"),
            patch("breadforge.dispatch.assess_and_allocate") as mock_assess,
            patch("breadforge.dispatch.run_agent") as mock_run,
        ):
            mock_issue.return_value = {"title": "Failing Issue", "body": "", "labels": []}
            mock_pr.return_value = None  # No PR created
            mock_run = AsyncMock(return_value=_make_run_result(exit_code=1))

            from breadforge.assessor import AllocationResult, ComplexityEstimate, ComplexityTier

            mock_assess.return_value = (
                AllocationResult(model="claude-sonnet-4-6", tier=ComplexityTier.MEDIUM),
                ComplexityEstimate(
                    tier=ComplexityTier.MEDIUM, confidence=0.8, reasoning="test", model_used="haiku"
                ),
            )

            with patch("breadforge.dispatch.run_agent", new=mock_run):
                dispatcher = RollingDispatcher(config, store, logger)
                # Only 1 run — should re-queue since retry_count=1 < max_retries=2
                await dispatcher.run([5])

        bead = store.read_work_bead(5)
        assert bead is not None
        assert bead.retry_count >= 1
