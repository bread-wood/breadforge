"""Extended health.py tests — HealthReport properties, nesting guard, tool checks."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from breadforge.health import CheckResult, CheckStatus, HealthReport, run_health_checks


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# HealthReport properties
# ---------------------------------------------------------------------------


class TestHealthReport:
    def test_healthy_when_all_pass(self) -> None:
        report = HealthReport(checks=[
            CheckResult("a", CheckStatus.PASS, "ok"),
            CheckResult("b", CheckStatus.PASS, "ok"),
        ])
        assert report.healthy is True

    def test_healthy_with_warnings(self) -> None:
        report = HealthReport(checks=[
            CheckResult("a", CheckStatus.PASS, "ok"),
            CheckResult("b", CheckStatus.WARN, "careful"),
        ])
        assert report.healthy is True

    def test_not_healthy_with_failure(self) -> None:
        report = HealthReport(checks=[
            CheckResult("a", CheckStatus.PASS, "ok"),
            CheckResult("b", CheckStatus.FAIL, "broken"),
        ])
        assert report.healthy is False

    def test_fatal_returns_only_fails(self) -> None:
        report = HealthReport(checks=[
            CheckResult("a", CheckStatus.PASS, "ok"),
            CheckResult("b", CheckStatus.FAIL, "broken"),
            CheckResult("c", CheckStatus.WARN, "careful"),
        ])
        fatal = report.fatal
        assert len(fatal) == 1
        assert fatal[0].name == "b"

    def test_warnings_returns_only_warns(self) -> None:
        report = HealthReport(checks=[
            CheckResult("a", CheckStatus.PASS, "ok"),
            CheckResult("b", CheckStatus.FAIL, "broken"),
            CheckResult("c", CheckStatus.WARN, "careful"),
        ])
        warns = report.warnings
        assert len(warns) == 1
        assert warns[0].name == "c"

    def test_empty_report_is_healthy(self) -> None:
        report = HealthReport(checks=[])
        assert report.healthy is True
        assert report.fatal == []
        assert report.warnings == []


# ---------------------------------------------------------------------------
# run_health_checks — tool presence and auth paths
# ---------------------------------------------------------------------------


def _make_fake_run(*, claude: bool = True, gh_auth: bool = True, repo_ok: bool = True):
    """Factory for fake subprocess.run that controls tool availability."""
    def fake_run(cmd, **kwargs):
        cmd_str = " ".join(str(c) for c in cmd)
        if "auth" in cmd_str:
            return _proc(0 if gh_auth else 1, stderr="not authenticated")
        if "repo" in cmd_str and "view" in cmd_str:
            return _proc(0 if repo_ok else 1, stdout='{"name":"repo"}')
        if "api.github.com/user" in cmd_str and "invitations" not in cmd_str:
            return _proc(0, "200")
        if "collaborators" in cmd_str:
            return _proc(0)  # already collaborator
        return _proc(0)
    return fake_run


class TestRunHealthChecks:
    def _run(self, env: dict, *, claude: bool = True, gh: bool = True, git: bool = True,
             gh_auth: bool = True, repo_ok: bool = True) -> HealthReport:
        with patch("subprocess.run", side_effect=_make_fake_run(gh_auth=gh_auth, repo_ok=repo_ok)):
            with patch("shutil.which", side_effect=lambda t: f"/usr/bin/{t}" if (
                (t == "claude" and claude) or
                (t == "gh" and gh) or
                (t == "git" and git)
            ) else None):
                with patch.dict(os.environ, env, clear=True):
                    return run_health_checks("owner/repo")

    def test_claude_not_found_fails(self) -> None:
        report = self._run(
            {"ANTHROPIC_API_KEY": "x", "BREADFORGE_GH_TOKEN": "tok"},
            claude=False,
        )
        check = next(c for c in report.checks if c.name == "claude-cli")
        assert check.status == CheckStatus.FAIL

    def test_claude_found_passes(self) -> None:
        report = self._run({"ANTHROPIC_API_KEY": "x", "BREADFORGE_GH_TOKEN": "tok"})
        check = next(c for c in report.checks if c.name == "claude-cli")
        assert check.status == CheckStatus.PASS

    def test_gh_not_found_fails(self) -> None:
        report = self._run(
            {"ANTHROPIC_API_KEY": "x", "BREADFORGE_GH_TOKEN": "tok"},
            gh=False,
        )
        check = next(c for c in report.checks if "gh" in c.name)
        assert check.status == CheckStatus.FAIL

    def test_gh_auth_fails(self) -> None:
        report = self._run(
            {"ANTHROPIC_API_KEY": "x", "BREADFORGE_GH_TOKEN": "tok"},
            gh_auth=False,
        )
        check = next(c for c in report.checks if c.name == "gh-auth")
        assert check.status == CheckStatus.FAIL

    def test_git_not_found_fails(self) -> None:
        report = self._run(
            {"ANTHROPIC_API_KEY": "x", "BREADFORGE_GH_TOKEN": "tok"},
            git=False,
        )
        check = next(c for c in report.checks if c.name == "git")
        assert check.status == CheckStatus.FAIL

    def test_repo_inaccessible_fails(self) -> None:
        report = self._run(
            {"ANTHROPIC_API_KEY": "x", "BREADFORGE_GH_TOKEN": "tok"},
            repo_ok=False,
        )
        check = next(c for c in report.checks if c.name == "repo-access")
        assert check.status == CheckStatus.FAIL

    def test_anthropic_key_missing_warns(self) -> None:
        report = self._run({"BREADFORGE_GH_TOKEN": "tok"})
        check = next(c for c in report.checks if c.name == "anthropic-key")
        assert check.status == CheckStatus.WARN

    def test_anthropic_key_set_passes(self) -> None:
        report = self._run({"ANTHROPIC_API_KEY": "sk-test", "BREADFORGE_GH_TOKEN": "tok"})
        check = next(c for c in report.checks if c.name == "anthropic-key")
        assert check.status == CheckStatus.PASS

    def test_proxy_secret_missing_warns(self) -> None:
        report = self._run({"ANTHROPIC_API_KEY": "x", "BREADFORGE_GH_TOKEN": "tok"})
        check = next(c for c in report.checks if c.name == "proxy-secret")
        assert check.status == CheckStatus.WARN

    def test_proxy_secret_set_passes(self) -> None:
        report = self._run({
            "ANTHROPIC_API_KEY": "x",
            "BREADFORGE_GH_TOKEN": "tok",
            "BREADFORGE_PROXY_SECRET": "secret123",
        })
        check = next(c for c in report.checks if c.name == "proxy-secret")
        assert check.status == CheckStatus.PASS


# ---------------------------------------------------------------------------
# Nesting guard
# ---------------------------------------------------------------------------


class TestNestingGuard:
    def _run_checks(self, extra_env: dict) -> HealthReport:
        env = {"ANTHROPIC_API_KEY": "x", "BREADFORGE_GH_TOKEN": "tok", **extra_env}
        with patch("subprocess.run", side_effect=_make_fake_run()):
            with patch("shutil.which", return_value="/usr/bin/tool"):
                with patch.dict(os.environ, env, clear=True):
                    return run_health_checks("owner/repo")

    def test_nesting_guard_pass_when_clean(self) -> None:
        report = self._run_checks({})
        check = next(c for c in report.checks if c.name == "nesting-guard")
        assert check.status == CheckStatus.PASS

    def test_nesting_guard_fail_inside_agent(self) -> None:
        report = self._run_checks({"BREADFORGE_AGENT": "1"})
        check = next(c for c in report.checks if c.name == "nesting-guard")
        assert check.status == CheckStatus.FAIL
        assert "cannot run inside" in check.message

    def test_nesting_guard_warn_inside_claude_code(self) -> None:
        report = self._run_checks({"CLAUDE_CODE": "1"})
        check = next(c for c in report.checks if c.name == "nesting-guard")
        assert check.status == CheckStatus.WARN
        assert "intentional" in check.message


# ---------------------------------------------------------------------------
# gh auth timeout → WARN
# ---------------------------------------------------------------------------


class TestGhAuthTimeout:
    def test_gh_auth_timeout_warns(self) -> None:
        import subprocess

        def fake_run(cmd, **kwargs):
            cmd_str = " ".join(str(c) for c in cmd)
            if "auth" in cmd_str:
                raise subprocess.TimeoutExpired(cmd, 10)
            if "api.github.com/user" in cmd_str and "invitations" not in cmd_str:
                return _proc(0, "200")
            if "collaborators" in cmd_str:
                return _proc(0)
            return _proc(0, '{"name":"repo"}')

        with patch("subprocess.run", side_effect=fake_run):
            with patch("shutil.which", return_value="/usr/bin/tool"):
                with patch.dict(os.environ, {"BREADFORGE_GH_TOKEN": "tok", "ANTHROPIC_API_KEY": "x"}, clear=True):
                    report = run_health_checks("owner/repo")

        check = next(c for c in report.checks if c.name == "gh-auth")
        assert check.status == CheckStatus.WARN
        assert "timed out" in check.message


# ---------------------------------------------------------------------------
# Repo access timeout → WARN
# ---------------------------------------------------------------------------


class TestRepoAccessTimeout:
    def test_repo_access_timeout_warns(self) -> None:
        import subprocess

        def fake_run(cmd, **kwargs):
            cmd_str = " ".join(str(c) for c in cmd)
            if "repo" in cmd_str and "view" in cmd_str:
                raise subprocess.TimeoutExpired(cmd, 15)
            if "api.github.com/user" in cmd_str and "invitations" not in cmd_str:
                return _proc(0, "200")
            if "collaborators" in cmd_str:
                return _proc(0)
            return _proc(0)

        with patch("subprocess.run", side_effect=fake_run):
            with patch("shutil.which", return_value="/usr/bin/tool"):
                with patch.dict(os.environ, {"BREADFORGE_GH_TOKEN": "tok", "ANTHROPIC_API_KEY": "x"}, clear=True):
                    report = run_health_checks("owner/repo")

        check = next(c for c in report.checks if c.name == "repo-access")
        assert check.status == CheckStatus.WARN
        assert "timed out" in check.message
