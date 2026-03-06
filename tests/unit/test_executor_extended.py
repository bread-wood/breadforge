"""Extended executor tests — dry-run, BackendRouter, recover-running, abandon flag,
store-persist-on-rerun, error-in-task, logger integration, and make_handlers factory."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from breadforge.beads import BeadStore, GraphNode
from breadforge.config import Config
from breadforge.graph.executor import ExecutionGraph, GraphExecutor, _add_overlap_edges, make_handlers
from breadforge.graph.node import BackendRouter, NodeResult
from breadforge.logger import Logger


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> Config:
    return Config.from_env("owner/repo")


@pytest.fixture
def store(tmp_path: Path) -> BeadStore:
    return BeadStore(tmp_path / "beads", "owner/repo")


def make_node(
    id: str,
    type: str = "build",
    depends_on: list[str] | None = None,
    files: list[str] | None = None,
    max_retries: int = 3,
) -> GraphNode:
    context: dict = {}
    if files is not None:
        context["files"] = files
    return GraphNode(
        id=id,
        type=type,  # type: ignore[arg-type]
        depends_on=depends_on or [],
        context=context,
        max_retries=max_retries,
    )


def mock_handler(success: bool = True, output: dict | None = None, error: str | None = None, abandon: bool = False):
    handler = AsyncMock()
    handler.execute = AsyncMock(
        return_value=NodeResult(success=success, output=output or {}, error=error, abandon=abandon)
    )
    handler.recover = MagicMock(return_value=None)
    return handler


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_build_node_not_dispatched(self, config: Config) -> None:
        handler = mock_handler(success=True)
        executor = GraphExecutor(
            config=config,
            handlers={"build": handler},
            concurrency=2,
            watchdog_interval=0.1,
            dry_run=True,
        )
        graph = ExecutionGraph([make_node("build-a")])
        result = asyncio.run(executor.run(graph))
        assert result.success
        assert "build-a" in result.done
        assert handler.execute.call_count == 0  # skipped

    def test_merge_node_skipped_dry_run(self, config: Config) -> None:
        handler = mock_handler(success=True)
        executor = GraphExecutor(
            config=config,
            handlers={"merge": handler},
            concurrency=2,
            watchdog_interval=0.1,
            dry_run=True,
        )
        graph = ExecutionGraph([make_node("merge-a", type="merge")])
        result = asyncio.run(executor.run(graph))
        assert result.success
        assert handler.execute.call_count == 0

    def test_readme_node_skipped_dry_run(self, config: Config) -> None:
        handler = mock_handler(success=True)
        executor = GraphExecutor(
            config=config,
            handlers={"readme": handler},
            concurrency=2,
            watchdog_interval=0.1,
            dry_run=True,
        )
        graph = ExecutionGraph([make_node("readme", type="readme")])
        result = asyncio.run(executor.run(graph))
        assert result.success
        assert handler.execute.call_count == 0

    def test_plan_node_runs_in_dry_run(self, config: Config) -> None:
        plan_handler = mock_handler(success=True, output={"new_nodes": []})
        executor = GraphExecutor(
            config=config,
            handlers={"plan": plan_handler},
            concurrency=2,
            watchdog_interval=0.1,
            dry_run=True,
        )
        graph = ExecutionGraph([make_node("v1-plan", type="plan")])
        result = asyncio.run(executor.run(graph))
        assert result.success
        assert plan_handler.execute.call_count == 1  # plan always runs

    def test_dry_run_build_creates_work_bead(self, config: Config, store: BeadStore) -> None:
        """Build node in dry-run should create a WorkBead when issue_number is set."""
        node = make_node("build-core")
        node.context["issue_number"] = 99
        node.context["module"] = "core"
        node.context["issue_title"] = "impl: core"

        executor = GraphExecutor(
            config=config,
            handlers={"build": mock_handler(success=True)},
            store=store,
            concurrency=1,
            watchdog_interval=0.1,
            dry_run=True,
        )
        graph = ExecutionGraph([node])
        asyncio.run(executor.run(graph))
        bead = store.read_work_bead(99)
        assert bead is not None
        assert bead.issue_number == 99

    def test_dry_run_does_not_persist_nodes(self, config: Config, store: BeadStore) -> None:
        executor = GraphExecutor(
            config=config,
            handlers={"build": mock_handler(success=True)},
            store=store,
            concurrency=1,
            watchdog_interval=0.1,
            dry_run=True,
        )
        graph = ExecutionGraph([make_node("build-a")])
        asyncio.run(executor.run(graph))
        # Should NOT have written to the store in dry-run
        assert store.read_node("build-a") is None

    def test_dry_run_plan_expands_graph(self, config: Config) -> None:
        new_nodes = [
            {
                "id": "build-dry",
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
            }
        ]
        plan_handler = mock_handler(success=True, output={"new_nodes": new_nodes})
        executor = GraphExecutor(
            config=config,
            handlers={"plan": plan_handler, "build": mock_handler(success=True)},
            concurrency=2,
            watchdog_interval=0.1,
            dry_run=True,
        )
        graph = ExecutionGraph([make_node("v1-plan", type="plan")])
        result = asyncio.run(executor.run(graph))
        assert result.success
        assert "build-dry" in result.done


# ---------------------------------------------------------------------------
# BackendRouter integration
# ---------------------------------------------------------------------------


class TestBackendRouterIntegration:
    def test_router_overrides_model_for_research(self, config: Config) -> None:
        """BackendRouter should change effective_config.model for research nodes."""
        captured_configs: list[Config] = []

        async def execute(node, cfg):
            captured_configs.append(cfg)
            return NodeResult(success=True)

        handler = MagicMock()
        handler.execute = execute
        handler.recover = MagicMock(return_value=None)

        router = BackendRouter(build_model="claude-sonnet-4-6", research_model="gemini-2.5-pro")
        executor = GraphExecutor(
            config=config,
            handlers={"research": handler},
            concurrency=1,
            watchdog_interval=0.1,
            backend_router=router,
        )
        graph = ExecutionGraph([make_node("v1-research", type="research")])
        asyncio.run(executor.run(graph))
        assert len(captured_configs) == 1
        assert captured_configs[0].model == "gemini-2.5-pro"

    def test_assigned_model_skips_router(self, config: Config) -> None:
        """Node with assigned_model should NOT be overridden by BackendRouter."""
        captured_configs: list[Config] = []

        async def execute(node, cfg):
            captured_configs.append(cfg)
            return NodeResult(success=True)

        handler = MagicMock()
        handler.execute = execute
        handler.recover = MagicMock(return_value=None)

        router = BackendRouter(build_model="claude-sonnet-4-6", research_model="gemini-2.5-pro")
        executor = GraphExecutor(
            config=config,
            handlers={"research": handler},
            concurrency=1,
            watchdog_interval=0.1,
            backend_router=router,
        )
        node = make_node("v1-research", type="research")
        node.assigned_model = "claude-opus-4-6"  # pre-assigned; router must not override
        graph = ExecutionGraph([node])
        asyncio.run(executor.run(graph))
        assert captured_configs[0].model == config.model  # original model unchanged

    def test_no_router_uses_default_config(self, config: Config) -> None:
        captured: list[Config] = []

        async def execute(node, cfg):
            captured.append(cfg)
            return NodeResult(success=True)

        handler = MagicMock()
        handler.execute = execute
        handler.recover = MagicMock(return_value=None)

        executor = GraphExecutor(
            config=config,
            handlers={"build": handler},
            concurrency=1,
            watchdog_interval=0.1,
            backend_router=None,
        )
        graph = ExecutionGraph([make_node("build-a")])
        asyncio.run(executor.run(graph))
        assert captured[0].model == config.model


# ---------------------------------------------------------------------------
# Abandon flag (skip retries immediately)
# ---------------------------------------------------------------------------


class TestAbandonFlag:
    def test_abandon_true_skips_retries(self, config: Config) -> None:
        handler = mock_handler(success=False, error="dep gone", abandon=True)
        executor = GraphExecutor(
            config=config,
            handlers={"build": handler},
            concurrency=1,
            watchdog_interval=0.1,
        )
        node = make_node("build-a", max_retries=5)
        graph = ExecutionGraph([node])
        result = asyncio.run(executor.run(graph))
        assert "build-a" in result.abandoned
        assert handler.execute.call_count == 1  # never retried


# ---------------------------------------------------------------------------
# Recover running nodes
# ---------------------------------------------------------------------------


class TestRecoverRunningNodes:
    def test_recover_running_marks_done(self, config: Config, store: BeadStore) -> None:
        node = make_node("build-a")
        node.state = "running"  # type: ignore[assignment]
        store.write_node(node)

        recovery = NodeResult(success=True, output={"pr": 42})
        handler = AsyncMock()
        handler.execute = AsyncMock(return_value=NodeResult(success=True))
        handler.recover = MagicMock(return_value=recovery)

        executor = GraphExecutor(
            config=config,
            handlers={"build": handler},
            store=store,
            concurrency=1,
            watchdog_interval=0.1,
        )
        graph = ExecutionGraph([make_node("build-a")])
        result = asyncio.run(executor.run(graph))
        assert "build-a" in result.done
        # Node was recovered; execute should not have been called
        assert handler.execute.call_count == 0

    def test_recover_running_none_redispatches(self, config: Config, store: BeadStore) -> None:
        node = make_node("build-a")
        node.state = "running"  # type: ignore[assignment]
        store.write_node(node)

        handler = AsyncMock()
        handler.execute = AsyncMock(return_value=NodeResult(success=True))
        handler.recover = MagicMock(return_value=None)  # re-dispatch

        executor = GraphExecutor(
            config=config,
            handlers={"build": handler},
            store=store,
            concurrency=1,
            watchdog_interval=0.1,
        )
        graph = ExecutionGraph([make_node("build-a")])
        result = asyncio.run(executor.run(graph))
        assert "build-a" in result.done
        assert handler.execute.call_count == 1  # was re-dispatched

    def test_recover_running_no_handler_skipped(self, config: Config, store: BeadStore) -> None:
        """Running node with no registered handler is left pending (re-dispatched will fail)."""
        node = make_node("build-x", type="build")
        node.state = "running"  # type: ignore[assignment]
        store.write_node(node)

        executor = GraphExecutor(
            config=config,
            handlers={},  # no handler for "build"
            store=store,
            concurrency=1,
            watchdog_interval=0.1,
        )
        graph = ExecutionGraph([make_node("build-x", type="build", max_retries=1)])
        result = asyncio.run(executor.run(graph))
        # Node re-dispatched → fails (no handler) → abandoned
        assert "build-x" in result.abandoned

    def test_recover_running_failure_result_sets_retry_count(self, config: Config, store: BeadStore) -> None:
        """If handler.recover() returns failure, retry_count is inherited from stored value."""
        node = make_node("build-a")
        node.state = "running"  # type: ignore[assignment]
        node.retry_count = 2
        store.write_node(node)

        # recovery says failed — node should be re-dispatched with retry_count from store
        recovery = NodeResult(success=False, error="pr not found")
        handler = AsyncMock()
        handler.execute = AsyncMock(return_value=NodeResult(success=True))
        handler.recover = MagicMock(return_value=recovery)

        executor = GraphExecutor(
            config=config,
            handlers={"build": handler},
            store=store,
            concurrency=1,
            watchdog_interval=0.1,
        )
        graph = ExecutionGraph([make_node("build-a", max_retries=5)])
        result = asyncio.run(executor.run(graph))
        assert "build-a" in result.done  # re-dispatched and succeeds this time

    def test_no_store_skips_recovery(self, config: Config) -> None:
        handler = mock_handler(success=True)
        executor = GraphExecutor(
            config=config,
            handlers={"build": handler},
            store=None,
            concurrency=1,
            watchdog_interval=0.1,
        )
        graph = ExecutionGraph([make_node("build-a")])
        result = asyncio.run(executor.run(graph))
        assert result.success


# ---------------------------------------------------------------------------
# Retry with in-flight recovery
# ---------------------------------------------------------------------------


class TestRetryRecovery:
    def test_handler_recover_on_retry_success(self, config: Config) -> None:
        """If a node fails once and handler.recover() returns success on retry, mark done."""
        recovery = NodeResult(success=True, output={"pr": 99})
        handler = AsyncMock()
        handler.execute = AsyncMock(return_value=NodeResult(success=False, error="failed"))
        handler.recover = MagicMock(return_value=recovery)

        executor = GraphExecutor(
            config=config,
            handlers={"build": handler},
            concurrency=1,
            watchdog_interval=0.1,
        )
        node = make_node("build-a", max_retries=3)
        graph = ExecutionGraph([node])
        result = asyncio.run(executor.run(graph))
        assert "build-a" in result.done
        assert handler.execute.call_count == 1
        assert handler.recover.call_count >= 1


# ---------------------------------------------------------------------------
# Task exception handling
# ---------------------------------------------------------------------------


class TestTaskException:
    def test_exception_in_task_treated_as_failure(self, config: Config) -> None:
        handler = AsyncMock()
        handler.execute = AsyncMock(side_effect=RuntimeError("handler crashed"))
        handler.recover = MagicMock(return_value=None)

        executor = GraphExecutor(
            config=config,
            handlers={"build": handler},
            concurrency=1,
            watchdog_interval=0.1,
        )
        node = make_node("build-a", max_retries=1)
        graph = ExecutionGraph([node])
        result = asyncio.run(executor.run(graph))
        assert "build-a" in result.abandoned
        assert not result.success


# ---------------------------------------------------------------------------
# No handler for node type
# ---------------------------------------------------------------------------


class TestNoHandler:
    def test_missing_handler_abandons_node(self, config: Config) -> None:
        executor = GraphExecutor(
            config=config,
            handlers={},  # no handlers
            concurrency=1,
            watchdog_interval=0.1,
        )
        node = make_node("build-a", max_retries=1)
        graph = ExecutionGraph([node])
        result = asyncio.run(executor.run(graph))
        assert "build-a" in result.abandoned


# ---------------------------------------------------------------------------
# Restore abandoned → pending writes to store
# ---------------------------------------------------------------------------


class TestRestoreAbandonedWritesPending:
    def test_abandoned_node_reset_to_pending_in_store(self, config: Config, store: BeadStore) -> None:
        """After _restore_from_store, abandoned nodes should be written back as pending."""
        node = make_node("build-a")
        node.state = "abandoned"  # type: ignore[assignment]
        store.write_node(node)

        handler = mock_handler(success=True)
        executor = GraphExecutor(
            config=config,
            handlers={"build": handler},
            store=store,
            concurrency=1,
            watchdog_interval=0.1,
        )
        graph = ExecutionGraph([make_node("build-a")])
        result = asyncio.run(executor.run(graph))
        assert "build-a" in result.done
        persisted = store.read_node("build-a")
        assert persisted is not None
        assert persisted.state == "done"

    def test_store_restore_done_plan_expands_graph(self, config: Config, store: BeadStore) -> None:
        """A done plan node in store re-expands the graph so build nodes are added."""
        new_node_data = {
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
        }
        # Simulate a previous run: plan done, build pending (not in store)
        plan_node = make_node("v1-plan", type="plan")
        plan_node.state = "done"  # type: ignore[assignment]
        plan_node.output = {"new_nodes": [new_node_data]}
        store.write_node(plan_node)

        build_handler = mock_handler(success=True)
        executor = GraphExecutor(
            config=config,
            handlers={"plan": mock_handler(success=True), "build": build_handler},
            store=store,
            concurrency=2,
            watchdog_interval=0.1,
        )
        graph = ExecutionGraph([make_node("v1-plan", type="plan")])
        result = asyncio.run(executor.run(graph))
        assert "v1-plan" in result.done
        assert "build-core" in result.done
        # Build was dispatched (not skipped, not restored)
        assert build_handler.execute.call_count == 1

    def test_plan_expansion_restores_done_build_from_store(self, config: Config, store: BeadStore) -> None:
        """Plan handler returns new_nodes; if a build node is already done in store, skip it."""
        new_node_data = {
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
        }
        # Build node already done in store from a previous run
        build_in_store = make_node("build-core")
        build_in_store.state = "done"  # type: ignore[assignment]
        build_in_store.output = {"pr": 42}
        store.write_node(build_in_store)

        plan_handler = mock_handler(success=True, output={"new_nodes": [new_node_data]})
        build_handler = mock_handler(success=True)
        executor = GraphExecutor(
            config=config,
            handlers={"plan": plan_handler, "build": build_handler},
            store=store,
            concurrency=2,
            watchdog_interval=0.1,
        )
        graph = ExecutionGraph([make_node("v1-plan", type="plan")])
        result = asyncio.run(executor.run(graph))
        assert "build-core" in result.done
        assert build_handler.execute.call_count == 0  # already done, not re-dispatched

    def test_plan_expansion_restores_abandoned_build_from_store(self, config: Config, store: BeadStore) -> None:
        """Plan handler returns new_nodes; abandoned build from store counted in abandoned."""
        new_node_data = {
            "id": "build-fail",
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
        }
        build_in_store = make_node("build-fail")
        build_in_store.state = "abandoned"  # type: ignore[assignment]
        store.write_node(build_in_store)

        plan_handler = mock_handler(success=True, output={"new_nodes": [new_node_data]})
        build_handler = mock_handler(success=True)
        executor = GraphExecutor(
            config=config,
            handlers={"plan": plan_handler, "build": build_handler},
            store=store,
            concurrency=2,
            watchdog_interval=0.1,
        )
        graph = ExecutionGraph([make_node("v1-plan", type="plan")])
        result = asyncio.run(executor.run(graph))
        assert "build-fail" in result.abandoned
        assert build_handler.execute.call_count == 0  # was abandoned, not re-dispatched

    def test_abandoned_node_shows_pending_after_restore(self, config: Config, store: BeadStore) -> None:
        """The bead store should NOT show abandoned mid-run for a retried node."""
        node = make_node("build-a")
        node.state = "abandoned"  # type: ignore[assignment]
        store.write_node(node)

        # Trap the state right after _restore_from_store but before dispatch
        states_seen: list[str] = []

        original_restore = GraphExecutor._restore_from_store

        def patched_restore(self, graph, result):
            original_restore(self, graph, result)
            # Read back from store after restore
            n = self._store.read_node("build-a")
            if n:
                states_seen.append(n.state)

        import unittest.mock as um
        with um.patch.object(GraphExecutor, "_restore_from_store", patched_restore):
            executor = GraphExecutor(
                config=config,
                handlers={"build": mock_handler(success=True)},
                store=store,
                concurrency=1,
                watchdog_interval=0.1,
            )
            graph = ExecutionGraph([make_node("build-a")])
            asyncio.run(executor.run(graph))

        assert "pending" in states_seen, f"Expected pending in store after restore, got {states_seen}"


# ---------------------------------------------------------------------------
# Logger integration (log paths covered when logger is set)
# ---------------------------------------------------------------------------


class TestExecutorWithLogger:
    def test_logger_receives_events(self, config: Config, tmp_path) -> None:
        log_path = tmp_path / "test.jsonl"
        logger = Logger(log_path, run_id="test")

        handler = mock_handler(success=True)
        executor = GraphExecutor(
            config=config,
            handlers={"build": handler},
            concurrency=1,
            watchdog_interval=0.1,
            logger=logger,
        )
        graph = ExecutionGraph([make_node("build-a")])
        result = asyncio.run(executor.run(graph))
        assert result.success
        # Log file should have been written
        assert log_path.exists()

    def test_logger_records_abandonment(self, config: Config, tmp_path) -> None:
        import json

        log_path = tmp_path / "test.jsonl"
        logger = Logger(log_path, run_id="test")

        handler = mock_handler(success=False, error="failed")
        executor = GraphExecutor(
            config=config,
            handlers={"build": handler},
            concurrency=1,
            watchdog_interval=0.1,
            logger=logger,
        )
        graph = ExecutionGraph([make_node("build-a", max_retries=1)])
        result = asyncio.run(executor.run(graph))
        assert "build-a" in result.abandoned
        records = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        events = [r["event"] for r in records]
        assert "error" in events

    def test_logger_records_auto_abandon(self, config: Config, tmp_path) -> None:
        import json

        log_path = tmp_path / "test.jsonl"
        logger = Logger(log_path, run_id="test")

        build_handler = mock_handler(success=False, error="failed")
        merge_handler = mock_handler(success=True)

        executor = GraphExecutor(
            config=config,
            handlers={"build": build_handler, "merge": merge_handler},
            concurrency=2,
            watchdog_interval=0.1,
            logger=logger,
        )
        build = make_node("build", max_retries=1)
        merge = make_node("merge", type="merge", depends_on=["build"])
        graph = ExecutionGraph([build, merge])
        result = asyncio.run(executor.run(graph))
        assert "merge" in result.abandoned
        records = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        info_msgs = [r.get("message", "") for r in records if r["event"] == "info"]
        assert any("auto-abandoned" in m for m in info_msgs)


# ---------------------------------------------------------------------------
# make_handlers factory
# ---------------------------------------------------------------------------


class TestMakeHandlersFactory:
    def test_returns_all_expected_types(self, tmp_path: Path) -> None:
        store = BeadStore(tmp_path / "beads", "owner/repo")
        handlers = make_handlers(store=store)
        for expected in ("plan", "research", "build", "merge", "readme", "wait", "consensus", "design_doc"):
            assert expected in handlers, f"Missing handler for type: {expected}"

    def test_returns_handlers_without_store(self) -> None:
        handlers = make_handlers()
        assert "plan" in handlers
        assert "build" in handlers


# ---------------------------------------------------------------------------
# Store write paths in run loop (lines 239, 249)
# ---------------------------------------------------------------------------


class TestStoreWriteDuringRun:
    def test_store_written_when_node_starts_running(self, config: Config, store: BeadStore) -> None:
        """When a node transitions to running, its state is persisted to the store."""
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
        # After run, persisted state should be "done"
        persisted = store.read_node("build-a")
        assert persisted is not None
        assert persisted.state == "done"

    def test_auto_abandon_written_to_store(self, config: Config, store: BeadStore) -> None:
        """Auto-abandoned nodes should be persisted to the store."""
        build_handler = mock_handler(success=False, error="failed")
        merge_handler = mock_handler(success=True)

        build = make_node("build-a", max_retries=1)
        merge = make_node("merge-a", type="merge", depends_on=["build-a"])

        executor = GraphExecutor(
            config=config,
            handlers={"build": build_handler, "merge": merge_handler},
            store=store,
            concurrency=2,
            watchdog_interval=0.1,
        )
        graph = ExecutionGraph([build, merge])
        result = asyncio.run(executor.run(graph))

        assert "merge-a" in result.abandoned
        persisted = store.read_node("merge-a")
        assert persisted is not None
        assert persisted.state == "abandoned"
