"""Bead system — atomic state tracking for issues, PRs, and campaigns.

Beads are the canonical source of truth. GitHub issues/PRs are inputs and outputs;
beads are the internal ledger.

Layout:
  ~/.breadforge/beads/<owner>/<repo>/work/<N>.json      WorkBead
  ~/.breadforge/beads/<owner>/<repo>/prs/pr-<N>.json    PRBead
  ~/.breadforge/beads/<owner>/<repo>/merge-queue.json   MergeQueue
  ~/.breadforge/beads/<owner>/<repo>/campaign.json      CampaignBead
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

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
# BeadStore
# ---------------------------------------------------------------------------


class BeadStore:
    """Atomic bead reads/writes using write-to-tmp + os.replace."""

    def __init__(self, beads_dir: Path, repo: str) -> None:
        owner, name = repo.split("/", 1)
        self._root = beads_dir / owner / name
        self._work_dir = self._root / "work"
        self._prs_dir = self._root / "prs"
        self._root.mkdir(parents=True, exist_ok=True)
        self._work_dir.mkdir(exist_ok=True)
        self._prs_dir.mkdir(exist_ok=True)

    # --- Work beads ---

    def _work_path(self, issue_number: int) -> Path:
        return self._work_dir / f"{issue_number}.json"

    def write_work_bead(self, bead: WorkBead) -> None:
        bead.touch()
        self._atomic_write(self._work_path(bead.issue_number), bead.model_dump(mode="json"))

    def read_work_bead(self, issue_number: int) -> WorkBead | None:
        path = self._work_path(issue_number)
        if not path.exists():
            return None
        return WorkBead.model_validate(self._read_json(path))

    def list_work_beads(
        self,
        state: WorkState | None = None,
        milestone: str | None = None,
    ) -> list[WorkBead]:
        beads = []
        for p in self._work_dir.glob("*.json"):
            try:
                b = WorkBead.model_validate(self._read_json(p))
                if state and b.state != state:
                    continue
                if milestone and b.milestone != milestone:
                    continue
                beads.append(b)
            except Exception:
                pass
        return beads

    # --- PR beads ---

    def _pr_path(self, pr_number: int) -> Path:
        return self._prs_dir / f"pr-{pr_number}.json"

    def write_pr_bead(self, bead: PRBead) -> None:
        bead.touch()
        self._atomic_write(self._pr_path(bead.pr_number), bead.model_dump(mode="json"))

    def read_pr_bead(self, pr_number: int) -> PRBead | None:
        path = self._pr_path(pr_number)
        if not path.exists():
            return None
        return PRBead.model_validate(self._read_json(path))

    def list_pr_beads(self, state: PRState | None = None) -> list[PRBead]:
        beads = []
        for p in self._prs_dir.glob("pr-*.json"):
            try:
                b = PRBead.model_validate(self._read_json(p))
                if state and b.state != state:
                    continue
                beads.append(b)
            except Exception:
                pass
        return beads

    # --- Merge queue ---

    def _mq_path(self) -> Path:
        return self._root / "merge-queue.json"

    def read_merge_queue(self) -> MergeQueue:
        path = self._mq_path()
        if not path.exists():
            repo = str(self._root.parent.name) + "/" + str(self._root.name)
            return MergeQueue(repo=repo)
        return MergeQueue.model_validate(self._read_json(path))

    def write_merge_queue(self, queue: MergeQueue) -> None:
        self._atomic_write(self._mq_path(), queue.model_dump(mode="json"))

    def enqueue_merge(self, item: MergeQueueItem) -> None:
        q = self.read_merge_queue()
        q.enqueue(item)
        self.write_merge_queue(q)

    # --- Campaign bead ---

    def _campaign_path(self) -> Path:
        return self._root / "campaign.json"

    def read_campaign_bead(self) -> CampaignBead | None:
        path = self._campaign_path()
        if not path.exists():
            return None
        return CampaignBead.model_validate(self._read_json(path))

    def write_campaign_bead(self, bead: CampaignBead) -> None:
        bead.touch()
        self._atomic_write(self._campaign_path(), bead.model_dump(mode="json"))

    # --- Internal helpers ---

    def _atomic_write(self, path: Path, data: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, path)

    def _read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))
