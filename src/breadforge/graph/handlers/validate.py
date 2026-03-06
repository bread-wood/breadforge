"""ValidateHandler — runs validation assertions and manages fix-cycle escalation.

Parses validation assertions from the spec context, runs each as a subprocess
(60 s timeout, capturing stdout/stderr/exit-code), tracks per-assertion
fix-cycle counts in bead metadata, and:

- Emits ``bug`` nodes for failed assertions that have not yet hit the
  escalation limit (3 cycles).
- Adds ``needs-human`` label via the GitHub API when an assertion reaches the
  3-cycle limit, and omits further fix nodes for that assertion.
- Closes the milestone tracking issue when every assertion passes.

Context keys
------------
spec_markdown : str
    Raw spec text from which ``parse_validation_assertions()`` extracts
    assertion commands.  Ignored when ``assertions`` is set directly.
assertions : list[str]
    Pre-parsed assertion shell commands.  Takes precedence over
    ``spec_markdown``.
tracking_issue_number : int | None
    GitHub issue number to close (with a comment) when all assertions pass.
fix_cycles : dict[str, int]
    Per-assertion fix-cycle counters keyed by assertion text.
    Read from node context on entry and written back on exit.

Output keys (NodeResult.output)
--------------------------------
passed : list[str]      — assertion commands that exited 0
failed : list[str]      — assertion commands that exited non-0 or timed out
bug_nodes : list[dict]  — serialised GraphNode dicts for bug-fix dispatch
escalated : list[str]   — assertions that hit the MAX_FIX_CYCLES limit
all_passed : bool       — True when every assertion passed
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

from breadforge.beads.types import GraphNode
from breadforge.graph.node import NodeResult
from breadforge.spec import parse_validation_assertions

if TYPE_CHECKING:
    from breadforge.config import Config

MAX_FIX_CYCLES = 3
ASSERTION_TIMEOUT_SECONDS = 60


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------


def _gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def _add_needs_human_label(repo: str, issue_number: int) -> None:
    _gh("issue", "edit", str(issue_number), "--repo", repo, "--add-label", "needs-human")


def _close_tracking_issue(repo: str, issue_number: int, comment: str) -> None:
    _gh(
        "issue",
        "close",
        str(issue_number),
        "--repo",
        repo,
        "--comment",
        comment,
    )


# ---------------------------------------------------------------------------
# Assertion runner
# ---------------------------------------------------------------------------


def _run_assertion(assertion: str) -> tuple[int, str, str]:
    """Run *assertion* as a shell command.

    Returns ``(exit_code, stdout, stderr)``.  On timeout the exit code is 1
    and stderr contains a human-readable timeout message.
    """
    try:
        result = subprocess.run(
            assertion,
            shell=True,
            capture_output=True,
            text=True,
            timeout=ASSERTION_TIMEOUT_SECONDS,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 1, "", f"assertion timed out after {ASSERTION_TIMEOUT_SECONDS}s"


# ---------------------------------------------------------------------------
# Bug node factory
# ---------------------------------------------------------------------------


def _make_bug_node(
    node_id: str,
    assertion: str,
    exit_code: int,
    stdout: str,
    stderr: str,
) -> dict[str, Any]:
    """Return a serialised GraphNode dict for a failed assertion.

    The ``bug`` node type is handled by a separate module (mod:graph-bug-handler).
    We emit it here as a plain dict so the executor can materialise and dispatch it.
    """
    return {
        "id": node_id,
        "type": "bug",
        "state": "pending",
        "depends_on": [],
        "context": {
            "assertion": assertion,
            "exit_code": exit_code,
            # Truncate to avoid bloating the bead store with huge outputs.
            "stdout": stdout[:2000],
            "stderr": stderr[:2000],
        },
        "output": None,
        "assigned_model": None,
        "retry_count": 0,
        "max_retries": 3,
    }


# ---------------------------------------------------------------------------
# ValidateHandler
# ---------------------------------------------------------------------------


class ValidateHandler:
    """Runs validation assertions and manages fix-cycle escalation.

    Conforms to the ``NodeHandler`` protocol: provides ``execute`` (async)
    and ``recover`` (sync) methods.
    """

    def __init__(
        self,
        store: Any = None,
        logger: Any = None,
    ) -> None:
        self._store = store
        self._logger = logger

    async def execute(self, node: GraphNode, config: Config) -> NodeResult:
        assertions = self._parse_assertions(node)

        if not assertions:
            if self._logger:
                self._logger.info(
                    f"validate node {node.id}: no assertions found — skipping",
                    node_id=node.id,
                )
            return NodeResult(
                success=True,
                output={
                    "passed": [],
                    "failed": [],
                    "bug_nodes": [],
                    "escalated": [],
                    "all_passed": True,
                },
            )

        # fix_cycles is a mutable copy; we write it back to node.context at the end.
        fix_cycles: dict[str, int] = dict(node.context.get("fix_cycles", {}))
        repo = config.repo

        passed: list[str] = []
        failed: list[str] = []
        bug_nodes: list[dict[str, Any]] = []
        escalated: list[str] = []

        for i, assertion in enumerate(assertions):
            exit_code, stdout, stderr = _run_assertion(assertion)

            if exit_code == 0:
                passed.append(assertion)
                if self._logger:
                    self._logger.info(
                        f"validate node {node.id}: assertion[{i}] passed",
                        node_id=node.id,
                    )
                continue

            # Assertion failed — determine escalation vs. bug node.
            failed.append(assertion)
            cycle_count = fix_cycles.get(assertion, 0)

            if self._logger:
                self._logger.info(
                    f"validate node {node.id}: assertion[{i}] failed "
                    f"(exit={exit_code}, cycles={cycle_count})",
                    node_id=node.id,
                )

            if cycle_count >= MAX_FIX_CYCLES:
                # Escalate: no more automated fix attempts.
                escalated.append(assertion)
                tracking_issue: int | None = node.context.get("tracking_issue_number")
                if tracking_issue:
                    _add_needs_human_label(repo, tracking_issue)
                if self._logger:
                    self._logger.info(
                        f"validate node {node.id}: assertion[{i}] escalated "
                        f"(hit {MAX_FIX_CYCLES}-cycle limit) — needs-human label added",
                        node_id=node.id,
                    )
            else:
                # Increment cycle count and emit a bug node for the fix agent.
                fix_cycles[assertion] = cycle_count + 1
                bug_node_id = f"{node.id}-bug-{i}-cycle{fix_cycles[assertion]}"
                bug_nodes.append(_make_bug_node(bug_node_id, assertion, exit_code, stdout, stderr))

        # Persist updated fix-cycle counters back into the node context so the
        # executor can serialise them with the node state.
        node.context["fix_cycles"] = fix_cycles

        all_passed = not failed

        if all_passed:
            tracking_issue = node.context.get("tracking_issue_number")
            if tracking_issue:
                _close_tracking_issue(
                    repo,
                    tracking_issue,
                    "All validation assertions passed. Closing milestone tracking issue.",
                )
                if self._logger:
                    self._logger.info(
                        f"validate node {node.id}: all assertions passed — "
                        f"closed tracking issue #{tracking_issue}",
                        node_id=node.id,
                    )

        return NodeResult(
            success=all_passed,
            output={
                "passed": passed,
                "failed": failed,
                "bug_nodes": bug_nodes,
                "escalated": escalated,
                "all_passed": all_passed,
            },
            error=None if all_passed else f"{len(failed)} assertion(s) failed",
        )

    def recover(self, node: GraphNode, config: Config) -> NodeResult | None:
        """Re-run assertions on restart — results are not idempotently cached."""
        return None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _parse_assertions(self, node: GraphNode) -> list[str]:
        """Extract assertion commands from node context.

        Prefers ``context["assertions"]`` (a pre-parsed list) over parsing
        ``context["spec_markdown"]`` with ``parse_validation_assertions()``.
        """
        direct: Any = node.context.get("assertions")
        if direct and isinstance(direct, list):
            return [str(a) for a in direct if str(a).strip()]

        spec_markdown: str = node.context.get("spec_markdown", "")
        if spec_markdown:
            return parse_validation_assertions(spec_markdown)

        return []
