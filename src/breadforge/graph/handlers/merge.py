"""MergeHandler — polls CI on a PR, dispatches repair agents on failure, squash-merges when passing.

Ported from merge.py process_merge_queue, adapted to the NodeHandler protocol.
The node context must supply 'pr_number' and optionally 'branch'.

CI polling: when CI is still running, the handler sleeps CI_POLL_INTERVAL_SECONDS
before returning a retriable failure.  The merge node has a higher max_retries
(set in _emit_merge_nodes) to accommodate CI wait time.

Repair loop: when CI fails, the handler dispatches a repair agent (up to
MAX_REPAIR_ATTEMPTS times) that reads the failure logs, fixes the code, and
pushes. The merge node then retries, picking up the new CI run.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from breadforge.beads.types import GraphNode
from breadforge.graph.node import NodeResult

CI_POLL_INTERVAL_SECONDS = 60
MAX_REPAIR_ATTEMPTS = 2

if TYPE_CHECKING:
    from breadforge.beads.store import BeadStore
    from breadforge.config import Config
    from breadforge.logger import Logger


def _gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def _pr_ci_passing(pr_number: int, repo: str) -> bool | None:
    """Returns True if all CI checks passed, False if any failed, None if still running.

    Uses statusCheckRollup from gh pr view, which is reliable regardless of exit code.
    CheckRun fields: status (QUEUED/IN_PROGRESS/COMPLETED), conclusion (SUCCESS/FAILURE/...).
    StatusContext fields: state (PENDING/SUCCESS/FAILURE/ERROR/EXPECTED).
    """
    result = _gh(
        "pr",
        "view",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "statusCheckRollup",
    )
    if result.returncode != 0:
        return None  # can't determine — treat as still running
    try:
        data = json.loads(result.stdout)
        checks = data.get("statusCheckRollup") or []
    except json.JSONDecodeError:
        return None

    if not checks:
        return True  # no CI configured

    _FAILING_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "CANCELLED", "ERROR"}
    _PASSING_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}

    for check in checks:
        typename = check.get("__typename", "")
        if typename == "CheckRun":
            status = check.get("status", "").upper()
            conclusion = check.get("conclusion", "").upper()
            if status != "COMPLETED":
                return None  # still running
            if conclusion in _FAILING_CONCLUSIONS:
                return False
        elif typename == "StatusContext":
            state = check.get("state", "").upper()
            if state == "PENDING":
                return None
            if state in ("FAILURE", "ERROR"):
                return False
        else:
            # Unknown type — check for common fields
            state = check.get("state", "").upper()
            conclusion = check.get("conclusion", "").upper()
            if state == "PENDING" or (not state and not conclusion):
                return None
            if state in ("FAILURE", "ERROR", "FAIL") or conclusion in _FAILING_CONCLUSIONS:
                return False

    return True


def _get_ci_failure_logs(branch: str, repo: str) -> str:
    """Fetch failure logs from the most recent CI run on the branch."""
    r = _gh(
        "run",
        "list",
        "--repo",
        repo,
        "--branch",
        branch,
        "--json",
        "databaseId,status,conclusion",
        "--limit",
        "1",
    )
    try:
        runs = json.loads(r.stdout)
        run_id = runs[0]["databaseId"] if runs else None
    except (json.JSONDecodeError, KeyError, IndexError):
        run_id = None

    if not run_id:
        return ""

    log = subprocess.run(
        ["gh", "run", "view", str(run_id), "--repo", repo, "--log-failed"],
        capture_output=True,
        text=True,
    )
    return log.stdout[:4000]


class MergeHandler:
    """Polls CI on a PR, dispatches repair agents on failure, squash-merges when passing."""

    def __init__(
        self,
        store: BeadStore | None = None,
        logger: Logger | None = None,
    ) -> None:
        self._store = store
        self._logger = logger

    async def _dispatch_repair(
        self, pr_number: int, branch: str, repo: str, config: Config
    ) -> None:
        """Run a repair agent to fix CI failures on the PR branch."""
        from breadforge.agents.runner import run_agent

        failure_logs = _get_ci_failure_logs(branch, repo)

        prompt = f"""You are fixing CI failures on PR #{pr_number} in repo {repo} on branch `{branch}`.

CI failure logs:
{failure_logs or "(could not retrieve logs — check `gh run list --branch {branch}`)"}

Steps:
1. `git clone https://github.com/{repo}.git . && git checkout {branch}`
2. Read the failure output carefully — identify root cause (test failure, lint error, import error, etc.)
3. Fix the minimal set of changes needed to make CI pass
4. Run tests locally if possible: `uv run pytest` or equivalent
5. Run lint: `uv run ruff check`
6. Commit: `git commit -am "fix: resolve CI failures on {branch}"`
7. `git push origin {branch}`
8. STOP — do not merge, do not open a new PR

Fix only what CI is complaining about. Do not refactor or expand scope."""

        workspace = Path(tempfile.mkdtemp(prefix=f"breadforge-repair-{pr_number}-"))
        result = await run_agent(
            prompt,
            model=config.model,
            timeout_minutes=20,
            cwd=workspace,
            allowed_tools=["Bash", "Edit", "Write", "Read", "Glob", "Grep", "MultiEdit"],
        )
        if self._logger:
            self._logger.info(
                f"repair agent for PR #{pr_number} exited {result.exit_code}",
                node_id=f"repair-{pr_number}",
            )

    async def _rebase_branch(self, branch: str, repo: str) -> bool:
        """Clone the repo, rebase the branch onto main, and force-push.

        Returns True if the rebase succeeded, False if there were conflicts.
        Runs in a thread to avoid blocking the event loop.
        """
        import asyncio
        import tempfile

        def _do_rebase() -> bool:
            workdir = Path(tempfile.mkdtemp(prefix=f"breadforge-rebase-{branch[:20]}-"))
            run = lambda *cmd: subprocess.run(  # noqa: E731
                list(cmd), cwd=workdir, capture_output=True, text=True
            )
            if run("gh", "repo", "clone", repo, ".").returncode != 0:
                return False
            if run("git", "checkout", branch).returncode != 0:
                return False
            if run("git", "fetch", "origin", "main").returncode != 0:
                return False
            rebase = run("git", "rebase", "origin/main")
            if rebase.returncode != 0:
                run("git", "rebase", "--abort")
                return False
            push = run("git", "push", "--force-with-lease", "origin", branch)
            return push.returncode == 0

        return await asyncio.get_event_loop().run_in_executor(None, _do_rebase)

    async def execute(self, node: GraphNode, config: Config) -> NodeResult:
        pr_number: int | None = node.context.get("pr_number")
        module_issue_number: int | None = node.context.get("issue_number")
        milestone_issue_number: int | None = node.context.get("milestone_issue_number")

        if not pr_number:
            # Try to find the PR from the corresponding build node's stored output
            build_node_id: str | None = node.context.get("build_node_id")
            if build_node_id and self._store:
                build_node = self._store.read_node(build_node_id)
                if build_node and build_node.output:
                    pr_number = build_node.output.get("pr_number")
                if build_node and build_node.context:
                    if not module_issue_number:
                        module_issue_number = build_node.context.get("issue_number")
                    if not milestone_issue_number:
                        milestone_issue_number = build_node.context.get("milestone_issue_number")

        if not pr_number:
            # If the build node is abandoned, abandon this merge node too — no point retrying
            build_node_id = node.context.get("build_node_id")
            if build_node_id and self._store:
                build_node = self._store.read_node(build_node_id)
                if build_node and build_node.state == "abandoned":
                    return NodeResult(
                        success=False,
                        abandon=True,
                        error=f"merge node: build node {build_node_id} was abandoned",
                    )
            return NodeResult(success=False, error="merge node: no pr_number in context")

        repo = config.repo
        branch: str = node.context.get("branch", "")
        if not branch:
            # Resolve branch from build node context
            build_node_id = node.context.get("build_node_id")
            if build_node_id and self._store:
                build_node = self._store.read_node(build_node_id)
                if build_node and build_node.context:
                    branch = build_node.context.get("branch", "")

        # Conflict check — rebase before CI polling if the PR has conflicts
        if branch:
            mergeable = _gh("pr", "view", str(pr_number), "--repo", repo, "--json", "mergeable")
            try:
                is_conflicting = json.loads(mergeable.stdout).get("mergeable") == "CONFLICTING"
            except (json.JSONDecodeError, AttributeError):
                is_conflicting = False

            if is_conflicting:
                rebase_ok = await self._rebase_branch(branch, repo)
                if not rebase_ok:
                    return NodeResult(
                        success=False,
                        error=f"PR #{pr_number} has merge conflicts that could not be auto-resolved on branch {branch!r}",
                    )
                if self._logger:
                    self._logger.info(
                        f"PR #{pr_number}: rebased {branch!r} onto main, retrying CI poll",
                        node_id=node.id,
                    )
                # Let CI pick up the new push before polling
                await asyncio.sleep(CI_POLL_INTERVAL_SECONDS)
                return NodeResult(success=False, error=f"rebased PR #{pr_number}; waiting for CI")

        ci_status = _pr_ci_passing(pr_number, repo)
        if ci_status is None:
            # CI still running — wait before signaling retry
            await asyncio.sleep(CI_POLL_INTERVAL_SECONDS)
            return NodeResult(success=False, error="CI still running")

        if ci_status is False:
            if self._store:
                pr_bead = self._store.read_pr_bead(pr_number)
                if pr_bead:
                    pr_bead.state = "ci_failing"  # type: ignore[assignment]
                    self._store.write_pr_bead(pr_bead)

            repair_count: int = node.context.get("repair_count", 0)
            if repair_count < MAX_REPAIR_ATTEMPTS and branch:
                node.context["repair_count"] = repair_count + 1
                if self._store:
                    self._store.write_node(node)
                if self._logger:
                    self._logger.info(
                        f"PR #{pr_number} CI failing — dispatching repair agent "
                        f"(attempt {repair_count + 1}/{MAX_REPAIR_ATTEMPTS})",
                        node_id=node.id,
                    )
                await self._dispatch_repair(pr_number, branch, repo, config)
                # After repair pushes, CI will re-run — sleep to let it start
                await asyncio.sleep(CI_POLL_INTERVAL_SECONDS)
                return NodeResult(
                    success=False,
                    error=f"CI was failing on PR #{pr_number}; repair dispatched, retrying",
                )

            return NodeResult(
                success=False,
                error=f"CI failing on PR #{pr_number} after {repair_count} repair attempts",
            )

        # CI passing — squash merge
        result = _gh("pr", "merge", str(pr_number), "--repo", repo, "--squash", "--delete-branch")

        if result.returncode != 0:
            return NodeResult(
                success=False,
                error=f"merge failed: {result.stderr[:200]}",
            )

        # Update bead states
        if self._store:
            pr_bead = self._store.read_pr_bead(pr_number)
            if pr_bead:
                pr_bead.state = "merged"  # type: ignore[assignment]
                self._store.write_pr_bead(pr_bead)

                work_bead = self._store.read_work_bead(pr_bead.issue_number)
                if work_bead:
                    work_bead.state = "closed"  # type: ignore[assignment]
                    self._store.write_work_bead(work_bead)

        if self._logger:
            branch = node.context.get("branch", "")
            issue_number = node.context.get("issue_number", 0)
            self._logger.merge(pr_number, issue_number, branch)

        # Comment + close the module issue if present
        if module_issue_number:
            _gh(
                "issue",
                "comment",
                str(module_issue_number),
                "--repo",
                repo,
                "--body",
                f"PR #{pr_number} merged.",
            )
            _gh("issue", "close", str(module_issue_number), "--repo", repo)
        # Comment progress on the milestone issue
        if milestone_issue_number:
            _gh(
                "issue",
                "comment",
                str(milestone_issue_number),
                "--repo",
                repo,
                "--body",
                f"PR #{pr_number} merged to main.",
            )

        return NodeResult(
            success=True,
            output={"pr_number": pr_number, "merged": True},
        )
