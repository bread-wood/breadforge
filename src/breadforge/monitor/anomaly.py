"""Anomaly types and AnomalyStore."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class AnomalyKind(StrEnum):
    ZOMBIE_PR = "zombie_pr"
    STUCK_ISSUE = "stuck_issue"
    CONFLICT_PR = "conflict_pr"
    STALE_LABEL = "stale_label"


RepairTier = Literal["auto", "agent", "human"]


class AnomalyBead(BaseModel):
    anomaly_id: str
    repo: str
    kind: AnomalyKind
    issue_number: int | None = None
    pr_number: int | None = None
    branch: str | None = None
    repair_tier: RepairTier = "agent"
    repair_attempts: int = 0
    repair_branch: str | None = None
    repair_pr_number: int | None = None
    resolved: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC)


class AnomalyStore:
    """Persists anomaly beads under the beads directory."""

    def __init__(self, beads_dir: Path, repo: str) -> None:
        owner, name = repo.split("/", 1)
        self._dir = beads_dir / owner / name / "anomalies"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, anomaly_id: str) -> Path:
        return self._dir / f"{anomaly_id}.json"

    def write(self, bead: AnomalyBead) -> None:
        bead.touch()
        data = bead.model_dump(mode="json")
        tmp = self._path(bead.anomaly_id).with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        os.replace(tmp, self._path(bead.anomaly_id))

    def read(self, anomaly_id: str) -> AnomalyBead | None:
        p = self._path(anomaly_id)
        if not p.exists():
            return None
        return AnomalyBead.model_validate(json.loads(p.read_text()))

    def list_open(self) -> list[AnomalyBead]:
        beads = []
        for p in self._dir.glob("*.json"):
            try:
                b = AnomalyBead.model_validate(json.loads(p.read_text()))
                if not b.resolved:
                    beads.append(b)
            except Exception:
                pass
        return beads
