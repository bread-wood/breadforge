"""Unit tests for ValidateHandler.

Covers:
- All assertions pass: milestone tracking issue is closed, no bug nodes emitted
- Single failure: bug node emitted with correct context
- Timeout: treated as failure (non-zero exit code, timeout stderr)
- fix_cycle >= 3: no bug node emitted, needs-human label added to tracking issue
- Mixed pass/fail: only failures produce bug nodes
- No assertions in context: returns all_passed=True with empty lists
- spec_markdown path: parse_validation_assertions extracts assertions
- recover() always returns None (re-dispatch)
- Output truncation at 2000 chars
- fix_cycles counter is persisted back into node.context
"""

from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import (
    MagicMock,
    patch,
)

from breadforge.config import Config
from breadforge.graph.handlers.validate import (
    MAX_FIX_CYCLES,
    ValidateHandler,
    _add_needs_human_label,
    _close_tracking_issue,
    _make_bug_node,
    _run_assertion,
)
from breadforge.graph.node import make_node

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config() -> Config:
    return Config.from_env("owner/repo")


def _node(context: dict | None = None) -> object:
    """Return a GraphNode-like object usable with ValidateHandler."""
    return make_node("v1-validate", type="build", context=context or {})


# ---------------------------------------------------------------------------
# _run_assertion unit tests
# ---------------------------------------------------------------------------


class TestRunAssertion:
    def test_success_exit_zero(self) -> None:
        exit_code, stdout, stderr = _run_assertion("true")
        assert exit_code == 0

    def test_failure_exit_nonzero(self) -> None:
        exit_code, stdout, stderr = _run_assertion("false")
        assert exit_code != 0

    def test_stdout_captured(self) -> None:
        exit_code, stdout, stderr = _run_assertion("echo hello")
        assert exit_code == 0
        assert "hello" in stdout

    def test_stderr_captured(self) -> None:
        exit_code, stdout, stderr = _run_assertion("echo err >&2; false")
        assert exit_code != 0
        assert "err" in stderr

    def test_timeout_returns_failure(self) -> None:
        with patch("breadforge.graph.handlers.validate.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="sleep 100", timeout=60)
            exit_code, stdout, stderr = _run_assertion("sleep 100")
        assert exit_code == 1
        assert stdout == ""
        assert "timed out" in stderr
        assert "60" in stderr


# ---------------------------------------------------------------------------
# _make_bug_node unit tests
# ---------------------------------------------------------------------------


class TestMakeBugNode:
    def test_structure(self) -> None:
        node = _make_bug_node("v1-bug-0-cycle1", "pytest tests/", 1, "out", "err")
        assert node["id"] == "v1-bug-0-cycle1"
        assert node["type"] == "bug"
        assert node["state"] == "pending"
        assert node["depends_on"] == []
        assert node["context"]["assertion"] == "pytest tests/"
        assert node["context"]["exit_code"] == 1
        assert node["context"]["stdout"] == "out"
        assert node["context"]["stderr"] == "err"
        assert node["output"] is None

    def test_stdout_truncated_at_2000(self) -> None:
        long_out = "x" * 3000
        node = _make_bug_node("id", "cmd", 1, long_out, "")
        assert len(node["context"]["stdout"]) == 2000

    def test_stderr_truncated_at_2000(self) -> None:
        long_err = "y" * 3000
        node = _make_bug_node("id", "cmd", 1, "", long_err)
        assert len(node["context"]["stderr"]) == 2000


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------


class TestGitHubHelpers:
    def test_add_needs_human_label(self) -> None:
        with patch("breadforge.graph.handlers.validate.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _add_needs_human_label("owner/repo", 42)
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "needs-human" in args
        assert "42" in args

    def test_close_tracking_issue(self) -> None:
        with patch("breadforge.graph.handlers.validate.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _close_tracking_issue("owner/repo", 7, "All passed.")
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "close" in args
        assert "7" in args
        assert "All passed." in args


# ---------------------------------------------------------------------------
# ValidateHandler.execute — all-pass case
# ---------------------------------------------------------------------------


class TestValidateHandlerAllPass:
    def test_all_pass_no_tracking_issue(self) -> None:
        node = _node({"assertions": ["true", "true"]})
        with patch("breadforge.graph.handlers.validate._run_assertion", return_value=(0, "ok", "")):
            result = asyncio.run(ValidateHandler().execute(node, _config()))
        assert result.success is True
        assert result.output["all_passed"] is True
        assert result.output["passed"] == ["true", "true"]
        assert result.output["failed"] == []
        assert result.output["bug_nodes"] == []
        assert result.output["escalated"] == []

    def test_all_pass_closes_tracking_issue(self) -> None:
        node = _node({"assertions": ["true"], "tracking_issue_number": 99})
        with (
            patch("breadforge.graph.handlers.validate._run_assertion", return_value=(0, "ok", "")),
            patch("breadforge.graph.handlers.validate._close_tracking_issue") as mock_close,
        ):
            result = asyncio.run(ValidateHandler().execute(node, _config()))
        assert result.success is True
        mock_close.assert_called_once_with(
            "owner/repo",
            99,
            "All validation assertions passed. Closing milestone tracking issue.",
        )

    def test_no_assertions_returns_all_passed(self) -> None:
        node = _node({})
        result = asyncio.run(ValidateHandler().execute(node, _config()))
        assert result.success is True
        assert result.output["all_passed"] is True
        assert result.output["passed"] == []
        assert result.output["bug_nodes"] == []

    def test_empty_assertions_list_returns_all_passed(self) -> None:
        node = _node({"assertions": []})
        result = asyncio.run(ValidateHandler().execute(node, _config()))
        assert result.success is True
        assert result.output["all_passed"] is True


# ---------------------------------------------------------------------------
# ValidateHandler.execute — failure cases
# ---------------------------------------------------------------------------


class TestValidateHandlerFailures:
    def _run_with_results(self, assertions, per_cmd_results, context=None):
        """Helper that maps assertion commands to (exit_code, stdout, stderr) tuples."""
        ctx = {"assertions": assertions}
        if context:
            ctx.update(context)
        node = _node(ctx)

        results_iter = iter(per_cmd_results)

        def fake_run(assertion):
            return next(results_iter)

        with patch("breadforge.graph.handlers.validate._run_assertion", side_effect=fake_run):
            return asyncio.run(ValidateHandler().execute(node, _config())), node

    def test_single_failure_emits_bug_node(self) -> None:
        result, node = self._run_with_results(
            ["pytest tests/"],
            [(1, "", "1 failed")],
        )
        assert result.success is False
        assert result.output["failed"] == ["pytest tests/"]
        assert len(result.output["bug_nodes"]) == 1
        bug = result.output["bug_nodes"][0]
        assert bug["type"] == "bug"
        assert bug["context"]["assertion"] == "pytest tests/"
        assert bug["context"]["exit_code"] == 1
        assert bug["context"]["stderr"] == "1 failed"

    def test_single_failure_fix_cycle_incremented(self) -> None:
        result, node = self._run_with_results(
            ["pytest tests/"],
            [(1, "", "err")],
        )
        # fix_cycles should now be 1 for this assertion
        assert node.context["fix_cycles"]["pytest tests/"] == 1

    def test_bug_node_id_includes_cycle(self) -> None:
        result, node = self._run_with_results(
            ["false"],
            [(1, "", "err")],
        )
        bug = result.output["bug_nodes"][0]
        assert "cycle1" in bug["id"]

    def test_timeout_treated_as_failure(self) -> None:
        node = _node({"assertions": ["sleep 100"]})
        with patch(
            "breadforge.graph.handlers.validate._run_assertion",
            return_value=(1, "", "assertion timed out after 60s"),
        ):
            result = asyncio.run(ValidateHandler().execute(node, _config()))
        assert result.success is False
        assert len(result.output["bug_nodes"]) == 1
        bug = result.output["bug_nodes"][0]
        assert "timed out" in bug["context"]["stderr"]

    def test_mixed_pass_and_fail(self) -> None:
        result, node = self._run_with_results(
            ["true", "false", "true"],
            [(0, "ok", ""), (1, "", "err"), (0, "ok", "")],
        )
        assert result.success is False
        assert result.output["passed"] == ["true", "true"]
        assert result.output["failed"] == ["false"]
        assert len(result.output["bug_nodes"]) == 1

    def test_multiple_failures_emit_multiple_bug_nodes(self) -> None:
        result, node = self._run_with_results(
            ["a", "b", "c"],
            [(1, "", "e1"), (1, "", "e2"), (1, "", "e3")],
        )
        assert len(result.output["bug_nodes"]) == 3

    def test_error_message_reports_failure_count(self) -> None:
        result, _ = self._run_with_results(
            ["a", "b"],
            [(1, "", "e"), (1, "", "e")],
        )
        assert result.error is not None
        assert "2" in result.error


# ---------------------------------------------------------------------------
# ValidateHandler.execute — fix-cycle escalation
# ---------------------------------------------------------------------------


class TestValidateHandlerEscalation:
    def test_cycle_at_limit_escalates_no_bug_node(self) -> None:
        node = _node(
            {
                "assertions": ["pytest tests/"],
                "fix_cycles": {"pytest tests/": MAX_FIX_CYCLES},
                "tracking_issue_number": 5,
            }
        )
        with (
            patch("breadforge.graph.handlers.validate._run_assertion", return_value=(1, "", "err")),
            patch("breadforge.graph.handlers.validate._add_needs_human_label") as mock_label,
        ):
            result = asyncio.run(ValidateHandler().execute(node, _config()))
        assert result.output["bug_nodes"] == []
        assert result.output["escalated"] == ["pytest tests/"]
        mock_label.assert_called_once_with("owner/repo", 5)

    def test_cycle_below_limit_emits_bug_node(self) -> None:
        node = _node(
            {
                "assertions": ["pytest tests/"],
                "fix_cycles": {"pytest tests/": MAX_FIX_CYCLES - 1},
            }
        )
        with patch(
            "breadforge.graph.handlers.validate._run_assertion", return_value=(1, "", "err")
        ):
            result = asyncio.run(ValidateHandler().execute(node, _config()))
        assert len(result.output["bug_nodes"]) == 1

    def test_escalation_without_tracking_issue_no_gh_call(self) -> None:
        node = _node(
            {
                "assertions": ["pytest tests/"],
                "fix_cycles": {"pytest tests/": MAX_FIX_CYCLES},
            }
        )
        with (
            patch("breadforge.graph.handlers.validate._run_assertion", return_value=(1, "", "err")),
            patch("breadforge.graph.handlers.validate._add_needs_human_label") as mock_label,
        ):
            result = asyncio.run(ValidateHandler().execute(node, _config()))
        mock_label.assert_not_called()
        assert result.output["escalated"] == ["pytest tests/"]

    def test_mixed_escalated_and_bug_node(self) -> None:
        """One assertion at limit (escalated), one below (bug node emitted)."""
        node = _node(
            {
                "assertions": ["a", "b"],
                "fix_cycles": {"a": MAX_FIX_CYCLES, "b": 0},
                "tracking_issue_number": 10,
            }
        )
        side_effects = [(1, "", "err_a"), (1, "", "err_b")]
        with (
            patch("breadforge.graph.handlers.validate._run_assertion", side_effect=side_effects),
            patch("breadforge.graph.handlers.validate._add_needs_human_label") as mock_label,
        ):
            result = asyncio.run(ValidateHandler().execute(node, _config()))
        assert result.output["escalated"] == ["a"]
        assert len(result.output["bug_nodes"]) == 1
        assert result.output["bug_nodes"][0]["context"]["assertion"] == "b"
        mock_label.assert_called_once_with("owner/repo", 10)

    def test_fix_cycles_persisted_in_node_context(self) -> None:
        node = _node({"assertions": ["pytest tests/"], "fix_cycles": {}})
        with patch(
            "breadforge.graph.handlers.validate._run_assertion", return_value=(1, "", "err")
        ):
            asyncio.run(ValidateHandler().execute(node, _config()))
        # cycle counter incremented from 0 → 1
        assert node.context["fix_cycles"]["pytest tests/"] == 1


# ---------------------------------------------------------------------------
# ValidateHandler.execute — spec_markdown path
# ---------------------------------------------------------------------------


class TestValidateHandlerSpecMarkdown:
    SPEC_WITH_ASSERTIONS = """\
# Project v1.0 — Test Milestone

## Validation

```validate
pytest tests/unit/
uv run ruff check src/
```
"""

    def test_assertions_extracted_from_spec_markdown(self) -> None:
        node = _node({"spec_markdown": self.SPEC_WITH_ASSERTIONS})
        results = [(0, "ok", ""), (0, "ok", "")]
        with patch("breadforge.graph.handlers.validate._run_assertion", side_effect=results):
            result = asyncio.run(ValidateHandler().execute(node, _config()))
        assert result.success is True
        assert len(result.output["passed"]) == 2

    def test_assertions_key_takes_precedence_over_spec_markdown(self) -> None:
        node = _node(
            {
                "assertions": ["true"],
                "spec_markdown": self.SPEC_WITH_ASSERTIONS,
            }
        )
        with patch(
            "breadforge.graph.handlers.validate._run_assertion", return_value=(0, "ok", "")
        ) as mock_run:
            asyncio.run(ValidateHandler().execute(node, _config()))
        assert mock_run.call_count == 1  # only 1 assertion from the list, not 2 from markdown

    def test_empty_spec_markdown_returns_all_passed(self) -> None:
        node = _node({"spec_markdown": "# No validation section here"})
        result = asyncio.run(ValidateHandler().execute(node, _config()))
        assert result.success is True
        assert result.output["all_passed"] is True


# ---------------------------------------------------------------------------
# ValidateHandler.recover
# ---------------------------------------------------------------------------


class TestValidateHandlerRecover:
    def test_recover_returns_none(self) -> None:
        node = _node({})
        result = ValidateHandler().recover(node, _config())
        assert result is None


# ---------------------------------------------------------------------------
# Logger integration
# ---------------------------------------------------------------------------


class TestValidateHandlerLogger:
    def test_logger_called_on_pass(self) -> None:
        logger = MagicMock()
        node = _node({"assertions": ["true"]})
        with patch("breadforge.graph.handlers.validate._run_assertion", return_value=(0, "ok", "")):
            asyncio.run(ValidateHandler(logger=logger).execute(node, _config()))
        assert logger.info.called

    def test_logger_called_on_failure(self) -> None:
        logger = MagicMock()
        node = _node({"assertions": ["false"]})
        with patch(
            "breadforge.graph.handlers.validate._run_assertion", return_value=(1, "", "err")
        ):
            asyncio.run(ValidateHandler(logger=logger).execute(node, _config()))
        assert logger.info.called

    def test_logger_called_on_escalation(self) -> None:
        logger = MagicMock()
        node = _node(
            {
                "assertions": ["false"],
                "fix_cycles": {"false": MAX_FIX_CYCLES},
            }
        )
        with (
            patch("breadforge.graph.handlers.validate._run_assertion", return_value=(1, "", "err")),
            patch("breadforge.graph.handlers.validate._add_needs_human_label"),
        ):
            asyncio.run(ValidateHandler(logger=logger).execute(node, _config()))
        # Should log at least the escalation message
        assert logger.info.called

    def test_no_logger_does_not_raise(self) -> None:
        node = _node({"assertions": ["true"]})
        with patch("breadforge.graph.handlers.validate._run_assertion", return_value=(0, "ok", "")):
            result = asyncio.run(ValidateHandler(logger=None).execute(node, _config()))
        assert result.success is True
