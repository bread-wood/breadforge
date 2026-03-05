"""MergeHandler — polls CI on a PR, dispatches agents for failures, squash-merges when passing.

Ported from merge.py process_merge_queue, adapted to the NodeHandler protocol.
The node context must supply 'pr_number' and optionally 'branch'.

Agent dispatch sequence (each gated by a per-node attempt counter):
  1. Conflict resolution — if the PR is CONFLICTING, dispatch an agent to rebase and resolve.
  2. Review fix — if a reviewer requested changes, dispatch an agent to address them.
  3. CI repair — if CI is failing, dispatch an agent to read logs and fix the code.
  4. Merge — squash-merge once CI passes and no blockers remain.

CI polling: when CI is still running, the handler sleeps CI_POLL_INTERVAL_SECONDS
before returning a retriable failure.  The merge node has a higher max_retries
(set in _emit_merge_nodes) to accommodate CI wait time.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from breadforge.beads.types import GraphNode
from breadforge.config import Config
from breadforge.graph.node import NodeResult

CI_POLL_INTERVAL_SECONDS = 60
MAX_CONFLICT_ATTEMPTS = 1
MAX_REVIEW_ATTEMPTS = 2
MAX_REPAIR_ATTEMPTS = 2

if TYPE_CHECKING:
    from breadforge.beads.store import BeadStore
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


def _get_pending_review_comments(pr_number: int, repo: str) -> str:
    """Return a formatted string of unresolved CHANGES_REQUESTED reviews and inline comments.

    Returns empty string if there are no pending review requests.
    """
    r = _gh(
        "pr",
        "view",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "reviews,comments",
    )
    try:
        data = json.loads(r.stdout)
    except (json.JSONDecodeError, AttributeError):
        return ""

    parts: list[str] = []

    # Top-level reviews with CHANGES_REQUESTED
    for review in data.get("reviews") or []:
        if review.get("state", "").upper() == "CHANGES_REQUESTED":
            author = review.get("author", {}).get("login", "reviewer")
            body = review.get("body", "").strip()
            if body:
                parts.append(f"[Review from @{author}]\n{body}")

    # Inline PR comments (position-based — not thread state, so include all)
    r2 = _gh("api", f"repos/{repo}/pulls/{pr_number}/comments")
    try:
        inline = json.loads(r2.stdout)
        for comment in inline:
            author = comment.get("user", {}).get("login", "reviewer")
            path = comment.get("path", "")
            body = comment.get("body", "").strip()
            if body:
                parts.append(f"[Inline comment on {path} from @{author}]\n{body}")
    except (json.JSONDecodeError, TypeError):
        pass

    return "\n\n".join(parts)


def _has_changes_requested(pr_number: int, repo: str) -> bool:
    """Return True if any reviewer has requested changes (and no subsequent approval)."""
    r = _gh("pr", "view", str(pr_number), "--repo", repo, "--json", "reviews")
    try:
        reviews = json.loads(r.stdout).get("reviews") or []
    except (json.JSONDecodeError, AttributeError):
        return False

    # Track latest review state per reviewer
    latest: dict[str, str] = {}
    for review in reviews:
        author = (review.get("author") or {}).get("login", "")
        state = review.get("state", "").upper()
        if author and state in ("CHANGES_REQUESTED", "APPROVED", "DISMISSED"):
            latest[author] = state

    return any(s == "CHANGES_REQUESTED" for s in latest.values())


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

    def recover(self, node: GraphNode, config: Config) -> NodeResult | None:
        """Merge nodes have no recoverable state — always re-dispatch."""
        return None

    async def _dispatch_conflict_resolution(
        self, pr_number: int, branch: str, repo: str, config: Config
    ) -> None:
        """Dispatch an agent to rebase the branch onto main and resolve conflicts."""
        from breadforge.agents.runner import run_agent

        prompt = f"""You are resolving merge conflicts on PR #{pr_number} in repo {repo} on branch `{branch}`.

The branch needs to be rebased onto main. Some files may have conflicts.

Steps:
1. `gh repo clone {repo} . && git fetch origin && git checkout {branch}`
2. `git fetch origin main && git rebase origin/main`
3. For each conflicted file:
   - Read both sides of the conflict carefully
   - Keep ALL new functionality from BOTH sides — do not drop features
   - Resolve using the current file structure (refer to other non-conflicted files for context)
   - `git add <resolved-file>`
4. `git rebase --continue` (repeat step 3 for each commit if multi-commit rebase)
5. `git push --force-with-lease origin {branch}`
6. STOP — do not merge, do not close the PR

You must preserve all intentional changes from the PR. Do not discard any features."""

        workspace = Path(tempfile.mkdtemp(prefix=f"breadforge-conflict-{pr_number}-"))
        result = await run_agent(
            prompt,
            model=config.model,
            timeout_minutes=30,
            cwd=workspace,
            allowed_tools=["Bash", "Edit", "Write", "Read", "Glob", "Grep", "MultiEdit"],
        )
        if self._logger:
            self._logger.info(
                f"conflict-resolution agent for PR #{pr_number} exited {result.exit_code}",
                node_id=f"conflict-{pr_number}",
            )

    async def _dispatch_review_fix(
        self,
        pr_number: int,
        branch: str,
        repo: str,
        config: Config,
        review_comments: str,
    ) -> None:
        """Dispatch an agent to address reviewer feedback on the PR."""
        from breadforge.agents.runner import run_agent

        prompt = f"""You are addressing reviewer feedback on PR #{pr_number} in repo {repo} on branch `{branch}`.

Reviewer feedback to address:
{review_comments}

Steps:
1. `gh repo clone {repo} . && git checkout {branch}`
2. Read the full PR: `gh pr view {pr_number} --repo {repo}`
3. Read each piece of reviewer feedback above carefully
4. For each comment:
   - If it requests a code change: make the minimal fix that addresses the concern
   - If it asks a question: answer it in a PR comment via `gh pr comment {pr_number} --repo {repo} --body "..."`
   - If it is a false positive or out of scope: reply in a PR comment explaining why
5. Run tests: `uv run pytest` (or equivalent)
6. Run lint: `uv run ruff check`
7. Commit changes: `git commit -am "fix: address reviewer feedback on PR #{pr_number}"`
8. `git push origin {branch}`
9. STOP — do not merge

Address every comment. Do not ignore any reviewer feedback."""

        workspace = Path(tempfile.mkdtemp(prefix=f"breadforge-review-{pr_number}-"))
        result = await run_agent(
            prompt,
            model=config.model,
            timeout_minutes=25,
            cwd=workspace,
            allowed_tools=["Bash", "Edit", "Write", "Read", "Glob", "Grep", "MultiEdit"],
        )
        if self._logger:
            self._logger.info(
                f"review-fix agent for PR #{pr_number} exited {result.exit_code}",
                node_id=f"review-{pr_number}",
            )

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

        # Step 1: Conflict check — dispatch conflict-resolution agent if PR is conflicting
        if branch:
            mergeable = _gh("pr", "view", str(pr_number), "--repo", repo, "--json", "mergeable")
            try:
                is_conflicting = json.loads(mergeable.stdout).get("mergeable") == "CONFLICTING"
            except (json.JSONDecodeError, AttributeError):
                is_conflicting = False

            if is_conflicting:
                conflict_count: int = node.context.get("conflict_count", 0)
                if conflict_count < MAX_CONFLICT_ATTEMPTS:
                    node.context["conflict_count"] = conflict_count + 1
                    if self._store:
                        self._store.write_node(node)
                    if self._logger:
                        self._logger.info(
                            f"PR #{pr_number} has conflicts — dispatching conflict-resolution agent "
                            f"(attempt {conflict_count + 1}/{MAX_CONFLICT_ATTEMPTS})",
                            node_id=node.id,
                        )
                    await self._dispatch_conflict_resolution(pr_number, branch, repo, config)
                    # Let CI pick up the push before polling
                    await asyncio.sleep(CI_POLL_INTERVAL_SECONDS)
                    return NodeResult(
                        success=False,
                        error=f"PR #{pr_number} had conflicts; resolution agent dispatched, retrying",
                    )
                return NodeResult(
                    success=False,
                    error=f"PR #{pr_number} still conflicting after {conflict_count} resolution attempt(s)",
                )

        # Step 2: Review comment check — dispatch review-fix agent if changes were requested
        if branch and _has_changes_requested(pr_number, repo):
            review_count: int = node.context.get("review_count", 0)
            if review_count < MAX_REVIEW_ATTEMPTS:
                review_comments = _get_pending_review_comments(pr_number, repo)
                node.context["review_count"] = review_count + 1
                if self._store:
                    self._store.write_node(node)
                if self._logger:
                    self._logger.info(
                        f"PR #{pr_number} has change requests — dispatching review-fix agent "
                        f"(attempt {review_count + 1}/{MAX_REVIEW_ATTEMPTS})",
                        node_id=node.id,
                    )
                await self._dispatch_review_fix(pr_number, branch, repo, config, review_comments)
                await asyncio.sleep(CI_POLL_INTERVAL_SECONDS)
                return NodeResult(
                    success=False,
                    error=f"PR #{pr_number} had change requests; review-fix agent dispatched, retrying",
                )
            if self._logger:
                self._logger.info(
                    f"PR #{pr_number}: {review_count} review-fix attempt(s) exhausted, proceeding to merge",
                    node_id=node.id,
                )

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
        return NodeResult(
            success=True,
            output={"pr_number": pr_number, "merged": True},
        )
