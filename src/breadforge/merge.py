"""Merge queue processing — sequential squash merges.

Drains the MergeQueue one item at a time:
  rebase → CI check → squash merge → update beads → next item
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from breadforge.beads import BeadStore, PRState
from breadforge.config import Config
from breadforge.logger import Logger


def _gh(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def _get_default_branch(repo: str) -> str:
    result = _gh(
        "repo", "view", repo, "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"
    )
    branch = result.stdout.strip()
    return branch if branch else "mainline"


def _pr_ci_passing(pr_number: int, repo: str) -> bool | None:
    """Returns True if CI passing, False if failing, None if still running."""
    import json

    result = _gh("pr", "checks", str(pr_number), "--repo", repo, "--json", "name,state,conclusion")
    if result.returncode != 0:
        return None
    try:
        checks = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not checks:
        return True  # no checks configured
    states = [c.get("state", "") for c in checks]
    conclusions = [c.get("conclusion", "") for c in checks]
    if any(s in ("IN_PROGRESS", "QUEUED", "REQUESTED") for s in states):
        return None
    return all(c in ("SUCCESS", "SKIPPED", "") for c in conclusions)


def _rebase_branch(branch: str, default_branch: str, repo_root: Path) -> bool:
    _git("fetch", "origin", cwd=repo_root)
    result = _git("rebase", f"origin/{default_branch}", cwd=repo_root)
    if result.returncode != 0:
        _git("rebase", "--abort", cwd=repo_root)
        return False
    push = _git("push", "--force-with-lease", "origin", branch, cwd=repo_root)
    return push.returncode == 0


def process_merge_queue(
    store: BeadStore,
    config: Config,
    repo_root: Path | None = None,
    logger: Logger | None = None,
) -> int:
    """Drain the merge queue. Returns number of PRs merged."""
    merged = 0

    while True:
        queue = store.read_merge_queue()
        item = queue.peek()
        if item is None:
            break

        pr_bead = store.read_pr_bead(item.pr_number)
        if pr_bead is None:
            # Stale queue item — dequeue and skip
            queue.dequeue()
            store.write_merge_queue(queue)
            continue

        ci_status = _pr_ci_passing(item.pr_number, config.repo)

        if ci_status is None:
            # Still running — check back later
            break

        if ci_status is False:
            # CI failing — mark bead, dequeue, let watchdog handle
            pr_bead.state = PRState.__args__[0]  # type: ignore
            pr_bead.state = "ci_failing"  # type: ignore
            store.write_pr_bead(pr_bead)
            queue.dequeue()
            store.write_merge_queue(queue)
            if logger:
                logger.error(
                    f"PR #{item.pr_number} CI failing — skipping merge",
                    pr_number=item.pr_number,
                )
            break

        # CI passing — merge
        result = _gh(
            "pr",
            "merge",
            str(item.pr_number),
            "--repo",
            config.repo,
            "--squash",
            "--delete-branch",
        )

        if result.returncode == 0:
            # Update bead states
            pr_bead.state = "merged"  # type: ignore
            store.write_pr_bead(pr_bead)

            work_bead = store.read_work_bead(item.issue_number)
            if work_bead:
                work_bead.state = "closed"  # type: ignore
                store.write_work_bead(work_bead)

            queue.dequeue()
            store.write_merge_queue(queue)

            if logger:
                logger.merge(item.pr_number, item.issue_number, item.branch)

            merged += 1
        else:
            if logger:
                logger.error(
                    f"merge failed for PR #{item.pr_number}: {result.stderr}",
                    pr_number=item.pr_number,
                )
            break

    return merged
