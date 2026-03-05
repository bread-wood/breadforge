"""Tests for the WaitHandler node type.

WaitHandler pauses DAG execution until a condition recorded in node.context
is satisfied, then succeeds.  It polls at a configurable interval and gives up
after max_polls attempts.

This file defines the handler inline (the production handler will live in
src/breadforge/graph/handlers/wait.py once that module is implemented).
Tests cover:
  - condition evaluation (always_true, always_false, file_exists)
  - poll counting and timeout semantics
  - protocol compliance (recover() returns None)
  - integration through GraphExecutor with a registered "wait" handler
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from breadforge.beads.types import GraphNode
from breadforge.config import Config
from breadforge.graph.executor import ExecutionGraph, GraphExecutor
from breadforge.graph.node import NodeResult

# ---------------------------------------------------------------------------
# Minimal WaitHandler — defines the behavioral contract
# ---------------------------------------------------------------------------


class WaitHandler:
    """Polls a condition until it is satisfied or max_polls is exhausted.

    Context keys:
        condition (str): "always_true" | "always_false" | "file_exists"
        path (str): file path used by the "file_exists" condition
        poll_interval (float): seconds between polls (default 0.05)
        max_polls (int): maximum number of poll attempts (default 3)
    """

    async def execute(self, node: GraphNode, config: Config) -> NodeResult:
        condition: str = node.context.get("condition", "always_true")
        poll_interval: float = float(node.context.get("poll_interval", 0.05))
        max_polls: int = int(node.context.get("max_polls", 3))

        for attempt in range(1, max_polls + 1):
            met = self._check(condition, node)
            if met:
                return NodeResult(
                    success=True,
                    output={"polls": attempt, "condition": condition},
                )
            if attempt < max_polls:
                await asyncio.sleep(poll_interval)

        return NodeResult(
            success=False,
            error=f"wait condition '{condition}' not met after {max_polls} polls",
        )

    def _check(self, condition: str, node: GraphNode) -> bool:
        if condition == "always_true":
            return True
        if condition == "always_false":
            return False
        if condition == "file_exists":
            return Path(str(node.context.get("path", ""))).exists()
        # Unknown condition — treat as unsatisfied
        return False

    def recover(self, node: GraphNode, config: Config) -> NodeResult | None:
        """Re-dispatch on restart — wait conditions must be re-evaluated."""
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def make_wait_node(
    node_id: str = "wait-1",
    condition: str = "always_true",
    poll_interval: float = 0.01,
    max_polls: int = 3,
    max_retries: int = 3,
    depends_on: list[str] | None = None,
    extra_context: dict | None = None,
) -> GraphNode:
    """Construct a 'wait' GraphNode via model_construct (bypasses Literal check)."""
    context: dict = {
        "condition": condition,
        "poll_interval": poll_interval,
        "max_polls": max_polls,
    }
    if extra_context:
        context.update(extra_context)
    return GraphNode.model_construct(
        id=node_id,
        type="wait",
        state="pending",
        depends_on=depends_on or [],
        context=context,
        output=None,
        assigned_model=None,
        retry_count=0,
        max_retries=max_retries,
        created_at=_now(),
        started_at=None,
        completed_at=None,
    )


@pytest.fixture
def config() -> Config:
    return Config.from_env("owner/repo")


# ---------------------------------------------------------------------------
# Unit tests — condition evaluation
# ---------------------------------------------------------------------------


class TestWaitHandlerConditions:
    def test_always_true_succeeds_on_first_poll(self, config: Config) -> None:
        handler = WaitHandler()
        node = make_wait_node(condition="always_true")
        result = asyncio.run(handler.execute(node, config))
        assert result.success
        assert result.output["polls"] == 1

    def test_always_false_exhausts_polls(self, config: Config) -> None:
        handler = WaitHandler()
        node = make_wait_node(condition="always_false", max_polls=2)
        result = asyncio.run(handler.execute(node, config))
        assert not result.success
        assert "not met after 2 polls" in (result.error or "")

    def test_file_exists_succeeds_when_file_present(self, config: Config, tmp_path: Path) -> None:
        target = tmp_path / "trigger.txt"
        target.write_text("ready")
        handler = WaitHandler()
        node = make_wait_node(condition="file_exists", extra_context={"path": str(target)})
        result = asyncio.run(handler.execute(node, config))
        assert result.success

    def test_file_exists_fails_when_file_absent(self, config: Config, tmp_path: Path) -> None:
        handler = WaitHandler()
        node = make_wait_node(
            condition="file_exists",
            max_polls=2,
            extra_context={"path": str(tmp_path / "missing.txt")},
        )
        result = asyncio.run(handler.execute(node, config))
        assert not result.success

    def test_unknown_condition_fails_gracefully(self, config: Config) -> None:
        handler = WaitHandler()
        node = make_wait_node(condition="no_such_condition", max_polls=1)
        result = asyncio.run(handler.execute(node, config))
        assert not result.success

    def test_output_includes_poll_count(self, config: Config) -> None:
        handler = WaitHandler()
        node = make_wait_node(condition="always_true", max_polls=5)
        result = asyncio.run(handler.execute(node, config))
        assert result.success
        assert "polls" in result.output

    def test_recover_returns_none(self, config: Config) -> None:
        handler = WaitHandler()
        node = make_wait_node()
        assert handler.recover(node, config) is None


# ---------------------------------------------------------------------------
# Integration tests — GraphExecutor with a registered "wait" handler
# ---------------------------------------------------------------------------


class TestWaitHandlerIntegration:
    def test_executor_dispatches_wait_then_build(self, config: Config) -> None:
        wait_node = make_wait_node("gate", condition="always_true")
        build_node = GraphNode(
            id="build-after-gate",
            type="build",
            depends_on=["gate"],
        )
        graph = ExecutionGraph([wait_node, build_node])

        build_handler = AsyncMock()
        build_handler.execute = AsyncMock(return_value=NodeResult(success=True))
        build_handler.recover = lambda node, cfg: None

        executor = GraphExecutor(
            config=config,
            handlers={"wait": WaitHandler(), "build": build_handler},
            concurrency=2,
            watchdog_interval=0.1,
        )
        result = asyncio.run(executor.run(graph))
        assert result.success
        assert "gate" in result.done
        assert "build-after-gate" in result.done

    def test_abandoned_wait_unblocks_downstream(self, config: Config) -> None:
        """A wait node that exhausts retries is abandoned; downstream can still run."""
        wait_node = make_wait_node(
            "gate-fail",
            condition="always_false",
            max_polls=1,
            max_retries=1,
        )
        build_node = GraphNode(
            id="build-after-fail",
            type="build",
            depends_on=["gate-fail"],
        )
        graph = ExecutionGraph([wait_node, build_node])

        build_handler = AsyncMock()
        build_handler.execute = AsyncMock(return_value=NodeResult(success=True))
        build_handler.recover = lambda node, cfg: None

        executor = GraphExecutor(
            config=config,
            handlers={"wait": WaitHandler(), "build": build_handler},
            concurrency=2,
            watchdog_interval=0.1,
        )
        result = asyncio.run(executor.run(graph))
        assert "gate-fail" in result.abandoned
        assert "build-after-fail" in result.done

    def test_no_handler_for_wait_returns_error(self, config: Config) -> None:
        """If no 'wait' handler is registered, node is abandoned after retries."""
        wait_node = make_wait_node("gate-unhandled", max_retries=1)
        graph = ExecutionGraph([wait_node])

        executor = GraphExecutor(
            config=config,
            handlers={},  # no handlers registered
            concurrency=1,
            watchdog_interval=0.1,
        )
        result = asyncio.run(executor.run(graph))
        assert "gate-unhandled" in result.abandoned
