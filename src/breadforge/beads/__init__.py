"""beads — atomic state tracking for breadforge.

Re-exports all public types for backward compatibility.
"""

from breadforge.beads.store import BeadStore
from breadforge.beads.types import (
    CampaignBead,
    GraphNode,
    MergeQueue,
    MergeQueueItem,
    MilestonePlan,
    MilestoneStatus,
    NodeState,
    NodeType,
    PlanArtifact,
    PRBead,
    PRState,
    WorkBead,
    WorkState,
)

__all__ = [
    "BeadStore",
    "CampaignBead",
    "GraphNode",
    "MergeQueue",
    "MergeQueueItem",
    "MilestonePlan",
    "MilestoneStatus",
    "NodeState",
    "NodeType",
    "PRBead",
    "PRState",
    "PlanArtifact",
    "WorkBead",
    "WorkState",
]
