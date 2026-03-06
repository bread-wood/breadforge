"""BugHandler — files a GitHub issue for a validation failure and emits a build node.

When a validate node fails, the executor can dispatch a bug node whose context
carries the failure details.  BugHandler:

1. Reads failure details from ``node.context`` (command, stdout, stderr, exit_code).
2. Creates a GitHub issue labelled ``bug`` and ``stage/impl`` assigned to the
   milestone, with a body that reproduces the full failure.
3. Returns a ``NodeResult`` with ``success=True`` and ``new_nodes`` containing a
   single ``build`` node whose context references the new bug issue number
   (rather than a plan artifact).

Context keys consumed
---------------------
command : str
    The validate command that failed (e.g. ``"uv run pytest"``).
stdout : str
    Captured stdout from the failing command.
stderr : str
    Captured stderr from the failing command.
exit_code : int
    Exit code of the failing command.
milestone : str
    GitHub milestone title to assign to the filed issue.
module : str
    Module label (e.g. ``"mod:runner"``); used in the issue title and as the
    build node's ``module`` context key.
files : list[str]
    Allowed file scope forwarded to the build node context.
issue_title : str, optional
    Override for the filed issue title.  Defaults to a generated title.

Output keys (on success)
------------------------
bug_issue_number : int
    The GitHub issue number that was created.
new_nodes : list[dict]
    One-element list containing a serialised ``build`` ``GraphNode`` that the
    executor will add to the graph.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

from breadforge.beads.types import GraphNode
from breadforge.graph.node import NodeResult, make_node

if TYPE_CHECKING:
    from breadforge.config import Config

_TRUNCATE_BYTES = 8_000  # cap captured output in issue body to stay within GH limits


def _gh(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def _truncate(text: str, limit: int = _TRUNCATE_BYTES) -> str:
    """Return *text* truncated to *limit* bytes, appending a note if cut."""
    encoded = text.encode()
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode(errors="replace") + f"\n... [truncated to {limit} bytes]"


def _build_issue_body(
    command: str,
    stdout: str,
    stderr: str,
    exit_code: int,
) -> str:
    """Render a Markdown issue body reproducing the full validation failure."""
    return (
        "## Validation failure\n\n"
        f"**Exit code:** `{exit_code}`\n\n"
        "**Command:**\n"
        f"```\n{command}\n```\n\n"
        "**stdout:**\n"
        f"```\n{_truncate(stdout) or '(empty)'}\n```\n\n"
        "**stderr:**\n"
        f"```\n{_truncate(stderr) or '(empty)'}\n```\n"
    )


def _create_github_issue(
    repo: str,
    title: str,
    body: str,
    milestone: str,
) -> int | None:
    """Create a GitHub issue and return its number, or None on failure."""
    args = [
        "issue",
        "create",
        "--repo",
        repo,
        "--title",
        title,
        "--body",
        body,
        "--label",
        "bug",
        "--label",
        "stage/impl",
    ]
    if milestone:
        args += ["--milestone", milestone]

    result = _gh(*args)
    if result.returncode != 0:
        return None

    # gh issue create prints the issue URL; extract the number from the last segment
    url = result.stdout.strip()
    try:
        return int(url.rstrip("/").rsplit("/", 1)[-1])
    except (ValueError, IndexError):
        return None


def _build_node_for_issue(
    parent_node_id: str,
    issue_number: int,
    module: str,
    files: list[str],
    milestone: str,
) -> dict[str, Any]:
    """Return a serialised build GraphNode that implements the bug-fix issue."""
    node_id = f"bug-fix-{issue_number}"
    node = make_node(
        id=node_id,
        type="build",
        depends_on=[parent_node_id],
        context={
            "issue_number": issue_number,
            "module": module,
            "files": files,
            "milestone": milestone,
            # Explicitly no plan_artifact — the build node works from the issue body
        },
    )
    return node.model_dump(mode="json")


class BugHandler:
    """Files a GitHub issue for a validation failure and emits a remedial build node."""

    def __init__(self, store=None, logger=None) -> None:
        self._store = store
        self._logger = logger

    async def execute(self, node: GraphNode, config: Config) -> NodeResult:
        command: str = node.context.get("command", "")
        stdout: str = node.context.get("stdout", "")
        stderr: str = node.context.get("stderr", "")
        exit_code: int = int(node.context.get("exit_code", 1))
        milestone: str = node.context.get("milestone", "")
        module: str = node.context.get("module", "")
        files: list[str] = node.context.get("files", [])
        repo = config.repo

        issue_title: str = node.context.get(
            "issue_title",
            f"fix({module or 'validate'}): repair validation failure"
            + (f" in {module}" if module else ""),
        )

        body = _build_issue_body(command, stdout, stderr, exit_code)

        issue_number = _create_github_issue(repo, issue_title, body, milestone)
        if issue_number is None:
            return NodeResult(
                success=False,
                error="failed to create bug issue on GitHub",
            )

        if self._logger:
            self._logger.info(
                f"bug handler filed issue #{issue_number} for node {node.id}",
                node_id=node.id,
            )

        new_node = _build_node_for_issue(node.id, issue_number, module, files, milestone)
        return NodeResult(
            success=True,
            output={
                "bug_issue_number": issue_number,
                "new_nodes": [new_node],
            },
        )

    def recover(self, node: GraphNode, config: Config) -> NodeResult | None:
        """If a bug issue was already filed, reconstruct the result without re-filing."""
        issue_number = node.output and node.output.get("bug_issue_number")
        if not issue_number:
            return None  # no recorded issue — re-dispatch

        module: str = node.context.get("module", "")
        files: list[str] = node.context.get("files", [])
        milestone: str = node.context.get("milestone", "")

        if self._logger:
            self._logger.info(
                f"recovered bug node {node.id}: issue #{issue_number} already filed",
                node_id=node.id,
            )

        new_node = _build_node_for_issue(node.id, issue_number, module, files, milestone)
        return NodeResult(
            success=True,
            output={
                "bug_issue_number": issue_number,
                "new_nodes": [new_node],
            },
        )
