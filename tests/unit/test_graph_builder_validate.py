"""Unit tests for validate and bug node emitters in graph/builder.py, and
for the validate/bug handler registration in executor.py make_handlers()."""

from __future__ import annotations

from breadforge.graph.builder import emit_bug_node, emit_validate_node
from breadforge.graph.executor import make_handlers

# ---------------------------------------------------------------------------
# emit_validate_node
# ---------------------------------------------------------------------------


class TestEmitValidateNode:
    def test_basic_structure(self) -> None:
        node = emit_validate_node(
            milestone="v1",
            repo="owner/repo",
            assertions=["bw status --json | jq -e '.ok'"],
        )
        assert node.id == "v1-validate"
        assert node.type == "validate"
        assert node.context["milestone"] == "v1"
        assert node.context["repo"] == "owner/repo"
        assert node.context["assertions"] == ["bw status --json | jq -e '.ok'"]
        assert node.context["fix_cycle"] == 0
        assert node.depends_on == []
        assert node.max_retries == 3

    def test_depends_on_readme(self) -> None:
        node = emit_validate_node(
            milestone="v2",
            repo="owner/repo",
            assertions=["echo ok"],
            depends_on=["v2-readme"],
        )
        assert "v2-readme" in node.depends_on

    def test_fix_cycle_stored(self) -> None:
        node = emit_validate_node(
            milestone="v1",
            repo="owner/repo",
            assertions=["echo ok"],
            fix_cycle=2,
        )
        assert node.context["fix_cycle"] == 2

    def test_milestone_issue_number_included_when_given(self) -> None:
        node = emit_validate_node(
            milestone="v1",
            repo="owner/repo",
            assertions=["echo ok"],
            milestone_issue_number=42,
        )
        assert node.context["milestone_issue_number"] == 42

    def test_milestone_issue_number_absent_when_not_given(self) -> None:
        node = emit_validate_node(
            milestone="v1",
            repo="owner/repo",
            assertions=["echo ok"],
        )
        assert "milestone_issue_number" not in node.context

    def test_multiple_assertions(self) -> None:
        assertions = [
            "bw speculate --dry-run --json | jq -e '.ok'",
            "bw heartbeat --json | jq -e '.checked >= 0'",
        ]
        node = emit_validate_node(
            milestone="v1",
            repo="owner/repo",
            assertions=assertions,
        )
        assert node.context["assertions"] == assertions

    def test_empty_assertions(self) -> None:
        node = emit_validate_node(
            milestone="v1",
            repo="owner/repo",
            assertions=[],
        )
        assert node.context["assertions"] == []


# ---------------------------------------------------------------------------
# emit_bug_node
# ---------------------------------------------------------------------------


class TestEmitBugNode:
    def test_basic_structure(self) -> None:
        node = emit_bug_node(
            milestone="v1",
            repo="owner/repo",
            assertion="bw speculate --dry-run",
            exit_code=1,
            stdout="",
            stderr="Error: not found",
        )
        assert node.type == "bug"
        assert node.id.startswith("v1-bug-")
        assert node.context["milestone"] == "v1"
        assert node.context["repo"] == "owner/repo"
        assert node.context["assertion"] == "bw speculate --dry-run"
        assert node.context["exit_code"] == 1
        assert node.context["stdout"] == ""
        assert node.context["stderr"] == "Error: not found"
        assert node.context["fix_cycle"] == 1
        assert node.depends_on == []
        assert node.max_retries == 1

    def test_node_id_is_deterministic(self) -> None:
        """Same assertion always yields the same node ID."""
        assertion = "bw speculate --dry-run --json | jq -e '.ok'"
        node1 = emit_bug_node(
            milestone="v1",
            repo="owner/repo",
            assertion=assertion,
            exit_code=1,
            stdout="",
            stderr="",
        )
        node2 = emit_bug_node(
            milestone="v1",
            repo="owner/repo",
            assertion=assertion,
            exit_code=1,
            stdout="",
            stderr="",
        )
        assert node1.id == node2.id

    def test_different_assertions_different_ids(self) -> None:
        node_a = emit_bug_node(
            milestone="v1",
            repo="owner/repo",
            assertion="cmd a",
            exit_code=1,
            stdout="",
            stderr="",
        )
        node_b = emit_bug_node(
            milestone="v1",
            repo="owner/repo",
            assertion="cmd b",
            exit_code=1,
            stdout="",
            stderr="",
        )
        assert node_a.id != node_b.id

    def test_fix_cycle_stored(self) -> None:
        node = emit_bug_node(
            milestone="v1",
            repo="owner/repo",
            assertion="echo ok",
            exit_code=2,
            stdout="out",
            stderr="err",
            fix_cycle=3,
        )
        assert node.context["fix_cycle"] == 3

    def test_milestone_issue_number_included_when_given(self) -> None:
        node = emit_bug_node(
            milestone="v1",
            repo="owner/repo",
            assertion="echo ok",
            exit_code=1,
            stdout="",
            stderr="",
            milestone_issue_number=99,
        )
        assert node.context["milestone_issue_number"] == 99

    def test_milestone_issue_number_absent_when_not_given(self) -> None:
        node = emit_bug_node(
            milestone="v1",
            repo="owner/repo",
            assertion="echo ok",
            exit_code=1,
            stdout="",
            stderr="",
        )
        assert "milestone_issue_number" not in node.context

    def test_depends_on(self) -> None:
        node = emit_bug_node(
            milestone="v1",
            repo="owner/repo",
            assertion="echo ok",
            exit_code=1,
            stdout="",
            stderr="",
            depends_on=["v1-validate"],
        )
        assert "v1-validate" in node.depends_on


# ---------------------------------------------------------------------------
# make_handlers — validate and bug registration
# ---------------------------------------------------------------------------


class TestMakeHandlers:
    def test_core_handlers_present(self) -> None:
        handlers = make_handlers()
        for key in (
            "plan",
            "research",
            "build",
            "merge",
            "readme",
            "wait",
            "consensus",
            "design_doc",
        ):
            assert key in handlers, f"handler {key!r} missing from make_handlers()"

    def test_validate_handler_registered_when_module_available(self) -> None:
        """If validate.py exists, 'validate' key is in handlers."""
        import importlib

        try:
            importlib.import_module("breadforge.graph.handlers.validate")
        except ImportError:
            # Handler not yet built — skip this assertion
            return

        handlers = make_handlers()
        assert "validate" in handlers

    def test_bug_handler_registered_when_module_available(self) -> None:
        """If bug.py exists, 'bug' key is in handlers."""
        import importlib

        try:
            importlib.import_module("breadforge.graph.handlers.bug")
        except ImportError:
            return

        handlers = make_handlers()
        assert "bug" in handlers

    def test_make_handlers_does_not_raise_when_handlers_missing(self) -> None:
        """make_handlers() must not raise even if validate.py / bug.py are absent."""
        # This test verifies the defensive import pattern works without side effects.
        handlers = make_handlers()
        assert isinstance(handlers, dict)
        assert len(handlers) >= 8
