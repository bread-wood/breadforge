"""Integration tests for graph handlers — mocked run_agent."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from breadforge.agents.runner import RunResult
from breadforge.beads import BeadStore, GraphNode, PRBead, WorkBead
from breadforge.config import Config
from breadforge.graph.handlers.build import BuildHandler
from breadforge.graph.handlers.merge import MergeHandler
from breadforge.graph.handlers.research import ResearchHandler

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> Config:
    return Config.from_env("owner/repo")


@pytest.fixture
def store(tmp_path: Path) -> BeadStore:
    return BeadStore(tmp_path / "beads", "owner/repo")


def fake_run_result(
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = None,
    duration_ms: float = 100.0,
) -> RunResult:
    return RunResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr or "",
        duration_ms=duration_ms,
    )


# ---------------------------------------------------------------------------
# BuildHandler
# ---------------------------------------------------------------------------


class TestBuildHandler:
    def test_success_no_issue(self, config: Config, store: BeadStore, tmp_path: Path) -> None:
        node = GraphNode(
            id="v1-build-core",
            type="build",
            context={
                "module": "core",
                "files": ["src/core.py"],
                "milestone": "v1.0",
            },
        )

        with (
            patch("breadforge.graph.handlers.build.run_agent", new_callable=AsyncMock) as mock_run,
            patch("breadforge.graph.handlers.build._get_pr_number", return_value=42),
            patch("breadforge.graph.handlers.build._claim_issue"),
            patch("breadforge.graph.handlers.build._unclaim_issue"),
            patch(
                "breadforge.agents.assessor.assess_and_allocate", new_callable=AsyncMock
            ) as mock_assess,
        ):
            from breadforge.agents.assessor import (
                AllocationResult,
                ComplexityEstimate,
                ComplexityTier,
            )

            mock_assess.return_value = (
                AllocationResult(model="claude-sonnet-4-6", tier=ComplexityTier.MEDIUM),
                ComplexityEstimate(
                    tier=ComplexityTier.MEDIUM, confidence=0.8, reasoning="test", model_used="test"
                ),
            )
            mock_run.return_value = fake_run_result(exit_code=0)

            handler = BuildHandler(store=store)
            result = asyncio.run(handler.execute(node, config))

        assert result.success
        assert result.output["pr_number"] == 42

    def test_agent_failure(self, config: Config, store: BeadStore) -> None:
        node = GraphNode(
            id="v1-build-core",
            type="build",
            context={"module": "core", "milestone": "v1.0"},
        )

        with (
            patch("breadforge.graph.handlers.build.run_agent", new_callable=AsyncMock) as mock_run,
            patch(
                "breadforge.agents.assessor.assess_and_allocate", new_callable=AsyncMock
            ) as mock_assess,
        ):
            from breadforge.agents.assessor import (
                AllocationResult,
                ComplexityEstimate,
                ComplexityTier,
            )

            mock_assess.return_value = (
                AllocationResult(model="claude-sonnet-4-6", tier=ComplexityTier.MEDIUM),
                ComplexityEstimate(
                    tier=ComplexityTier.MEDIUM, confidence=0.8, reasoning="test", model_used="test"
                ),
            )
            mock_run.return_value = fake_run_result(exit_code=1, stderr="error")

            handler = BuildHandler(store=store)
            result = asyncio.run(handler.execute(node, config))

        assert not result.success

    def test_no_pr_created(self, config: Config, store: BeadStore) -> None:
        node = GraphNode(
            id="v1-build-core",
            type="build",
            context={"module": "core", "milestone": "v1.0"},
        )

        with (
            patch("breadforge.graph.handlers.build.run_agent", new_callable=AsyncMock) as mock_run,
            patch("breadforge.graph.handlers.build._get_pr_number", return_value=None),
            patch(
                "breadforge.agents.assessor.assess_and_allocate", new_callable=AsyncMock
            ) as mock_assess,
        ):
            from breadforge.agents.assessor import (
                AllocationResult,
                ComplexityEstimate,
                ComplexityTier,
            )

            mock_assess.return_value = (
                AllocationResult(model="claude-sonnet-4-6", tier=ComplexityTier.MEDIUM),
                ComplexityEstimate(
                    tier=ComplexityTier.MEDIUM, confidence=0.8, reasoning="test", model_used="test"
                ),
            )
            mock_run.return_value = fake_run_result(exit_code=0)

            handler = BuildHandler(store=store)
            result = asyncio.run(handler.execute(node, config))

        assert not result.success
        assert "no PR" in result.error

    def test_uses_plan_artifact_for_assessment(self, store: BeadStore) -> None:
        from breadforge.beads.types import PlanArtifact

        # Use a config with no model override so risk_flags take effect
        config_no_override = Config(repo="owner/repo", model="")
        artifact = PlanArtifact(
            milestone="v1.0",
            modules=["core"],
            files_per_module={"core": ["src/core.py"]},
            approach="simple",
            confidence=0.9,
            risk_flags=["security"],  # should force opus
        )
        node = GraphNode(
            id="v1-build-core",
            type="build",
            context={
                "module": "core",
                "milestone": "v1.0",
                "plan_artifact": artifact.model_dump(),
            },
        )

        with (
            patch("breadforge.graph.handlers.build.run_agent", new_callable=AsyncMock) as mock_run,
            patch("breadforge.graph.handlers.build._get_pr_number", return_value=99),
        ):
            mock_run.return_value = fake_run_result(exit_code=0)
            handler = BuildHandler(store=store)
            result = asyncio.run(handler.execute(node, config_no_override))

        assert result.success
        assert result.output["model"] == "claude-opus-4-6"


# ---------------------------------------------------------------------------
# MergeHandler
# ---------------------------------------------------------------------------


class TestMergeHandler:
    def test_ci_passing_merges(self, config: Config, store: BeadStore) -> None:
        node = GraphNode(
            id="v1-build-core-merge",
            type="merge",
            context={"pr_number": 42, "issue_number": 1, "branch": "1-core"},
        )
        work_bead = WorkBead(issue_number=1, repo="owner/repo", title="Core")
        pr_bead = PRBead(pr_number=42, repo="owner/repo", issue_number=1, branch="1-core")
        store.write_work_bead(work_bead)
        store.write_pr_bead(pr_bead)

        def _gh_side_effect(*args):
            result = MagicMock()
            result.returncode = 0
            result.stdout = json.dumps([])
            result.stderr = ""
            return result

        with patch("breadforge.graph.handlers.merge._gh", side_effect=_gh_side_effect):
            handler = MergeHandler(store=store)
            result = asyncio.run(handler.execute(node, config))

        assert result.success
        assert result.output["merged"] is True

        merged_pr = store.read_pr_bead(42)
        assert merged_pr is not None
        assert merged_pr.state == "merged"
        closed_work = store.read_work_bead(1)
        assert closed_work is not None
        assert closed_work.state == "closed"

    def test_ci_still_running_fails(self, config: Config, store: BeadStore) -> None:
        node = GraphNode(
            id="v1-merge",
            type="merge",
            context={"pr_number": 10},
        )

        with patch("breadforge.graph.handlers.merge._pr_ci_passing", return_value=None):
            handler = MergeHandler(store=store)
            result = asyncio.run(handler.execute(node, config))

        assert not result.success
        assert "CI still running" in result.error

    def test_ci_failing(self, config: Config, store: BeadStore) -> None:
        node = GraphNode(
            id="v1-merge",
            type="merge",
            context={"pr_number": 10},
        )

        with patch("breadforge.graph.handlers.merge._pr_ci_passing", return_value=False):
            handler = MergeHandler(store=store)
            result = asyncio.run(handler.execute(node, config))

        assert not result.success
        assert "CI failing" in result.error

    def test_no_pr_number(self, config: Config) -> None:
        node = GraphNode(id="v1-merge", type="merge", context={})
        handler = MergeHandler()
        result = asyncio.run(handler.execute(node, config))
        assert not result.success
        assert "no pr_number" in result.error


# ---------------------------------------------------------------------------
# ResearchHandler
# ---------------------------------------------------------------------------


class TestResearchHandler:
    def test_no_unknowns_succeeds(self, config: Config, store: BeadStore) -> None:
        node = GraphNode(
            id="v1-research-empty",
            type="research",
            context={"milestone": "v1.0", "unknowns": []},
        )
        handler = ResearchHandler(store=store)
        result = asyncio.run(handler.execute(node, config))
        assert result.success
        assert result.output["findings"] == ""

    def test_runs_agent_and_stores_findings(self, config: Config, store: BeadStore) -> None:
        node = GraphNode(
            id="v1-research-auth",
            type="research",
            context={"milestone": "v1.0", "unknowns": ["Which JWT library?"]},
        )

        with patch(
            "breadforge.graph.handlers.research.run_agent", new_callable=AsyncMock
        ) as mock_run:
            mock_run.return_value = fake_run_result(exit_code=0, stdout="# Research\n\nUse PyJWT.")
            handler = ResearchHandler(store=store)
            result = asyncio.run(handler.execute(node, config))

        assert result.success
        assert "PyJWT" in result.output["findings"]

        stored = store.read_research_findings("v1-research-auth")
        assert stored is not None
        assert "PyJWT" in stored

    def test_agent_failure(self, config: Config, store: BeadStore) -> None:
        node = GraphNode(
            id="v1-research-fail",
            type="research",
            context={"milestone": "v1.0", "unknowns": ["Something?"]},
        )

        with patch(
            "breadforge.graph.handlers.research.run_agent", new_callable=AsyncMock
        ) as mock_run:
            mock_run.return_value = fake_run_result(exit_code=1, stderr="timeout")
            handler = ResearchHandler(store=store)
            result = asyncio.run(handler.execute(node, config))

        assert not result.success

    def test_restricted_tools_passed(self, config: Config, store: BeadStore) -> None:
        """ResearchHandler should pass WebSearch/WebFetch only."""
        node = GraphNode(
            id="v1-research-tools",
            type="research",
            context={"milestone": "v1.0", "unknowns": ["Q?"]},
        )

        with patch(
            "breadforge.graph.handlers.research.run_agent", new_callable=AsyncMock
        ) as mock_run:
            mock_run.return_value = fake_run_result(exit_code=0, stdout="findings")
            handler = ResearchHandler(store=store)
            asyncio.run(handler.execute(node, config))

        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs.get("allowed_tools") == ["WebSearch", "WebFetch"]
        assert call_kwargs.get("timeout_minutes") == 15
