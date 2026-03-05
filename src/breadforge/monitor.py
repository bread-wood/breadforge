"""Monitor — anomaly detection and automated repair.

Runs as a background loop scanning for:
  - zombie_pr: PR open but CI has been failing/stuck for too long
  - stuck_issue: issue claimed with no PR after timeout
  - conflict_pr: PR has merge conflicts
  - stale_label: in-progress label with no matching claimed bead

When anomalies are detected, they are persisted as AnomalyBeads and routed
to repair agents based on repair_tier.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from breadforge.beads import BeadStore
from breadforge.config import Config
from breadforge.logger import Logger
from breadforge.runner import build_agent_prompt, run_agent

# ---------------------------------------------------------------------------
# Anomaly types
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# AnomalyStore (extends BeadStore layout)
# ---------------------------------------------------------------------------


class AnomalyStore:
    """Persists anomaly beads under the beads directory."""

    def __init__(self, beads_dir: Path, repo: str) -> None:

        owner, name = repo.split("/", 1)
        self._dir = beads_dir / owner / name / "anomalies"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, anomaly_id: str) -> Path:
        return self._dir / f"{anomaly_id}.json"

    def write(self, bead: AnomalyBead) -> None:
        import os

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


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def _detect_anomalies(
    store: BeadStore,
    repo: str,
    *,
    stuck_minutes: int = 120,
    zombie_minutes: int = 60,
) -> list[AnomalyBead]:

    anomalies: list[AnomalyBead] = []
    now = datetime.now(UTC)

    # --- Stuck issues: claimed with no PR for too long ---
    for bead in store.list_work_beads(state="claimed"):
        elapsed = (now - bead.updated_at).total_seconds() / 60
        if elapsed > stuck_minutes and bead.pr_number is None:
            anomalies.append(
                AnomalyBead(
                    anomaly_id=f"stuck-{bead.issue_number}-{int(now.timestamp())}",
                    repo=repo,
                    kind=AnomalyKind.STUCK_ISSUE,
                    issue_number=bead.issue_number,
                    branch=bead.branch,
                    repair_tier="agent",
                )
            )

    # --- Zombie PRs: PR open with CI failing/stuck for too long ---
    for pr_bead in store.list_pr_beads():
        if pr_bead.state in ("merged", "abandoned"):
            continue
        elapsed = (now - pr_bead.updated_at).total_seconds() / 60
        if elapsed > zombie_minutes:
            ci_result = _gh(
                "pr",
                "checks",
                str(pr_bead.pr_number),
                "--repo",
                repo,
                "--json",
                "name,state,conclusion",
            )
            try:
                checks = json.loads(ci_result.stdout) if ci_result.returncode == 0 else []
                conclusions = [c.get("conclusion", "") for c in checks]
                if any(c == "FAILURE" for c in conclusions):
                    anomalies.append(
                        AnomalyBead(
                            anomaly_id=f"zombie-{pr_bead.pr_number}-{int(now.timestamp())}",
                            repo=repo,
                            kind=AnomalyKind.ZOMBIE_PR,
                            issue_number=pr_bead.issue_number,
                            pr_number=pr_bead.pr_number,
                            branch=pr_bead.branch,
                            repair_tier="agent",
                        )
                    )
            except (json.JSONDecodeError, TypeError):
                pass

    # --- Conflict PRs ---
    for pr_bead in store.list_pr_beads():
        if pr_bead.state in ("merged", "abandoned", "conflict"):
            continue
        result = _gh("pr", "view", str(pr_bead.pr_number), "--repo", repo, "--json", "mergeable")
        try:
            data = json.loads(result.stdout) if result.returncode == 0 else {}
            if data.get("mergeable") == "CONFLICTING":
                anomalies.append(
                    AnomalyBead(
                        anomaly_id=f"conflict-{pr_bead.pr_number}-{int(now.timestamp())}",
                        repo=repo,
                        kind=AnomalyKind.CONFLICT_PR,
                        issue_number=pr_bead.issue_number,
                        pr_number=pr_bead.pr_number,
                        branch=pr_bead.branch,
                        repair_tier="auto",
                    )
                )
        except (json.JSONDecodeError, TypeError):
            pass

    # --- Stale labels: in-progress on GitHub but no claimed bead ---
    result = _gh(
        "issue",
        "list",
        "--repo",
        repo,
        "--label",
        "in-progress",
        "--json",
        "number,title",
        "--limit",
        "50",
    )
    try:
        issues = json.loads(result.stdout) if result.returncode == 0 else []
        claimed_numbers = {b.issue_number for b in store.list_work_beads(state="claimed")}
        for issue in issues:
            n = issue["number"]
            if n not in claimed_numbers:
                anomalies.append(
                    AnomalyBead(
                        anomaly_id=f"stale-{n}-{int(now.timestamp())}",
                        repo=repo,
                        kind=AnomalyKind.STALE_LABEL,
                        issue_number=n,
                        repair_tier="auto",
                    )
                )
    except (json.JSONDecodeError, TypeError):
        pass

    return anomalies


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------


async def _repair_auto(abead: AnomalyBead, repo: str, logger: Logger) -> None:
    """Auto-repair: remove stale labels, rebase conflict branches."""
    if abead.kind == AnomalyKind.STALE_LABEL and abead.issue_number:
        _gh(
            "issue",
            "edit",
            str(abead.issue_number),
            "--repo",
            repo,
            "--remove-assignee",
            "@me",
            "--remove-label",
            "in-progress",
        )
        logger.repair(abead.anomaly_id, abead.issue_number, "removed_stale_label")
        abead.resolved = True

    elif abead.kind == AnomalyKind.CONFLICT_PR and abead.branch:
        default_branch = _get_default_branch(repo)
        # Attempt rebase via subprocess
        subprocess.run(["git", "fetch", "origin"], capture_output=True, text=True)
        rebase = subprocess.run(
            ["git", "rebase", f"origin/{default_branch}"], capture_output=True, text=True
        )
        if rebase.returncode == 0:
            push = subprocess.run(
                ["git", "push", "--force-with-lease", "origin", abead.branch],
                capture_output=True,
                text=True,
            )
            if push.returncode == 0:
                logger.repair(abead.anomaly_id, abead.issue_number, "rebased_conflict")
                abead.resolved = True
            else:
                subprocess.run(["git", "rebase", "--abort"], capture_output=True)
        else:
            subprocess.run(["git", "rebase", "--abort"], capture_output=True)


def _get_default_branch(repo: str) -> str:
    r = _gh("repo", "view", repo, "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name")
    return r.stdout.strip() or "mainline"


async def _repair_agent(
    abead: AnomalyBead,
    store: BeadStore,
    config: Config,
    logger: Logger,
) -> None:
    """Agent-based repair: dispatch a Claude Code agent to fix the issue."""
    if abead.repair_branch is None:
        # First dispatch
        branch = f"repair-{abead.anomaly_id[:20]}"
        abead.repair_branch = branch
        logger.repair(abead.anomaly_id, abead.issue_number, "dispatch_repair_agent")

        body = f"This is a repair task for anomaly `{abead.anomaly_id}` (kind: {abead.kind}).\n\n"
        if abead.kind == AnomalyKind.ZOMBIE_PR and abead.pr_number:
            body += (
                f"PR #{abead.pr_number} has been failing CI. "
                f"Investigate the CI failures on branch `{abead.branch}`, "
                f"fix the root cause, push the fix, and ensure CI passes.\n"
                f"Do NOT merge — stop after CI is green."
            )
        elif abead.kind == AnomalyKind.STUCK_ISSUE and abead.issue_number:
            body += (
                f"Issue #{abead.issue_number} was claimed but no PR was created. "
                f"Investigate the issue, implement a fix on a new branch, "
                f"and create a PR."
            )

        prompt = build_agent_prompt(
            issue_number=abead.issue_number or 0,
            issue_title=f"Repair: {abead.kind}",
            issue_body=body,
            branch=branch,
            repo=config.repo,
        )

        result = await run_agent(
            prompt,
            model=config.model,
            timeout_minutes=config.agent_timeout_minutes,
        )

        if result.success:
            # Find the PR the repair agent created
            import subprocess as sp

            pr_result = sp.run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    config.repo,
                    "--head",
                    branch,
                    "--json",
                    "number",
                    "--limit",
                    "1",
                ],
                capture_output=True,
                text=True,
            )
            try:
                items = json.loads(pr_result.stdout)
                if items:
                    abead.repair_pr_number = items[0]["number"]
                    logger.repair(
                        abead.anomaly_id,
                        abead.issue_number,
                        "repair_pr_created",
                        pr_number=abead.repair_pr_number,
                    )
            except (json.JSONDecodeError, KeyError, IndexError):
                pass
        else:
            # Agent failed — clear branch so next scan retries
            abead.repair_branch = None
            abead.repair_attempts += 1
    else:
        # Resume: poll CI on existing repair PR
        if abead.repair_pr_number:
            await _poll_repair_pr(abead, config.repo, logger)


async def _poll_repair_pr(abead: AnomalyBead, repo: str, logger: Logger) -> None:
    """Poll CI on a repair PR and merge if passing."""
    assert abead.repair_pr_number

    ci_result = _gh(
        "pr",
        "checks",
        str(abead.repair_pr_number),
        "--repo",
        repo,
        "--json",
        "name,state,conclusion",
    )
    try:
        checks = json.loads(ci_result.stdout) if ci_result.returncode == 0 else []
    except json.JSONDecodeError:
        return

    if not checks:
        # No CI — merge directly
        pass
    else:
        states = [c.get("state", "") for c in checks]
        conclusions = [c.get("conclusion", "") for c in checks]
        if any(s in ("IN_PROGRESS", "QUEUED") for s in states):
            return  # still running
        if any(c == "FAILURE" for c in conclusions):
            abead.repair_branch = None
            abead.repair_pr_number = None
            abead.repair_attempts += 1
            logger.repair(abead.anomaly_id, abead.issue_number, "repair_pr_ci_failed")
            return

    # CI passing — squash merge
    merge_result = _gh(
        "pr", "merge", str(abead.repair_pr_number), "--repo", repo, "--squash", "--delete-branch"
    )
    if merge_result.returncode == 0:
        abead.resolved = True
        logger.repair(
            abead.anomaly_id,
            abead.issue_number,
            "repair_merged",
            pr_number=abead.repair_pr_number,
        )


# ---------------------------------------------------------------------------
# Monitor loop
# ---------------------------------------------------------------------------


async def run_monitor(
    store: BeadStore,
    config: Config,
    logger: Logger,
    *,
    once: bool = False,
    interval_seconds: int = 300,
    dry_run: bool = False,
    max_repair_attempts: int = 3,
) -> None:
    """Main monitor loop. Detects anomalies and dispatches repairs."""
    astore = AnomalyStore(config.beads_dir, config.repo)

    while True:
        logger.info("monitor: scanning for anomalies")

        # Detect new anomalies
        new_anomalies = _detect_anomalies(store, config.repo)

        # Deduplicate: skip same kind+issue if already open
        existing_by_kind_issue = {(a.kind, a.issue_number): a for a in astore.list_open()}

        for anomaly in new_anomalies:
            key = (anomaly.kind, anomaly.issue_number)
            if key in existing_by_kind_issue:
                continue  # already tracking this anomaly
            logger.anomaly(anomaly.anomaly_id, anomaly.kind, anomaly.issue_number)
            astore.write(anomaly)

        # Process open anomalies
        for abead in astore.list_open():
            if abead.repair_attempts >= max_repair_attempts:
                logger.error(
                    f"anomaly {abead.anomaly_id} exceeded max repair attempts — needs human review",
                    anomaly_id=abead.anomaly_id,
                )
                continue

            if dry_run:
                logger.info(f"[dry-run] would repair {abead.kind} anomaly {abead.anomaly_id}")
                continue

            if abead.repair_tier == "auto":
                await _repair_auto(abead, config.repo, logger)
            elif abead.repair_tier == "agent":
                await _repair_agent(abead, store, config, logger)

            astore.write(abead)

        if once:
            break

        await asyncio.sleep(interval_seconds)
