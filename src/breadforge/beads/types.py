"""Bead type definitions — all Pydantic models for bead state tracking.

Includes existing bead types (WorkBead, PRBead, MergeQueue, CampaignBead)
plus new graph execution types (GraphNode, PlanArtifact).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# WorkBead
# ---------------------------------------------------------------------------

WorkState = Literal["open", "claimed", "pr_open", "merge_ready", "closed", "abandoned"]


class WorkBead(BaseModel):
    issue_number: int
    repo: str
    title: str
    stage: Literal["impl"] = "impl"
    state: WorkState = "open"
    branch: str | None = None
    pr_number: int | None = None
    retry_count: int = 0
    blocked_by: list[str] = Field(default_factory=list)
    """Cross-repo blocking deps: 'owner/repo:milestone' strings."""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    milestone: str | None = None
    spec_file: str | None = None
    node_id: str | None = None
    """GraphNode id that produced this work item, if dispatched via DAG executor."""
    model: str | None = None
    """Model selected by the assessor for the build agent."""

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC)


# ---------------------------------------------------------------------------
# PRBead
# ---------------------------------------------------------------------------

PRState = Literal[
    "open", "ci_failing", "conflict", "reviewing", "merge_ready", "merged", "abandoned"
]


class PRBead(BaseModel):
    pr_number: int
    repo: str
    issue_number: int
    branch: str
    state: PRState = "open"
    ci_attempts: int = 0
    review_attempts: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC)


# ---------------------------------------------------------------------------
# MergeQueue
# ---------------------------------------------------------------------------


class MergeQueueItem(BaseModel):
    pr_number: int
    issue_number: int
    branch: str
    enqueued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MergeQueue(BaseModel):
    repo: str
    items: list[MergeQueueItem] = Field(default_factory=list)

    def enqueue(self, item: MergeQueueItem) -> None:
        if not any(i.pr_number == item.pr_number for i in self.items):
            self.items.append(item)

    def dequeue(self) -> MergeQueueItem | None:
        if self.items:
            return self.items.pop(0)
        return None

    def peek(self) -> MergeQueueItem | None:
        return self.items[0] if self.items else None


# ---------------------------------------------------------------------------
# CampaignBead
# ---------------------------------------------------------------------------

MilestoneStatus = Literal["pending", "planning", "implementing", "shipped", "blocked", "failed"]


class MilestonePlan(BaseModel):
    milestone: str
    repo: str
    spec_file: str | None = None
    status: MilestoneStatus = "pending"
    wave: int = 0
    blocked_by: list[str] = Field(default_factory=list)
    """'owner/repo:milestone' strings that must be shipped first."""
    started_at: datetime | None = None
    shipped_at: datetime | None = None


class CampaignBead(BaseModel):
    repo: str
    milestones: list[MilestonePlan] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC)

    def get_milestone(self, milestone: str, repo: str | None = None) -> MilestonePlan | None:
        for m in self.milestones:
            if m.milestone == milestone and (repo is None or m.repo == repo):
                return m
        return None

    def pending_in_wave(self, wave: int) -> list[MilestonePlan]:
        return [m for m in self.milestones if m.wave == wave and m.status == "pending"]

    def all_shipped_in_wave(self, wave: int) -> bool:
        wave_items = [m for m in self.milestones if m.wave == wave]
        return all(m.status == "shipped" for m in wave_items)


# ---------------------------------------------------------------------------
# GraphNode + PlanArtifact (new DAG execution types)
# ---------------------------------------------------------------------------

NodeType = Literal[
    "research", "plan", "build", "merge", "readme", "wait", "consensus", "design_doc"
]
NodeState = Literal["pending", "running", "done", "failed", "abandoned"]


class PlanArtifact(BaseModel):
    """Structured output from the plan handler — drives graph expansion."""

    milestone: str
    modules: list[str]
    files_per_module: dict[str, list[str]]
    """module → explicit file paths the build node may touch."""
    approach: str
    confidence: float = Field(ge=0.0, le=1.0)
    """< 0.6 → emit research nodes before building."""
    unknowns: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    """e.g. 'novel-domain', 'security', 'multi-module-coordination'."""


class GraphNode(BaseModel):
    """A single node in the execution DAG."""

    id: str
    """'{milestone}-{type}-{module}' or '{milestone}-plan'."""
    type: NodeType
    state: NodeState = "pending"
    depends_on: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] | None = None
    assigned_model: str | None = None
    retry_count: int = 0
    max_retries: int = 3
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def touch_started(self) -> None:
        self.started_at = datetime.now(UTC)

    def touch_completed(self) -> None:
        self.completed_at = datetime.now(UTC)
