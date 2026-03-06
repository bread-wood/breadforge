"""Unit tests for GraphExecutor, ExecutionGraph, and _add_overlap_edges."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from breadforge.beads import BeadStore, GraphNode
from breadforge.config import Config
from breadforge.graph.executor import (
    ExecutionGraph,
    GraphExecutor,
    _add_overlap_edges,
)
from breadforge.graph.node import NodeResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_node(
    id: str,
    type: str = "build",
    depends_on: list[str] | None = None,
    files: list[str] | None = None,
    max_retries: int = 3,
) -> GraphNode:
    context = {}
    if files is not None:
        context["files"] = files
    return GraphNode(
        id=id,
        type=type,  # type: ignore[arg-type]
        depends_on=depends_on or [],
        context=context,
        max_retries=max_retries,
    )


def mock_handler(success: bool = True, output: dict | None = None, error: str | None = None):
    from unittest.mock import MagicMock

    handler = AsyncMock()
    handler.execute = AsyncMock(
        return_value=NodeResult(success=success, output=output or {}, error=error)
    )
    handler.recover = MagicMock(return_value=None)  # recover() is sync; returns None by default
    return handler


@pytest.fixture
def config() -> Config:
    return Config.from_env("owner/repo")


@pytest.fixture
def store(tmp_path: Path) -> BeadStore:
    return BeadStore(tmp_path / "beads", "owner/repo")


# ---------------------------------------------------------------------------
# ExecutionGraph
# ---------------------------------------------------------------------------


class TestExecutionGraph:
    def test_get_ready_no_deps(self) -> None:
        graph = ExecutionGraph([make_node("a"), make_node("b")])
        ready = graph.get_ready()
        assert {n.id for n in ready} == {"a", "b"}

    def test_get_ready_blocked(self) -> None:
        graph = ExecutionGraph(
            [
                make_node("a"),
                make_node("b", depends_on=["a"]),
            ]
        )
        ready = graph.get_ready()
        assert [n.id for n in ready] == ["a"]

    def test_get_ready_after_dep_done(self) -> None:
        a = make_node("a")
        b = make_node("b", depends_on=["a"])
        graph = ExecutionGraph([a, b])
        # Mark a as done
        graph.get_node("a").state = "done"  # type: ignore[union-attr]
        ready = graph.get_ready()
        assert [n.id for n in ready] == ["b"]

    def test_get_ready_dep_abandoned_unblocks(self) -> None:
        a = make_node("a")
        b = make_node("b", depends_on=["a"])
        graph = ExecutionGraph([a, b])
        graph.get_node("a").state = "abandoned"  # type: ignore[union-attr]
        ready = graph.get_ready()
        assert [n.id for n in ready] == ["b"]

    def test_has_pending(self) -> None:
        graph = ExecutionGraph([make_node("a")])
        assert graph.has_pending() is True
        graph.get_node("a").state = "done"  # type: ignore[union-attr]
        assert graph.has_pending() is False

    def test_add_nodes_dynamic(self) -> None:
        graph = ExecutionGraph([make_node("plan")])
        graph.get_node("plan").state = "done"  # type: ignore[union-attr]
        graph.add_nodes([make_node("build-a"), make_node("build-b")])
        assert graph.has_pending() is True
        ready = graph.get_ready()
        assert {n.id for n in ready} == {"build-a", "build-b"}

    def test_add_nodes_no_duplicate(self) -> None:
        graph = ExecutionGraph([make_node("a")])
        graph.add_nodes([make_node("a")])  # duplicate
        assert len(graph.all_nodes()) == 1


# ---------------------------------------------------------------------------
# _add_overlap_edges
# ---------------------------------------------------------------------------


class TestOverlapEdges:
    def test_no_overlap(self) -> None:
        nodes = [
            make_node("build-a", files=["src/a.py"]),
            make_node("build-b", files=["src/b.py"]),
        ]
        result = _add_overlap_edges(nodes)
        assert result[0].depends_on == []
        assert result[1].depends_on == []

    def test_overlap_creates_edge(self) -> None:
        nodes = [
            make_node("build-a", files=["src/shared.py", "src/a.py"]),
            make_node("build-b", files=["src/shared.py", "src/b.py"]),
        ]
        result = _add_overlap_edges(nodes)
        nodes_by_id = {n.id: n for n in result}
        # alphabetical: build-a < build-b, so build-b depends on build-a
        assert "build-a" in nodes_by_id["build-b"].depends_on

    def test_overlap_alphabetical_ordering(self) -> None:
        nodes = [
            make_node("build-zz", files=["src/shared.py"]),
            make_node("build-aa", files=["src/shared.py"]),
        ]
        result = _add_overlap_edges(nodes)
        nodes_by_id = {n.id: n for n in result}
        # build-aa < build-zz → build-zz depends on build-aa
        assert "build-aa" in nodes_by_id["build-zz"].depends_on

    def test_three_way_overlap(self) -> None:
        nodes = [
            make_node("build-a", files=["src/shared.py"]),
            make_node("build-b", files=["src/shared.py"]),
            make_node("build-c", files=["src/shared.py"]),
        ]
        result = _add_overlap_edges(nodes)
        nodes_by_id = {n.id: n for n in result}
        # build-a < build-b < build-c
        assert "build-a" in nodes_by_id["build-b"].depends_on
        assert "build-b" in nodes_by_id["build-c"].depends_on

    def test_non_build_nodes_skipped(self) -> None:
        nodes = [
            make_node("plan", type="plan", files=["src/shared.py"]),
            make_node("build-a", type="build", files=["src/shared.py"]),
        ]
        result = _add_overlap_edges(nodes)
        nodes_by_id = {n.id: n for n in result}
        # plan node is not a build node — no edge from plan to build-a
        assert nodes_by_id["build-a"].depends_on == []


# ---------------------------------------------------------------------------
# GraphExecutor
# ---------------------------------------------------------------------------


class TestGraphExecutor:
    def test_single_node_success(self, config: Config) -> None:
        handler = mock_handler(success=True)
        executor = GraphExecutor(
            config=config,
            handlers={"build": handler},
            concurrency=2,
            watchdog_interval=0.1,
        )
        graph = ExecutionGraph([make_node("build-a")])
        result = asyncio.run(executor.run(graph))
        assert result.success
        assert "build-a" in result.done

    def test_dag_ordering(self, config: Config) -> None:
        """Dependent node runs after its dep completes."""
        call_order: list[str] = []

        async def make_execute(node_id: str):
            async def execute(node, cfg):
                call_order.append(node_id)
                return NodeResult(success=True)

            return execute

        build_handler = AsyncMock()
        build_handler.execute = AsyncMock(side_effect=lambda n, c: NodeResult(success=True))

        order: list[str] = []

        async def execute_a(node, cfg):
            order.append("a")
            return NodeResult(success=True)

        async def execute_b(node, cfg):
            order.append("b")
            return NodeResult(success=True)

        class SequentialHandler:
            def __init__(self, fn):
                self._fn = fn

            async def execute(self, node, cfg):
                return await self._fn(node, cfg)

        a = make_node("a")
        b = make_node("b", depends_on=["a"])
        graph = ExecutionGraph([a, b])

        # Use a single handler that records the call order
        dispatched: list[str] = []

        async def execute(node, cfg):
            dispatched.append(node.id)
            return NodeResult(success=True)

        handler = AsyncMock()
        handler.execute = execute

        executor = GraphExecutor(
            config=config,
            handlers={"build": handler},
            concurrency=4,
            watchdog_interval=0.1,
        )
        result = asyncio.run(executor.run(graph))
        assert result.success
        assert dispatched.index("a") < dispatched.index("b")

    def test_failure_retry_then_abandon(self, config: Config) -> None:
        handler = mock_handler(success=False, error="boom")
        executor = GraphExecutor(
            config=config,
            handlers={"build": handler},
            concurrency=1,
            watchdog_interval=0.1,
        )
        node = make_node("build-a", max_retries=2)
        graph = ExecutionGraph([node])
        result = asyncio.run(executor.run(graph))
        assert not result.success
        assert "build-a" in result.abandoned
        # Called max_retries times (2)
        assert handler.execute.call_count == 2

    def test_plan_node_expands_graph(self, config: Config) -> None:
        new_nodes = [
            {
                "id": "build-core",
                "type": "build",
                "state": "pending",
                "depends_on": [],
                "context": {},
                "output": None,
                "assigned_model": None,
                "retry_count": 0,
                "max_retries": 3,
                "created_at": "2026-01-01T00:00:00+00:00",
                "started_at": None,
                "completed_at": None,
            },
        ]

        plan_handler = AsyncMock()
        plan_handler.execute = AsyncMock(
            return_value=NodeResult(
                success=True,
                output={"new_nodes": new_nodes},
            )
        )
        build_handler = mock_handler(success=True)

        executor = GraphExecutor(
            config=config,
            handlers={"plan": plan_handler, "build": build_handler},
            concurrency=2,
            watchdog_interval=0.1,
        )
        graph = ExecutionGraph([make_node("v1-plan", type="plan")])
        result = asyncio.run(executor.run(graph))
        assert result.success
        assert "v1-plan" in result.done
        assert "build-core" in result.done

    def test_deadlock_detection(self, config: Config) -> None:
        """Nodes with unsatisfiable deps are not run."""
        # Node depends on itself — will never be ready
        node = make_node("build-a", depends_on=["build-a"])
        graph = ExecutionGraph([node])
        handler = mock_handler(success=True)
        executor = GraphExecutor(
            config=config,
            handlers={"build": handler},
            concurrency=1,
            watchdog_interval=0.1,
        )
        result = asyncio.run(executor.run(graph))
        # Graph has a pending node but it's never dispatched — executor exits
        assert not result.success or node.state == "pending"
        assert handler.execute.call_count == 0

    def test_rerun_retries_abandoned_nodes(self, config: Config, store: BeadStore) -> None:
        """On a second run, previously abandoned build nodes are re-dispatched."""
        from breadforge.beads import GraphNode as StoredNode

        # Simulate a previous run: plan done, build abandoned
        plan_node_data = make_node("v1-plan", type="plan")
        plan_node_data.state = "done"  # type: ignore[assignment]
        plan_node_data.output = {}
        store.write_node(plan_node_data)

        build_node_data = make_node("v1-build")
        build_node_data.state = "abandoned"  # type: ignore[assignment]
        build_node_data.retry_count = 3
        store.write_node(build_node_data)

        # New run: build handler now succeeds
        plan_handler = mock_handler(success=True)
        build_handler = mock_handler(success=True)
        executor = GraphExecutor(
            config=config,
            handlers={"plan": plan_handler, "build": build_handler},
            store=store,
            concurrency=2,
            watchdog_interval=0.1,
        )
        graph = ExecutionGraph([make_node("v1-plan", type="plan"), make_node("v1-build")])
        result = asyncio.run(executor.run(graph))

        # Plan stays done (not re-run); build is re-dispatched and succeeds
        assert "v1-plan" in result.done
        assert "v1-build" in result.done
        assert plan_handler.execute.call_count == 0  # skipped (was done)
        assert build_handler.execute.call_count == 1  # retried

    def test_store_persists_nodes(self, config: Config, store: BeadStore) -> None:
        handler = mock_handler(success=True)
        executor = GraphExecutor(
            config=config,
            handlers={"build": handler},
            store=store,
            concurrency=1,
            watchdog_interval=0.1,
        )
        graph = ExecutionGraph([make_node("build-a")])
        asyncio.run(executor.run(graph))
        persisted = store.read_node("build-a")
        assert persisted is not None
        assert persisted.state == "done"

    def test_auto_abandon_when_all_deps_abandoned(self, config: Config) -> None:
        """A node whose every dependency is abandoned is auto-abandoned without being dispatched."""
        build_handler = mock_handler(success=False, error="clone failed")
        downstream_handler = mock_handler(success=True)

        build = make_node("build-a", max_retries=1)
        downstream = make_node("merge-a", type="merge", depends_on=["build-a"])

        executor = GraphExecutor(
            config=config,
            handlers={"build": build_handler, "merge": downstream_handler},
            concurrency=2,
            watchdog_interval=0.1,
        )
        graph = ExecutionGraph([build, downstream])
        result = asyncio.run(executor.run(graph))

        assert "build-a" in result.abandoned
        assert "merge-a" in result.abandoned
        assert downstream_handler.execute.call_count == 0

    def test_auto_abandon_propagates_through_chain(self, config: Config) -> None:
        """Auto-abandonment cascades: build→merge→readme all abandoned."""
        build_handler = mock_handler(success=False, error="failed")
        merge_handler = mock_handler(success=True)
        readme_handler = mock_handler(success=True)

        build = make_node("build", max_retries=1)
        merge = make_node("merge", type="merge", depends_on=["build"])
        readme = make_node("readme", type="readme", depends_on=["merge"])

        executor = GraphExecutor(
            config=config,
            handlers={"build": build_handler, "merge": merge_handler, "readme": readme_handler},
            concurrency=3,
            watchdog_interval=0.1,
        )
        graph = ExecutionGraph([build, merge, readme])
        result = asyncio.run(executor.run(graph))

        assert "build" in result.abandoned
        assert "merge" in result.abandoned
        assert "readme" in result.abandoned
        assert merge_handler.execute.call_count == 0
        assert readme_handler.execute.call_count == 0

    def test_auto_abandon_skipped_when_any_dep_done(self, config: Config) -> None:
        """Node with mixed deps (some done, some abandoned) still runs."""
        build_a_done = make_node("build-a")
        build_b_fail = make_node("build-b", max_retries=1)
        readme = make_node("readme", type="readme", depends_on=["build-a", "build-b"])

        async def execute_build(node, cfg):
            if node.id == "build-a":
                return NodeResult(success=True)
            return NodeResult(success=False, error="failed")

        from unittest.mock import MagicMock
        build_handler = MagicMock()
        build_handler.execute = execute_build
        build_handler.recover = MagicMock(return_value=None)
        readme_handler = mock_handler(success=True)

        executor = GraphExecutor(
            config=config,
            handlers={"build": build_handler, "readme": readme_handler},
            concurrency=3,
            watchdog_interval=0.1,
        )
        graph = ExecutionGraph([build_a_done, build_b_fail, readme])
        result = asyncio.run(executor.run(graph))

        assert "build-a" in result.done
        assert "build-b" in result.abandoned
        assert "readme" in result.done  # ran because build-a succeeded
        assert readme_handler.execute.call_count == 1

    def test_auto_abandon_node_without_deps_not_affected(self, config: Config) -> None:
        """Nodes with no dependencies are never auto-abandoned."""
        handler = mock_handler(success=True)
        graph = ExecutionGraph([make_node("build-a")])
        executor = GraphExecutor(
            config=config,
            handlers={"build": handler},
            concurrency=1,
            watchdog_interval=0.1,
        )
        result = asyncio.run(executor.run(graph))
        assert "build-a" in result.done
        assert handler.execute.call_count == 1
