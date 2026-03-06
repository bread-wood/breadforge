"""Anomaly detection — scans beads and GitHub for aberrations."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

from breadforge.beads.store import BeadStore
from breadforge.monitor.anomaly import AnomalyBead, AnomalyKind


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

    # --- Zombie PRs: PR open with CI failing for too long ---
    for pr_bead in store.list_pr_beads():
        if pr_bead.state in ("merged", "abandoned"):
            continue
        elapsed = (now - pr_bead.updated_at).total_seconds() / 60
        if elapsed > zombie_minutes:
            ci_result = _gh(
                "pr",
                "view",
                str(pr_bead.pr_number),
                "--repo",
                repo,
                "--json",
                "statusCheckRollup",
            )
            try:
                data = json.loads(ci_result.stdout) if ci_result.stdout.strip() else {}
                checks = data.get("statusCheckRollup") or []
                ci_failing = False
                for c in checks:
                    # CheckRun: status=COMPLETED + conclusion=FAILURE/TIMED_OUT
                    if c.get("__typename") == "CheckRun":
                        if c.get("status") == "COMPLETED" and c.get("conclusion") in (
                            "FAILURE",
                            "TIMED_OUT",
                            "STARTUP_FAILURE",
                        ):
                            ci_failing = True
                            break
                    # StatusContext: state=FAILURE/ERROR
                    elif c.get("state") in ("FAILURE", "ERROR"):
                        ci_failing = True
                        break
                if ci_failing:
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
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

    # --- Stale labels ---
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
