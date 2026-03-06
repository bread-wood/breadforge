"""Unit tests for agent prompt templates."""

from __future__ import annotations

from breadforge.agents.prompts import PLAN_PROMPT, RESEARCH_PROMPT, build_agent_prompt


class TestBuildAgentPrompt:
    def test_basic_prompt_contains_required_fields(self) -> None:
        prompt = build_agent_prompt(
            issue_number=42,
            issue_title="Add feature X",
            issue_body="Detailed description.",
            branch="42-add-feature-x",
            repo="owner/repo",
        )
        assert "#42" in prompt
        assert "owner/repo" in prompt
        assert "42-add-feature-x" in prompt
        assert "Add feature X" in prompt
        assert "Detailed description." in prompt

    def test_workspace_ready_skips_clone(self) -> None:
        prompt = build_agent_prompt(
            issue_number=1,
            issue_title="Fix",
            issue_body="",
            branch="1-fix",
            repo="owner/repo",
            workspace_ready=True,
        )
        # workspace_ready tells the agent NOT to clone (the instruction says "Do NOT run git clone")
        assert "gh repo clone" not in prompt
        assert "git branch --show-current" in prompt

    def test_workspace_not_ready_includes_clone(self) -> None:
        prompt = build_agent_prompt(
            issue_number=2,
            issue_title="Feature",
            issue_body="",
            branch="2-feature",
            repo="owner/repo",
            workspace_ready=False,
        )
        assert "gh repo clone" in prompt

    def test_allowed_scope_included_when_workspace_ready(self) -> None:
        prompt = build_agent_prompt(
            issue_number=3,
            issue_title="Scoped",
            issue_body="",
            branch="3-scoped",
            repo="owner/repo",
            allowed_scope=["src/core.py", "src/utils.py"],
            workspace_ready=True,
        )
        assert "src/core.py" in prompt
        assert "src/utils.py" in prompt
        assert "SCOPE ENFORCEMENT IS ACTIVE" in prompt

    def test_allowed_scope_included_when_not_workspace_ready(self) -> None:
        prompt = build_agent_prompt(
            issue_number=4,
            issue_title="Scoped",
            issue_body="",
            branch="4-scoped",
            repo="owner/repo",
            allowed_scope=["src/a.py"],
            workspace_ready=False,
        )
        assert "src/a.py" in prompt

    def test_no_scope_no_scope_lines(self) -> None:
        prompt = build_agent_prompt(
            issue_number=5,
            issue_title="Free",
            issue_body="",
            branch="5-free",
            repo="owner/repo",
            workspace_ready=True,
        )
        assert "SCOPE ENFORCEMENT" not in prompt

    def test_stop_instruction_included(self) -> None:
        prompt = build_agent_prompt(
            issue_number=6,
            issue_title="Work",
            issue_body="",
            branch="6-work",
            repo="owner/repo",
        )
        assert "STOP" in prompt
        assert "Do not merge" in prompt

    def test_already_implemented_section_included(self) -> None:
        prompt = build_agent_prompt(
            issue_number=7,
            issue_title="Maybe done",
            issue_body="",
            branch="7-maybe",
            repo="owner/repo",
        )
        assert "already been implemented" in prompt or "already implemented" in prompt


class TestResearchPrompt:
    def test_is_string(self) -> None:
        assert isinstance(RESEARCH_PROMPT, str)

    def test_contains_format_vars(self) -> None:
        assert "{repo}" in RESEARCH_PROMPT
        assert "{milestone}" in RESEARCH_PROMPT
        assert "{unknowns}" in RESEARCH_PROMPT

    def test_formattable(self) -> None:
        result = RESEARCH_PROMPT.format(
            repo="owner/repo",
            milestone="v1.0",
            unknowns="- Which library?",
        )
        assert "owner/repo" in result
        assert "v1.0" in result

    def test_confidence_section_mentioned(self) -> None:
        assert "Confidence" in RESEARCH_PROMPT or "confidence" in RESEARCH_PROMPT


class TestPlanPrompt:
    def test_is_string(self) -> None:
        assert isinstance(PLAN_PROMPT, str)

    def test_contains_format_vars(self) -> None:
        assert "{spec_text}" in PLAN_PROMPT
        assert "{codebase_context}" in PLAN_PROMPT
        assert "{research_findings}" in PLAN_PROMPT

    def test_formattable(self) -> None:
        result = PLAN_PROMPT.format(
            spec_text="# My spec",
            codebase_context="Some context",
            research_findings="No findings",
        )
        assert "# My spec" in result

    def test_json_schema_in_prompt(self) -> None:
        assert '"modules"' in PLAN_PROMPT or "modules" in PLAN_PROMPT
        assert '"confidence"' in PLAN_PROMPT or "confidence" in PLAN_PROMPT
