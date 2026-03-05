"""Anomaly repair — auto and agent-based repair strategies."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

from breadforge.monitor.anomaly import AnomalyBead, AnomalyKind

if TYPE_CHECKING:
    from breadforge.beads.store import BeadStore
    from breadforge.config import Config
    from breadforge.logger import Logger


def _gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def _get_default_branch(repo: str) -> str:
    r = _gh("repo", "view", repo, "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name")
    return r.stdout.strip() or "mainline"


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


async def _repair_agent(
    abead: AnomalyBead,
    store: BeadStore,
    config: Config,
    logger: Logger,
) -> None:
    """Agent-based repair: dispatch a Claude Code agent to fix the issue."""
    from breadforge.agents.prompts import build_agent_prompt
    from breadforge.agents.runner import run_agent

    if abead.repair_branch is None:
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
                f"Investigate the issue, implement a fix on a new branch, and create a PR."
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
            pr_result = subprocess.run(
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
            abead.repair_branch = None
            abead.repair_attempts += 1
    else:
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

    if checks:
        states = [c.get("state", "") for c in checks]
        conclusions = [c.get("conclusion", "") for c in checks]
        if any(s in ("IN_PROGRESS", "QUEUED") for s in states):
            return
        if any(c == "FAILURE" for c in conclusions):
            abead.repair_branch = None
            abead.repair_pr_number = None
            abead.repair_attempts += 1
            logger.repair(abead.anomaly_id, abead.issue_number, "repair_pr_ci_failed")
            return

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
