"""Unit tests for validate node state formatting in the status command."""

from __future__ import annotations

from breadforge.beads import GraphNode
from breadforge.cli import _format_validate_state


def _make_validate_node(
    state: str,
    output: dict | None = None,
    context: dict | None = None,
) -> GraphNode:
    """Create a GraphNode with type='validate' for testing.

    Uses model_construct to bypass Pydantic validation so the 'validate' node type
    (not yet in the NodeType literal — added by a separate PR) can be exercised.
    """
    return GraphNode.model_construct(
        id="v1-validate",
        type="validate",
        state=state,
        output=output,
        context=context or {},
        depends_on=[],
        retry_count=0,
        max_retries=3,
        assigned_model=None,
    )


class TestFormatValidateState:
    def test_pending_state(self) -> None:
        node = _make_validate_node("pending")
        assert _format_validate_state(node) == "pending"

    def test_running_state(self) -> None:
        node = _make_validate_node("running")
        assert _format_validate_state(node) == "running"

    def test_done_with_all_pass_output(self) -> None:
        """done state + failed_count=0 in output → passed."""
        node = _make_validate_node("done", output={"failed_count": 0})
        assert _format_validate_state(node) == "passed"

    def test_done_with_failures_in_output(self) -> None:
        """done state + failed_count=3 in output → failed(3)."""
        node = _make_validate_node("done", output={"failed_count": 3})
        assert _format_validate_state(node) == "failed(3)"

    def test_failed_state_with_failure_count(self) -> None:
        """failed state + failed_count=2 → failed(2)."""
        node = _make_validate_node("failed", output={"failed_count": 2})
        assert _format_validate_state(node) == "failed(2)"

    def test_done_no_output(self) -> None:
        """done state with no output → passed (assumed all clear)."""
        node = _make_validate_node("done", output=None)
        assert _format_validate_state(node) == "passed"

    def test_done_with_passed_list_all_pass(self) -> None:
        """done state, assertions == passed list → passed."""
        assertions = ["cmd1", "cmd2"]
        node = _make_validate_node(
            "done",
            output={"passed": ["cmd1", "cmd2"]},
            context={"assertions": assertions},
        )
        assert _format_validate_state(node) == "passed"

    def test_done_with_passed_list_partial_pass(self) -> None:
        """done state, some assertions not in passed list → failed(N)."""
        assertions = ["cmd1", "cmd2", "cmd3"]
        node = _make_validate_node(
            "done",
            output={"passed": ["cmd1"]},
            context={"assertions": assertions},
        )
        # 3 total - 1 passed = 2 failed
        assert _format_validate_state(node) == "failed(2)"

    def test_failed_state_no_output(self) -> None:
        """failed state with no output → failed(0) as fallback."""
        node = _make_validate_node("failed", output=None)
        assert _format_validate_state(node) == "failed(0)"

    def test_abandoned_state(self) -> None:
        """Abandoned nodes show raw state."""
        node = _make_validate_node("abandoned")
        assert _format_validate_state(node) == "abandoned"

    def test_failed_count_takes_priority_over_passed_list(self) -> None:
        """failed_count in output takes priority over inferring from assertions."""
        assertions = ["cmd1", "cmd2"]
        node = _make_validate_node(
            "done",
            output={"failed_count": 1, "passed": ["cmd1", "cmd2"]},
            context={"assertions": assertions},
        )
        assert _format_validate_state(node) == "failed(1)"
