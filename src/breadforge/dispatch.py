"""Rolling dispatch loop with watchdog.

The dispatcher fills concurrency slots as agents complete, monitors for hung
agents, isolates per-issue failures, and emits heartbeat logs.

Key invariants:
- One agent per issue at a time
- Agents run in isolated worktrees (via claude --isolation worktree, or manual worktree)
- SIGTERM → SIGKILL on timeout
- Max retries per issue before abandoning
- Issue slot freed within one watchdog cycle of any completion or kill
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from breadforge.assessor import assess_and_allocate
from breadforge.beads import (
    BeadStore,
    MergeQueueItem,
    PRBead,
)
from breadforge.config import Config
from breadforge.logger import Logger
from breadforge.runner import RunResult, build_agent_prompt, run_agent

# ---------------------------------------------------------------------------
# Agent task tracking
# ---------------------------------------------------------------------------


@dataclass
class AgentTask:
    issue_number: int
    branch: str
    model: str
    task: asyncio.Task
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def elapsed_seconds(self) -> float:
        return (datetime.now(UTC) - self.started_at).total_seconds()


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------


def _gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd)


def _get_default_branch(repo: str) -> str:
    r = _gh("repo", "view", repo, "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name")
    return r.stdout.strip() or "mainline"


def _get_issue(repo: str, issue_number: int) -> dict[str, Any]:
    r = _gh("issue", "view", str(issue_number), "--repo", repo, "--json", "title,body,labels")
    if r.returncode != 0:
        return {"title": f"Issue #{issue_number}", "body": "", "labels": []}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"title": f"Issue #{issue_number}", "body": "", "labels": []}


def _get_pr_number(repo: str, branch: str) -> int | None:
    r = _gh("pr", "list", "--repo", repo, "--head", branch, "--json", "number", "--limit", "1")
    try:
        items = json.loads(r.stdout)
        return items[0]["number"] if items else None
    except (json.JSONDecodeError, KeyError, IndexError):
        return None


def _create_branch(repo: str, branch: str, default_branch: str) -> bool:
    # Branch creation happens in the agent's worktree
    return True


def _claim_issue(repo: str, issue_number: int) -> None:
    _gh(
        "issue",
        "edit",
        str(issue_number),
        "--repo",
        repo,
        "--add-assignee",
        "@me",
        "--add-label",
        "in-progress",
    )


def _unclaim_issue(repo: str, issue_number: int) -> None:
    _gh(
        "issue",
        "edit",
        str(issue_number),
        "--repo",
        repo,
        "--remove-assignee",
        "@me",
        "--remove-label",
        "in-progress",
    )


def _post_comment(repo: str, issue_number: int, body: str) -> None:
    _gh("issue", "comment", str(issue_number), "--repo", repo, "--body", body)


# ---------------------------------------------------------------------------
# RollingDispatcher
# ---------------------------------------------------------------------------


class RollingDispatcher:
    """Rolling dispatch loop — fills slots, runs watchdog, drains merge queue."""

    def __init__(
        self,
        config: Config,
        store: BeadStore,
        logger: Logger,
        repo_root: Path | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._logger = logger
        self._repo_root = repo_root
        self._slots: dict[int, AgentTask] = {}  # issue_number → task
        self._completed = 0
        self._default_branch = _get_default_branch(config.repo)

    # --- Public interface ---

    async def run(self, issue_numbers: list[int]) -> None:
        """Dispatch all issues with rolling concurrency."""
        queue = list(issue_numbers)

        while queue or self._slots:
            # Fill empty slots
            while queue and len(self._slots) < self._config.concurrency:
                issue_number = queue.pop(0)
                await self._start_agent(issue_number)

            if not self._slots:
                break

            # Wait for any task to complete (with a short poll interval)
            done, _ = await asyncio.wait(
                [t.task for t in self._slots.values()],
                timeout=float(self._config.watchdog_interval_seconds),
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Process completed tasks
            for task in done:
                issue_number = self._find_issue_for_task(task)
                if issue_number is not None:
                    agent_task = self._slots.pop(issue_number)
                    try:
                        result: RunResult = task.result()
                    except Exception as e:
                        result = RunResult(
                            exit_code=1,
                            stdout="",
                            stderr=str(e),
                            duration_ms=0,
                        )
                    await self._handle_completion(agent_task, result)

            # Watchdog: kill hung agents
            await self._watchdog()

    # --- Internal ---

    async def _start_agent(self, issue_number: int) -> None:
        bead = self._store.read_work_bead(issue_number)
        if bead is None:
            return

        # Check cross-repo blockers
        if bead.blocked_by:
            unresolved = await self._check_blockers(bead.blocked_by)
            if unresolved:
                self._logger.info(
                    f"issue #{issue_number} blocked: {unresolved}",
                    issue_number=issue_number,
                )
                return

        import re

        slug = re.sub(r"[^a-z0-9-]", "-", bead.title[:40].lower()).strip("-")
        slug = re.sub(r"-{2,}", "-", slug)
        branch = f"{issue_number}-{slug}"
        bead.branch = branch
        bead.state = "claimed"  # type: ignore
        self._store.write_work_bead(bead)

        _claim_issue(self._config.repo, issue_number)

        # Assess complexity and select model
        issue_data = _get_issue(self._config.repo, issue_number)
        allocation, estimate = await assess_and_allocate(
            issue_data.get("title", ""),
            issue_data.get("body", ""),
            override_model=self._config.model if self._config.model else None,
        )

        self._logger.dispatch(
            issue_number,
            branch,
            allocation.model,
            tier=allocation.tier,
            upgraded=allocation.upgraded,
        )

        # Build the agent prompt
        prompt = build_agent_prompt(
            issue_number=issue_number,
            issue_title=issue_data.get("title", ""),
            issue_body=issue_data.get("body", ""),
            branch=branch,
            repo=self._config.repo,
        )

        # Create isolated workspace for this agent
        import tempfile

        workspace = Path(tempfile.mkdtemp(prefix=f"breadforge-{issue_number}-"))

        # Launch async task
        task = asyncio.create_task(
            run_agent(
                prompt,
                model=allocation.model,
                timeout_minutes=self._config.agent_timeout_minutes,
                cwd=workspace,
            )
        )

        self._slots[issue_number] = AgentTask(
            issue_number=issue_number,
            branch=branch,
            model=allocation.model,
            task=task,
        )

    async def _handle_completion(self, agent_task: AgentTask, result: RunResult) -> None:
        issue_number = agent_task.issue_number
        branch = agent_task.branch

        self._logger.agent_complete(
            issue_number,
            branch,
            result.exit_code,
            result.duration_ms,
        )

        if result.exit_code != 0 or result.stderr:
            self._logger.error(
                f"agent #{issue_number} stderr: {(result.stderr or '')[:500]}",
                issue_number=issue_number,
                exit_code=result.exit_code,
            )

        bead = self._store.read_work_bead(issue_number)
        if bead is None:
            return

        # Check if agent created a PR
        pr_number = _get_pr_number(self._config.repo, branch)

        if pr_number:
            bead.pr_number = pr_number
            bead.state = "pr_open"  # type: ignore
            self._store.write_work_bead(bead)

            pr_bead = PRBead(
                pr_number=pr_number,
                repo=self._config.repo,
                issue_number=issue_number,
                branch=branch,
            )
            self._store.write_pr_bead(pr_bead)

            # Enqueue for merge when CI passes
            self._store.enqueue_merge(
                MergeQueueItem(
                    pr_number=pr_number,
                    issue_number=issue_number,
                    branch=branch,
                )
            )
            self._completed += 1
        else:
            # No PR — check retry count
            bead.retry_count += 1
            if bead.retry_count >= self._config.max_retries:
                bead.state = "abandoned"  # type: ignore
                self._store.write_work_bead(bead)
                _unclaim_issue(self._config.repo, issue_number)
                _post_comment(
                    self._config.repo,
                    issue_number,
                    f"breadforge: agent failed after {bead.retry_count} attempts without creating a PR. "
                    "Marking abandoned. Please investigate manually.",
                )
                self._logger.error(
                    f"issue #{issue_number} abandoned after {bead.retry_count} retries",
                    issue_number=issue_number,
                )
            else:
                # Re-queue
                bead.state = "open"  # type: ignore
                bead.branch = None
                self._store.write_work_bead(bead)
                _unclaim_issue(self._config.repo, issue_number)
                self._logger.info(
                    f"issue #{issue_number} re-queued (attempt {bead.retry_count})",
                    issue_number=issue_number,
                )

    async def _watchdog(self) -> None:
        """Kill agents that exceed timeout."""
        timeout_seconds = self._config.agent_timeout_minutes * 60

        for issue_number, agent_task in list(self._slots.items()):
            if agent_task.elapsed_seconds > timeout_seconds:
                self._logger.watchdog_kill(
                    issue_number,
                    agent_task.branch,
                    f"exceeded {self._config.agent_timeout_minutes}m timeout",
                )
                agent_task.task.cancel()
                import contextlib

                with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(agent_task.task, timeout=5.0)

                self._slots.pop(issue_number)

                # Re-queue with incremented retry
                bead = self._store.read_work_bead(issue_number)
                if bead:
                    bead.retry_count += 1
                    if bead.retry_count >= self._config.max_retries:
                        bead.state = "abandoned"  # type: ignore
                        self._store.write_work_bead(bead)
                        _unclaim_issue(self._config.repo, issue_number)
                    else:
                        bead.state = "open"  # type: ignore
                        bead.branch = None
                        self._store.write_work_bead(bead)
                        _unclaim_issue(self._config.repo, issue_number)

    async def _check_blockers(self, blocked_by: list[str]) -> list[str]:
        """Return unresolved blockers. A blocker resolves when the milestone has a GH release."""
        unresolved = []
        for dep in blocked_by:
            # Format: "owner/repo:milestone"
            if ":" not in dep:
                continue
            dep_repo, dep_milestone = dep.split(":", 1)
            resolved = await _check_milestone_released(dep_repo, dep_milestone)
            if not resolved:
                unresolved.append(dep)
        return unresolved

    def _find_issue_for_task(self, task: asyncio.Task) -> int | None:
        for issue_number, agent_task in self._slots.items():
            if agent_task.task is task:
                return issue_number
        return None

    @property
    def active_count(self) -> int:
        return len(self._slots)

    @property
    def completed_count(self) -> int:
        return self._completed


async def _check_milestone_released(repo: str, milestone: str) -> bool:
    """Check if a milestone has a published GitHub release."""
    result = _gh(
        "release",
        "view",
        milestone,
        "--repo",
        repo,
        "--json",
        "tagName",
    )
    return result.returncode == 0
