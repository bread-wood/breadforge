"""BuildHandler — dispatches a Claude Code agent for an impl work item.

Ported from dispatch.py RollingDispatcher._start_agent + _handle_completion.
Key addition: uses assess_from_plan_artifact() when a PlanArtifact is available
in node.context, falling back to assess_and_allocate() on raw issue text.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from breadforge.agents.assessor import assess_and_allocate, assess_from_plan_artifact
from breadforge.agents.prompts import build_agent_prompt
from breadforge.agents.runner import run_agent
from breadforge.beads.types import GraphNode, MergeQueueItem, PRBead
from breadforge.graph.node import NodeResult

if TYPE_CHECKING:
    from breadforge.beads.store import BeadStore
    from breadforge.config import Config
    from breadforge.logger import Logger


def _gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def _get_pr_number(repo: str, branch: str) -> int | None:
    r = _gh("pr", "list", "--repo", repo, "--head", branch, "--json", "number", "--limit", "1")
    try:
        items = json.loads(r.stdout)
        return items[0]["number"] if items else None
    except (json.JSONDecodeError, KeyError, IndexError):
        return None


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


_PRE_COMMIT_HOOK = """\
#!/bin/sh
# breadforge scope enforcement — blocks commits touching out-of-scope files
SCOPE_FILE="$(git rev-parse --show-toplevel)/.breadforge-scope"
[ -f "$SCOPE_FILE" ] || exit 0

STAGED=$(git diff --cached --name-only)
VIOLATIONS=""
for f in $STAGED; do
  [ "$f" = ".breadforge-scope" ] && continue
  grep -qxF "$f" "$SCOPE_FILE" || VIOLATIONS="$VIOLATIONS  $f\\n"
done

if [ -n "$VIOLATIONS" ]; then
  printf "breadforge: commit blocked — files outside allowed scope:\\n%s" "$VIOLATIONS" >&2
  printf "Allowed files listed in .breadforge-scope\\n" >&2
  exit 1
fi
"""


def _setup_workspace(
    workspace: Path, repo: str, branch: str, allowed_files: list[str]
) -> str | None:
    """Clone repo, create branch, install scope-enforcement pre-commit hook.

    Returns an error string on failure, None on success.
    """
    # Clone (depth=1 for speed; agent can fetch more if needed)
    r = subprocess.run(
        ["gh", "repo", "clone", repo, str(workspace), "--", "--depth=1"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return f"clone failed: {r.stderr[:200]}"

    # Create + push branch (ignore error if branch already exists)
    subprocess.run(["git", "checkout", "-b", branch], capture_output=True, cwd=workspace)
    subprocess.run(["git", "push", "-u", "origin", branch], capture_output=True, cwd=workspace)

    if allowed_files:
        # Write allowed-files manifest
        (workspace / ".breadforge-scope").write_text("\n".join(allowed_files) + "\n")

        # Install pre-commit hook
        hook_path = workspace / ".git" / "hooks" / "pre-commit"
        hook_path.write_text(_PRE_COMMIT_HOOK)
        hook_path.chmod(0o755)

    return None


def _verify_pr_scope(pr_number: int, repo: str, allowed_files: list[str]) -> list[str]:
    """Return list of out-of-scope files changed in the PR. Empty = clean."""
    r = _gh("pr", "view", str(pr_number), "--repo", repo, "--json", "files")
    if r.returncode != 0:
        return []
    try:
        data = json.loads(r.stdout)
        changed = {f["path"] for f in data.get("files", [])}
    except (json.JSONDecodeError, KeyError):
        return []
    allowed = set(allowed_files) | {".breadforge-scope"}
    return sorted(changed - allowed)


def _get_issue(repo: str, issue_number: int) -> dict[str, Any]:
    r = _gh("issue", "view", str(issue_number), "--repo", repo, "--json", "title,body,labels")
    if r.returncode != 0:
        return {"title": f"Issue #{issue_number}", "body": "", "labels": []}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"title": f"Issue #{issue_number}", "body": "", "labels": []}


class BuildHandler:
    """Dispatches a Claude Code agent to implement a build node."""

    def __init__(
        self,
        store: BeadStore | None = None,
        logger: Logger | None = None,
    ) -> None:
        self._store = store
        self._logger = logger

    async def execute(self, node: GraphNode, config: Config) -> NodeResult:
        issue_number: int | None = node.context.get("issue_number")
        module: str = node.context.get("module", "")
        files: list[str] = node.context.get("files", [])
        milestone: str = node.context.get("milestone", "")
        repo = config.repo

        # Model selection — prefer assigned_model set by plan handler; fall back to assessor
        if node.assigned_model:
            model = node.assigned_model
        else:
            allocation = await self._assess(node, config)
            model = allocation.model

        # Branch name
        branch = node.context.get("branch") or self._make_branch(node.id, module)
        node.context["branch"] = branch

        # Gather issue context early so we have the real title for the bead
        issue_title = node.context.get("issue_title", f"Implement {module or milestone}")
        issue_body = node.context.get("issue_body", "")
        if issue_number and not issue_body:
            issue_data = _get_issue(repo, issue_number)
            issue_title = issue_data.get("title", issue_title)
            issue_body = issue_data.get("body", "")

        # Claim GitHub issue with correct title now that we have it
        if issue_number and self._store:
            bead = self._store.read_work_bead(issue_number)
            if bead:
                bead.branch = branch
                bead.state = "claimed"  # type: ignore[assignment]
                bead.node_id = node.id
                bead.model = model
                bead.title = issue_title
                self._store.write_work_bead(bead)
            _claim_issue(repo, issue_number)

        # Set up workspace: clone, branch, scope-enforcement hook
        workspace = Path(tempfile.mkdtemp(prefix=f"breadforge-{node.id}-"))
        setup_error = _setup_workspace(workspace, repo, branch, files)
        if setup_error:
            if issue_number:
                _unclaim_issue(repo, issue_number)
            return NodeResult(success=False, error=f"workspace setup: {setup_error}")

        prompt = build_agent_prompt(
            issue_number=issue_number or 0,
            issue_title=issue_title,
            issue_body=issue_body,
            branch=branch,
            repo=repo,
            allowed_scope=files or None,
            workspace_ready=True,
        )

        result = await run_agent(
            prompt,
            model=model,
            timeout_minutes=config.agent_timeout_minutes,
            cwd=workspace,
            allowed_tools=[
                "Bash",
                "Edit",
                "Write",
                "Read",
                "Glob",
                "Grep",
                "MultiEdit",
            ],
        )

        if self._logger:
            self._logger.agent_complete(
                issue_number or 0,
                branch,
                result.exit_code,
                result.duration_ms,
            )

        if not result.success:
            if issue_number:
                _unclaim_issue(repo, issue_number)
            return NodeResult(
                success=False,
                error=f"agent exit {result.exit_code}: {(result.stderr or '')[:200]}",
            )

        # Find the PR the agent created — retry a few times for GitHub API latency
        import asyncio as _asyncio

        pr_number = _get_pr_number(repo, branch)
        if not pr_number:
            # Wait up to 90s for the PR to appear (3 × 30s)
            for _ in range(3):
                await _asyncio.sleep(30)
                pr_number = _get_pr_number(repo, branch)
                if pr_number:
                    break

        if not pr_number:
            if issue_number:
                _unclaim_issue(repo, issue_number)
            return NodeResult(success=False, error="agent completed but no PR found")

        # Verify scope: fail if any out-of-scope files were changed
        if files:
            violations = _verify_pr_scope(pr_number, repo, files)
            if violations:
                body = (
                    "**Scope violation** — this PR modifies files outside the allowed scope.\n\n"
                    "Out-of-scope files:\n"
                    + "\n".join(f"- `{v}`" for v in violations)
                    + "\n\nAllowed scope:\n"
                    + "\n".join(f"- `{f}`" for f in files)
                    + "\n\nPlease revert out-of-scope changes and push again."
                )
                _gh("pr", "comment", str(pr_number), "--repo", repo, "--body", body)
                if self._logger:
                    self._logger.error(
                        f"PR #{pr_number} scope violation: {violations}",
                        node_id=node.id,
                    )
                return NodeResult(
                    success=False,
                    error=f"scope violation in PR #{pr_number}: {violations}",
                )

        # Update beads
        if self._store and issue_number:
            bead = self._store.read_work_bead(issue_number)
            if bead:
                bead.pr_number = pr_number
                bead.state = "pr_open"  # type: ignore[assignment]
                self._store.write_work_bead(bead)

            pr_bead = PRBead(
                pr_number=pr_number,
                repo=repo,
                issue_number=issue_number,
                branch=branch,
            )
            self._store.write_pr_bead(pr_bead)
            self._store.enqueue_merge(
                MergeQueueItem(pr_number=pr_number, issue_number=issue_number, branch=branch)
            )

        # Comment progress on the milestone issue
        milestone_issue = node.context.get("milestone_issue_number")
        if milestone_issue:
            module = node.context.get("module", "")
            _post_comment(
                repo,
                milestone_issue,
                f"**`{module}` module done** — PR #{pr_number} opened on `{branch}`",
            )

        return NodeResult(
            success=True,
            output={"pr_number": pr_number, "branch": branch, "model": model},
        )

    async def _assess(self, node: GraphNode, config: Config):
        from breadforge.beads.types import PlanArtifact

        override = config.model if config.model else None

        artifact_data = node.context.get("plan_artifact")
        if artifact_data:
            try:
                artifact = PlanArtifact.model_validate(artifact_data)
                module = node.context.get("module", "")
                return assess_from_plan_artifact(artifact, module, override_model=override)
            except Exception:
                pass

        issue_title = node.context.get("issue_title", "")
        issue_body = node.context.get("issue_body", "")
        allocation, _ = await assess_and_allocate(issue_title, issue_body, override_model=override)
        return allocation

    def recover(self, node: GraphNode, config: Config) -> NodeResult | None:
        """If the node was running when breadforge crashed, check if a PR already exists."""
        branch = node.context.get("branch")
        if not branch:
            return None  # no branch recorded — re-dispatch
        pr_number = _get_pr_number(config.repo, branch)
        if pr_number:
            if self._logger:
                self._logger.info(
                    f"recovered build node {node.id}: PR #{pr_number} already exists on {branch}",
                    node_id=node.id,
                )
            return NodeResult(
                success=True,
                output={
                    "pr_number": pr_number,
                    "branch": branch,
                    "model": node.assigned_model or config.model,
                },
            )
        return None  # no PR found — re-dispatch

    def _make_branch(self, node_id: str, module: str) -> str:
        import re

        slug = re.sub(r"[^a-zA-Z0-9._-]", "-", (module or node_id).lower())[:40].strip("-")
        return f"graph-{slug}"
