"""GraphExecutor — async DAG executor with dynamic graph expansion.

Key design:
- ExecutionGraph tracks nodes and dependency resolution
- _add_overlap_edges adds sequential deps between build nodes that touch the same files
- GraphExecutor drives the async event loop: dispatch ready nodes, handle completions,
  expand graph when plan nodes emit new_nodes
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from breadforge.beads.types import GraphNode, NodeType
from breadforge.graph.node import NodeHandler, NodeResult

if TYPE_CHECKING:
    from breadforge.beads.store import BeadStore
    from breadforge.config import Config
    from breadforge.logger import Logger


# ---------------------------------------------------------------------------
# ExecutionGraph
# ---------------------------------------------------------------------------


class ExecutionGraph:
    """Tracks nodes and dependency state for the executor loop."""

    def __init__(self, nodes: list[GraphNode] | None = None) -> None:
        self._nodes: dict[str, GraphNode] = {}
        if nodes:
            for node in nodes:
                self._nodes[node.id] = node

    def add_nodes(self, nodes: list[GraphNode]) -> None:
        """Dynamically add nodes (e.g. from plan expansion)."""
        for node in nodes:
            if node.id not in self._nodes:
                self._nodes[node.id] = node

    def get_ready(self) -> list[GraphNode]:
        """Return pending nodes whose dependencies are all done/abandoned."""
        terminal: set[str] = {
            nid for nid, n in self._nodes.items() if n.state in ("done", "abandoned")
        }
        ready = []
        for node in self._nodes.values():
            if node.state != "pending":
                continue
            if all(dep in terminal for dep in node.depends_on):
                ready.append(node)
        return ready

    def has_pending(self) -> bool:
        return any(n.state in ("pending", "running") for n in self._nodes.values())

    def get_node(self, node_id: str) -> GraphNode | None:
        return self._nodes.get(node_id)

    def all_nodes(self) -> list[GraphNode]:
        return list(self._nodes.values())


# ---------------------------------------------------------------------------
# Overlap detection
# ---------------------------------------------------------------------------


def _add_overlap_edges(build_nodes: list[GraphNode]) -> list[GraphNode]:
    """Add sequential deps between build nodes touching the same files.

    When multiple build nodes declare the same file in context['files'],
    they are sequenced alphabetically by module name (deterministic).
    """
    nodes_by_id: dict[str, GraphNode] = {n.id: n for n in build_nodes}
    file_owners: dict[str, list[str]] = defaultdict(list)

    for node in build_nodes:
        if node.type != "build":
            continue
        for f in node.context.get("files", []):
            file_owners[f].append(node.id)

    for _file, owners in file_owners.items():
        if len(owners) <= 1:
            continue
        # Sort alphabetically for deterministic ordering
        owners_sorted = sorted(owners)
        for i in range(len(owners_sorted) - 1):
            successor = nodes_by_id[owners_sorted[i + 1]]
            if owners_sorted[i] not in successor.depends_on:
                successor.depends_on.append(owners_sorted[i])

    return build_nodes


# ---------------------------------------------------------------------------
# ExecutionResult
# ---------------------------------------------------------------------------


class ExecutionResult:
    """Summary returned when GraphExecutor.run() completes."""

    def __init__(self) -> None:
        self.done: list[str] = []
        self.failed: list[str] = []
        self.abandoned: list[str] = []

    @property
    def success(self) -> bool:
        return not self.failed and not self.abandoned


# ---------------------------------------------------------------------------
# GraphExecutor
# ---------------------------------------------------------------------------


class GraphExecutor:
    """Async DAG executor."""

    def __init__(
        self,
        config: Config,
        handlers: dict[NodeType, NodeHandler],
        store: BeadStore | None = None,
        logger: Logger | None = None,
        concurrency: int = 3,
        watchdog_interval: float = 60.0,
    ) -> None:
        self._config = config
        self._handlers = handlers
        self._store = store
        self._logger = logger
        self._concurrency = concurrency
        self._watchdog_interval = watchdog_interval

    def _restore_from_store(self, graph: ExecutionGraph, result: ExecutionResult) -> None:
        """Restore terminal node states and replay plan node expansions.

        Walks the initial graph nodes, restoring done/abandoned states from the store.
        For done plan nodes, also replays their stored new_nodes output so the graph
        is fully populated without re-running the plan LLM.
        """
        if not self._store:
            return
        # Process nodes breadth-first: plan node may add new nodes that also need restoring
        seen: set[str] = set()
        queue = list(graph.all_nodes())
        while queue:
            node = queue.pop(0)
            if node.id in seen:
                continue
            seen.add(node.id)
            existing = self._store.read_node(node.id)
            if not existing or existing.state not in ("done", "abandoned"):
                continue
            node.state = existing.state  # type: ignore[assignment]
            node.output = existing.output
            node.retry_count = existing.retry_count
            if existing.state == "done":
                result.done.append(node.id)
            else:
                result.abandoned.append(node.id)
            # Replay plan node expansion so dependents are in the graph
            if node.type == "plan" and existing.output and existing.output.get("new_nodes"):
                new = [GraphNode(**n) for n in existing.output["new_nodes"]]
                new = _add_overlap_edges(new)
                graph.add_nodes(new)
                queue.extend(new)  # also restore their states

    def _recover_running_nodes(self, graph: ExecutionGraph, result: ExecutionResult) -> None:
        """For nodes the store recorded as 'running' (crashed mid-flight), ask each
        handler if it can recover state without re-running. BuildHandler checks for
        an existing PR on the branch; all others return None (re-dispatch)."""
        if not self._store:
            return
        for node in graph.all_nodes():
            if node.state != "pending":
                continue  # already restored or not relevant
            stored = self._store.read_node(node.id)
            if not stored or stored.state != "running":
                continue
            handler = self._handlers.get(node.type)
            if handler is None:
                continue
            recovery = handler.recover(node, self._config)
            if recovery is None:
                continue  # re-dispatch normally
            node.output = recovery.output
            if recovery.success:
                node.state = "done"  # type: ignore[assignment]
                result.done.append(node.id)
                self._log_info(f"recovered node {node.id} as done (was running at crash)")
            else:
                node.retry_count = stored.retry_count
                # leave as pending — will be re-dispatched
            self._store.write_node(node)

    async def run(self, graph: ExecutionGraph) -> ExecutionResult:
        result = ExecutionResult()
        self._restore_from_store(graph, result)
        self._recover_running_nodes(graph, result)
        active: dict[str, asyncio.Task[NodeResult]] = {}

        while graph.has_pending() or active:
            # Fill concurrency slots
            ready = graph.get_ready()
            for node in ready[: self._concurrency - len(active)]:
                if node.id in active:
                    continue
                node.state = "running"  # type: ignore[assignment]
                node.touch_started()
                if self._store:
                    self._store.write_node(node)
                active[node.id] = asyncio.create_task(self._dispatch(node), name=node.id)

            if not active:
                # No active tasks and nothing became ready — check for deadlock
                if graph.has_pending():
                    self._log_error("executor deadlock: pending nodes with no ready tasks")
                break

            done_tasks, _ = await asyncio.wait(
                list(active.values()),
                timeout=self._watchdog_interval,
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in done_tasks:
                node_id = task.get_name()
                active.pop(node_id, None)
                node = graph.get_node(node_id)
                if node is None:
                    continue

                try:
                    node_result = task.result()
                except Exception as e:
                    node_result = NodeResult(success=False, error=str(e))

                await self._handle_completion(node, node_result, graph, result)

        return result

    async def _dispatch(self, node: GraphNode) -> NodeResult:
        handler = self._handlers.get(node.type)
        if handler is None:
            return NodeResult(success=False, error=f"no handler for type {node.type!r}")
        try:
            return await handler.execute(node, self._config)
        except Exception as e:
            return NodeResult(success=False, error=str(e))

    async def _handle_completion(
        self,
        node: GraphNode,
        result: NodeResult,
        graph: ExecutionGraph,
        exec_result: ExecutionResult,
    ) -> None:
        node.touch_completed()
        node.output = result.output

        if result.success:
            node.state = "done"  # type: ignore[assignment]
            exec_result.done.append(node.id)
            self._log_info(f"node {node.id} done")

            # Plan nodes may emit new nodes
            if node.type == "plan" and result.output.get("new_nodes"):
                new = [GraphNode(**n) for n in result.output["new_nodes"]]
                new = _add_overlap_edges(new)
                # Restore terminal states before adding to graph (skip completed work)
                if self._store:
                    for n in new:
                        existing = self._store.read_node(n.id)
                        if existing and existing.state in ("done", "abandoned"):
                            n.state = existing.state  # type: ignore[assignment]
                            n.output = existing.output
                            n.retry_count = existing.retry_count
                            if n.state == "done":
                                result.done.append(n.id)
                            else:
                                result.abandoned.append(n.id)
                graph.add_nodes(new)
                if self._store:
                    for n in new:
                        if n.state == "pending":  # only write new nodes, not restored ones
                            self._store.write_node(n)
                self._log_info(f"plan {node.id} expanded graph: +{len(new)} nodes")
        else:
            node.retry_count += 1
            if node.retry_count < node.max_retries:
                node.state = "pending"  # type: ignore[assignment]
                self._log_info(f"node {node.id} failed (attempt {node.retry_count}), re-queuing")
            else:
                node.state = "abandoned"  # type: ignore[assignment]
                exec_result.abandoned.append(node.id)
                self._log_error(
                    f"node {node.id} abandoned after {node.retry_count} attempts: {result.error}"
                )

        if self._store:
            self._store.write_node(node)

    def _log_info(self, message: str, **kwargs: Any) -> None:
        if self._logger:
            self._logger.info(message, **kwargs)

    def _log_error(self, message: str, **kwargs: Any) -> None:
        if self._logger:
            self._logger.error(message, **kwargs)


# ---------------------------------------------------------------------------
# make_handlers factory
# ---------------------------------------------------------------------------


def make_handlers(
    store: BeadStore | None = None,
    logger: Logger | None = None,
) -> dict[NodeType, NodeHandler]:
    """Instantiate all handlers. Import lazily to avoid circular deps."""
    from breadforge.graph.handlers.build import BuildHandler
    from breadforge.graph.handlers.merge import MergeHandler
    from breadforge.graph.handlers.plan import PlanHandler
    from breadforge.graph.handlers.readme import ReadmeHandler
    from breadforge.graph.handlers.research import ResearchHandler

    return {
        "plan": PlanHandler(store=store, logger=logger),
        "research": ResearchHandler(store=store, logger=logger),
        "build": BuildHandler(store=store, logger=logger),
        "merge": MergeHandler(store=store, logger=logger),
        "readme": ReadmeHandler(store=store, logger=logger),
    }
