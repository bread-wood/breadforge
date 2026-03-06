"""Unit tests for BugHandler in graph/handlers/bug.py."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from breadforge.config import Config
from breadforge.graph.handlers.bug import (
    BugHandler,
    _build_issue_body,
    _build_node_for_issue,
    _create_github_issue,
    _truncate,
)
from breadforge.graph.node import make_node

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(repo: str = "owner/repo") -> Config:
    return Config(repo=repo)


def _make_node(context: dict | None = None) -> object:
    return make_node(id="test-bug-1", type="build", context=context or {})


def _completed_proc(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# _truncate
# ---------------------------------------------------------------------------


class TestTruncate:
    def test_short_text_unchanged(self) -> None:
        assert _truncate("hello") == "hello"

    def test_empty_string_unchanged(self) -> None:
        assert _truncate("") == ""

    def test_text_at_limit_unchanged(self) -> None:
        text = "x" * 8_000
        result = _truncate(text)
        assert result == text

    def test_text_over_limit_is_cut(self) -> None:
        text = "a" * 9_000
        result = _truncate(text, limit=8_000)
        assert len(result.encode()) <= 8_000 + 60  # small allowance for the appended note
        assert "truncated" in result

    def test_custom_limit(self) -> None:
        text = "z" * 100
        result = _truncate(text, limit=10)
        assert "truncated" in result
        assert result.startswith("z" * 10)

    def test_multibyte_chars_handled(self) -> None:
        # Each '€' is 3 bytes; a 10-byte limit should cut cleanly
        text = "€" * 20  # 60 bytes
        result = _truncate(text, limit=10)
        assert "truncated" in result


# ---------------------------------------------------------------------------
# _build_issue_body
# ---------------------------------------------------------------------------


class TestBuildIssueBody:
    def test_contains_exit_code(self) -> None:
        body = _build_issue_body("pytest", "ok", "err", 2)
        assert "`2`" in body

    def test_contains_command(self) -> None:
        body = _build_issue_body("uv run pytest", "out", "err", 1)
        assert "uv run pytest" in body

    def test_contains_stdout(self) -> None:
        body = _build_issue_body("cmd", "my stdout", "", 0)
        assert "my stdout" in body

    def test_contains_stderr(self) -> None:
        body = _build_issue_body("cmd", "", "my stderr", 0)
        assert "my stderr" in body

    def test_empty_stdout_shows_placeholder(self) -> None:
        body = _build_issue_body("cmd", "", "", 0)
        assert "(empty)" in body

    def test_empty_stderr_shows_placeholder(self) -> None:
        body = _build_issue_body("cmd", "", "", 0)
        assert body.count("(empty)") >= 1

    def test_markdown_structure(self) -> None:
        body = _build_issue_body("cmd", "out", "err", 1)
        assert "## Validation failure" in body
        assert "```" in body


# ---------------------------------------------------------------------------
# _create_github_issue
# ---------------------------------------------------------------------------


class TestCreateGithubIssue:
    def test_returns_issue_number_from_url(self) -> None:
        proc = _completed_proc(stdout="https://github.com/owner/repo/issues/99\n")
        with patch("breadforge.graph.handlers.bug.subprocess.run", return_value=proc):
            number = _create_github_issue("owner/repo", "title", "body", "v1")
        assert number == 99

    def test_returns_none_on_nonzero_returncode(self) -> None:
        proc = _completed_proc(returncode=1, stderr="not found")
        with patch("breadforge.graph.handlers.bug.subprocess.run", return_value=proc):
            number = _create_github_issue("owner/repo", "title", "body", "v1")
        assert number is None

    def test_returns_none_when_url_unparseable(self) -> None:
        proc = _completed_proc(stdout="not a url\n")
        with patch("breadforge.graph.handlers.bug.subprocess.run", return_value=proc):
            number = _create_github_issue("owner/repo", "title", "body", "v1")
        assert number is None

    def test_milestone_included_in_gh_args(self) -> None:
        proc = _completed_proc(stdout="https://github.com/owner/repo/issues/7\n")
        with patch("breadforge.graph.handlers.bug.subprocess.run", return_value=proc) as mock_run:
            _create_github_issue("owner/repo", "title", "body", "validate")
        call_args = mock_run.call_args[0][0]
        assert "--milestone" in call_args
        assert "validate" in call_args

    def test_labels_bug_and_stage_impl_included(self) -> None:
        proc = _completed_proc(stdout="https://github.com/owner/repo/issues/5\n")
        with patch("breadforge.graph.handlers.bug.subprocess.run", return_value=proc) as mock_run:
            _create_github_issue("owner/repo", "title", "body", "v1")
        call_args = mock_run.call_args[0][0]
        # Both labels should appear
        label_indices = [i for i, a in enumerate(call_args) if a == "--label"]
        label_values = [call_args[i + 1] for i in label_indices]
        assert "bug" in label_values
        assert "stage/impl" in label_values

    def test_empty_milestone_skips_milestone_flag(self) -> None:
        proc = _completed_proc(stdout="https://github.com/owner/repo/issues/3\n")
        with patch("breadforge.graph.handlers.bug.subprocess.run", return_value=proc) as mock_run:
            _create_github_issue("owner/repo", "title", "body", "")
        call_args = mock_run.call_args[0][0]
        assert "--milestone" not in call_args

    def test_returns_none_when_stdout_has_no_slash(self) -> None:
        proc = _completed_proc(stdout="12345\n")
        with patch("breadforge.graph.handlers.bug.subprocess.run", return_value=proc):
            number = _create_github_issue("owner/repo", "title", "body", "")
        # "12345" rsplit("/", 1) → ["12345"]; int("12345") = 12345 — actually valid
        assert number == 12345

    def test_trailing_slash_stripped(self) -> None:
        proc = _completed_proc(stdout="https://github.com/owner/repo/issues/42/\n")
        with patch("breadforge.graph.handlers.bug.subprocess.run", return_value=proc):
            number = _create_github_issue("owner/repo", "title", "body", "")
        assert number == 42


# ---------------------------------------------------------------------------
# _build_node_for_issue
# ---------------------------------------------------------------------------


class TestBuildNodeForIssue:
    def test_returns_dict(self) -> None:
        result = _build_node_for_issue("parent-1", 10, "mod:runner", ["src/x.py"], "v1")
        assert isinstance(result, dict)

    def test_type_is_build(self) -> None:
        result = _build_node_for_issue("parent-1", 10, "mod:runner", [], "v1")
        assert result["type"] == "build"

    def test_depends_on_parent(self) -> None:
        result = _build_node_for_issue("parent-99", 10, "mod:runner", [], "v1")
        assert "parent-99" in result["depends_on"]

    def test_issue_number_in_context(self) -> None:
        result = _build_node_for_issue("p", 55, "mod:runner", [], "v1")
        assert result["context"]["issue_number"] == 55

    def test_module_in_context(self) -> None:
        result = _build_node_for_issue("p", 55, "mod:cli", [], "v1")
        assert result["context"]["module"] == "mod:cli"

    def test_files_in_context(self) -> None:
        files = ["src/a.py", "src/b.py"]
        result = _build_node_for_issue("p", 55, "", files, "v1")
        assert result["context"]["files"] == files

    def test_milestone_in_context(self) -> None:
        result = _build_node_for_issue("p", 55, "", [], "validate")
        assert result["context"]["milestone"] == "validate"

    def test_no_plan_artifact_in_context(self) -> None:
        result = _build_node_for_issue("p", 55, "", [], "v1")
        assert "plan_artifact" not in result["context"]

    def test_node_id_contains_issue_number(self) -> None:
        result = _build_node_for_issue("p", 77, "", [], "v1")
        assert "77" in result["id"]


# ---------------------------------------------------------------------------
# BugHandler.execute
# ---------------------------------------------------------------------------


class TestBugHandlerExecute:
    """Tests for BugHandler.execute()."""

    @pytest.mark.asyncio
    async def test_happy_path_returns_success(self) -> None:
        node = _make_node(
            {
                "command": "uv run pytest",
                "stdout": "FAILED tests/test_x.py",
                "stderr": "",
                "exit_code": 1,
                "milestone": "validate",
                "module": "mod:runner",
                "files": ["src/breadforge/runner.py"],
            }
        )
        config = _make_config()
        proc = _completed_proc(stdout="https://github.com/owner/repo/issues/101\n")

        with patch("breadforge.graph.handlers.bug.subprocess.run", return_value=proc):
            result = await BugHandler().execute(node, config)

        assert result.success is True
        assert result.output["bug_issue_number"] == 101

    @pytest.mark.asyncio
    async def test_happy_path_emits_one_build_node(self) -> None:
        node = _make_node(
            {
                "command": "uv run pytest",
                "stdout": "",
                "stderr": "error",
                "exit_code": 2,
                "milestone": "v1",
                "module": "mod:cli",
                "files": [],
            }
        )
        config = _make_config()
        proc = _completed_proc(stdout="https://github.com/owner/repo/issues/200\n")

        with patch("breadforge.graph.handlers.bug.subprocess.run", return_value=proc):
            result = await BugHandler().execute(node, config)

        assert len(result.output["new_nodes"]) == 1
        emitted = result.output["new_nodes"][0]
        assert emitted["type"] == "build"
        assert emitted["context"]["issue_number"] == 200

    @pytest.mark.asyncio
    async def test_failure_to_create_issue_returns_failure(self) -> None:
        node = _make_node({"command": "cmd", "stdout": "", "stderr": "", "exit_code": 1})
        config = _make_config()
        proc = _completed_proc(returncode=1, stderr="gh error")

        with patch("breadforge.graph.handlers.bug.subprocess.run", return_value=proc):
            result = await BugHandler().execute(node, config)

        assert result.success is False
        assert "bug issue" in result.error

    @pytest.mark.asyncio
    async def test_default_issue_title_contains_module(self) -> None:
        node = _make_node(
            {"command": "c", "stdout": "", "stderr": "", "exit_code": 1, "module": "mod:health"}
        )
        config = _make_config()
        proc = _completed_proc(stdout="https://github.com/owner/repo/issues/5\n")

        with patch("breadforge.graph.handlers.bug.subprocess.run", return_value=proc) as mock_run:
            await BugHandler().execute(node, config)

        call_args = mock_run.call_args[0][0]
        title_idx = call_args.index("--title")
        title = call_args[title_idx + 1]
        assert "mod:health" in title

    @pytest.mark.asyncio
    async def test_custom_issue_title_is_used(self) -> None:
        node = _make_node(
            {
                "command": "c",
                "stdout": "",
                "stderr": "",
                "exit_code": 1,
                "issue_title": "My custom bug title",
            }
        )
        config = _make_config()
        proc = _completed_proc(stdout="https://github.com/owner/repo/issues/9\n")

        with patch("breadforge.graph.handlers.bug.subprocess.run", return_value=proc) as mock_run:
            await BugHandler().execute(node, config)

        call_args = mock_run.call_args[0][0]
        title_idx = call_args.index("--title")
        assert call_args[title_idx + 1] == "My custom bug title"

    @pytest.mark.asyncio
    async def test_logger_called_on_success(self) -> None:
        node = _make_node({"command": "c", "stdout": "", "stderr": "", "exit_code": 1})
        config = _make_config()
        proc = _completed_proc(stdout="https://github.com/owner/repo/issues/3\n")
        logger = MagicMock()

        with patch("breadforge.graph.handlers.bug.subprocess.run", return_value=proc):
            await BugHandler(logger=logger).execute(node, config)

        logger.info.assert_called_once()
        assert "3" in logger.info.call_args[0][0]

    @pytest.mark.asyncio
    async def test_missing_context_keys_use_defaults(self) -> None:
        """Empty context should not raise; defaults are used instead."""
        node = _make_node({})
        config = _make_config()
        proc = _completed_proc(stdout="https://github.com/owner/repo/issues/1\n")

        with patch("breadforge.graph.handlers.bug.subprocess.run", return_value=proc):
            result = await BugHandler().execute(node, config)

        assert result.success is True

    @pytest.mark.asyncio
    async def test_exit_code_coerced_to_int(self) -> None:
        """exit_code stored as string in context should be coerced."""
        node = _make_node({"command": "c", "stdout": "", "stderr": "", "exit_code": "3"})
        config = _make_config()
        proc = _completed_proc(stdout="https://github.com/owner/repo/issues/2\n")

        with patch("breadforge.graph.handlers.bug.subprocess.run", return_value=proc) as mock_run:
            result = await BugHandler().execute(node, config)

        assert result.success is True
        # Confirm body contained "`3`"
        call_args = mock_run.call_args[0][0]
        body_idx = call_args.index("--body")
        assert "`3`" in call_args[body_idx + 1]

    @pytest.mark.asyncio
    async def test_uses_config_repo(self) -> None:
        node = _make_node({"command": "c", "stdout": "", "stderr": "", "exit_code": 1})
        config = _make_config(repo="myorg/myrepo")
        proc = _completed_proc(stdout="https://github.com/myorg/myrepo/issues/8\n")

        with patch("breadforge.graph.handlers.bug.subprocess.run", return_value=proc) as mock_run:
            await BugHandler().execute(node, config)

        call_args = mock_run.call_args[0][0]
        assert "myorg/myrepo" in call_args


# ---------------------------------------------------------------------------
# BugHandler.recover
# ---------------------------------------------------------------------------


class TestBugHandlerRecover:
    def test_returns_none_when_no_output(self) -> None:
        node = _make_node({})
        node.output = None
        config = _make_config()
        assert BugHandler().recover(node, config) is None

    def test_returns_none_when_no_bug_issue_number(self) -> None:
        node = _make_node({})
        node.output = {"other_key": 42}
        config = _make_config()
        assert BugHandler().recover(node, config) is None

    def test_returns_success_with_existing_issue(self) -> None:
        node = _make_node({"module": "mod:spec", "files": ["src/a.py"], "milestone": "v1"})
        node.output = {"bug_issue_number": 77}
        config = _make_config()

        result = BugHandler().recover(node, config)

        assert result is not None
        assert result.success is True
        assert result.output["bug_issue_number"] == 77

    def test_recover_emits_same_build_node(self) -> None:
        node = _make_node({"module": "mod:spec", "files": ["f.py"], "milestone": "v2"})
        node.output = {"bug_issue_number": 55}
        config = _make_config()

        result = BugHandler().recover(node, config)

        assert result is not None
        new_nodes = result.output["new_nodes"]
        assert len(new_nodes) == 1
        emitted = new_nodes[0]
        assert emitted["type"] == "build"
        assert emitted["context"]["issue_number"] == 55
        assert emitted["context"]["files"] == ["f.py"]
        assert emitted["context"]["milestone"] == "v2"

    def test_recover_logger_called(self) -> None:
        node = _make_node({"module": "", "files": [], "milestone": ""})
        node.output = {"bug_issue_number": 10}
        config = _make_config()
        logger = MagicMock()

        BugHandler(logger=logger).recover(node, config)

        logger.info.assert_called_once()
        assert "10" in logger.info.call_args[0][0]
