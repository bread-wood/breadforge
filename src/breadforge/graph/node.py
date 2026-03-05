"""Graph node types, state machine, and NodeHandler protocol.

Re-exports GraphNode/PlanArtifact/NodeType/NodeState from beads.types for
convenience, and adds NodeResult + NodeHandler protocol used by handlers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from breadforge.beads.types import (
    GraphNode,
    NodeState,
    NodeType,
    PlanArtifact,
)

if TYPE_CHECKING:
    from breadforge.config import Config

__all__ = [
    "GraphNode",
    "NodeState",
    "NodeType",
    "NodeResult",
    "NodeHandler",
    "PlanArtifact",
]


class NodeResult:
    """Result returned by a NodeHandler.execute() call.

    abandon=True skips the normal retry cycle and marks the node abandoned immediately.
    Use this when retrying would be pointless (e.g. build dependency already abandoned,
    duplicate PR detected).
    """

    __slots__ = ("success", "output", "error", "abandon")

    def __init__(
        self,
        success: bool,
        output: dict[str, Any] | None = None,
        error: str | None = None,
        abandon: bool = False,
    ) -> None:
        self.success = success
        self.output = output or {}
        self.error = error
        self.abandon = abandon

    def __repr__(self) -> str:
        return f"NodeResult(success={self.success}, error={self.error!r})"


class NodeHandler(Protocol):
    """Protocol that every handler (plan/research/build/merge) must implement."""

    async def execute(self, node: GraphNode, config: Config) -> NodeResult:
        """Execute the node and return a result. Must not mutate graph state."""
        ...

    def recover(self, node: GraphNode, config: Config) -> NodeResult | None:
        """Called for nodes found in 'running' state after a crash/restart.

        Return a NodeResult to mark the node done/failed without re-running it,
        or None to treat the node as pending and re-dispatch normally.
        Default: return None (re-dispatch).
        """
        return None
