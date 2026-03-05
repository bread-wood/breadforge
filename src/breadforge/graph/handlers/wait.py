"""WaitHandler — condition-based DAG gate node.

Polls a simple condition stored in node.context until it is satisfied or
max_polls attempts are exhausted.  This is the general-purpose wait node used
to implement synchronisation gates inside a single-repo DAG.  For cross-repo
milestone blocking, see the CampaignBead-aware WaitHandler in consensus.py.

Context keys
------------
condition : str
    One of ``"always_true"``, ``"always_false"``, ``"file_exists"``.
    Unknown values are treated as unsatisfied.
path : str
    File path tested by the ``"file_exists"`` condition.
poll_interval : float
    Seconds to sleep between poll attempts (default 0.05).
max_polls : int
    Maximum number of poll attempts before returning failure (default 3).

Output keys (on success)
------------------------
polls : int   — number of attempts before the condition was met
condition : str — the condition string that was evaluated
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from breadforge.beads.types import GraphNode
from breadforge.graph.node import NodeResult

if TYPE_CHECKING:
    from breadforge.config import Config


class WaitHandler:
    """Polls a condition until satisfied or max_polls exhausted."""

    def __init__(self, store=None, logger=None) -> None:
        self._store = store
        self._logger = logger

    async def execute(self, node: GraphNode, config: Config) -> NodeResult:
        condition: str = node.context.get("condition", "always_true")
        poll_interval: float = float(node.context.get("poll_interval", 0.05))
        max_polls: int = int(node.context.get("max_polls", 3))

        for attempt in range(1, max_polls + 1):
            if self._check(condition, node):
                if self._logger:
                    self._logger.info(
                        f"wait node {node.id}: condition {condition!r} met after {attempt} poll(s)",
                        node_id=node.id,
                    )
                return NodeResult(
                    success=True,
                    output={"polls": attempt, "condition": condition},
                )
            if attempt < max_polls:
                await asyncio.sleep(poll_interval)

        if self._logger:
            self._logger.info(
                f"wait node {node.id}: condition {condition!r} not met after {max_polls} polls",
                node_id=node.id,
            )
        return NodeResult(
            success=False,
            error=f"wait condition {condition!r} not met after {max_polls} polls",
        )

    def _check(self, condition: str, node: GraphNode) -> bool:
        if condition == "always_true":
            return True
        if condition == "always_false":
            return False
        if condition == "file_exists":
            return Path(str(node.context.get("path", ""))).exists()
        return False

    def recover(self, node: GraphNode, config: Config) -> NodeResult | None:
        """Re-dispatch on restart — wait conditions must be re-evaluated."""
        return None
