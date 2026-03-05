"""Bead system — shim for backward compatibility.

All types and BeadStore now live in breadforge.beads (the sub-package).
This module re-exports everything so existing imports continue to work.
"""

from breadforge.beads import (  # noqa: F401  # re-export
    BeadStore,
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
