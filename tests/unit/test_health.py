"""Unit tests for health.py — bot token validation and collaborator checks."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

from breadforge.health import CheckStatus, _check_bot_collaborator


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# _check_bot_collaborator
# ---------------------------------------------------------------------------


class TestCheckBotCollaborator:
    def test_already_collaborator(self) -> None:
        with patch("subprocess.run", return_value=_proc(0)) as mock_run:
            result = _check_bot_collaborator("owner/repo", "tok")
        assert result.status == CheckStatus.PASS
        assert "collaborator" in result.message
        assert mock_run.call_count == 1  # only the status check

    def test_not_collaborator_add_succeeds_invitation_accepted(self) -> None:
        invite_json = json.dumps([{"id": 7, "repository": {"full_name": "owner/repo"}}])
        responses = [
            _proc(1),  # collaborator check: not a member
            _proc(0),  # PUT: add succeeds
            _proc(0, invite_json),  # list invitations
            _proc(0, "204"),  # accept invitation
        ]
        with patch("subprocess.run", side_effect=responses):
            result = _check_bot_collaborator("owner/repo", "tok")
        assert result.status == CheckStatus.PASS
        assert "1 invitation" in result.message

    def test_not_collaborator_add_fails(self) -> None:
        responses = [
            _proc(1),  # collaborator check fails
            _proc(1, stderr="permission denied"),  # PUT fails
        ]
        with patch("subprocess.run", side_effect=responses):
            result = _check_bot_collaborator("owner/repo", "tok")
        assert result.status == CheckStatus.FAIL
        assert "auto-add failed" in result.message

    def test_no_bot_token_after_add(self) -> None:
        responses = [
            _proc(1),  # collaborator check fails
            _proc(0),  # PUT succeeds
        ]
        with patch("subprocess.run", side_effect=responses):
            result = _check_bot_collaborator("owner/repo", "")
        assert result.status == CheckStatus.FAIL
        assert "BREADFORGE_GH_TOKEN" in result.message

    def test_invitation_list_bad_json_treated_as_zero(self) -> None:
        responses = [
            _proc(1),
            _proc(0),
            _proc(0, "not-json"),
        ]
        with patch("subprocess.run", side_effect=responses):
            result = _check_bot_collaborator("owner/repo", "tok")
        assert result.status == CheckStatus.PASS
        assert "0 invitation" in result.message

    def test_invitation_list_contains_non_dicts(self) -> None:
        """Regression: GitHub error responses may embed strings in the list."""
        bad_json = json.dumps(["a string", None, 42])
        responses = [
            _proc(1),
            _proc(0),
            _proc(0, bad_json),
        ]
        with patch("subprocess.run", side_effect=responses):
            result = _check_bot_collaborator("owner/repo", "tok")
        assert result.status == CheckStatus.PASS
        assert "0 invitation" in result.message

    def test_invitation_for_other_repo_not_accepted(self) -> None:
        invite_json = json.dumps([{"id": 99, "repository": {"full_name": "other/repo"}}])
        responses = [
            _proc(1),
            _proc(0),
            _proc(0, invite_json),
            # no acceptance call expected
        ]
        with patch("subprocess.run", side_effect=responses) as mock_run:
            result = _check_bot_collaborator("owner/repo", "tok")
        assert result.status == CheckStatus.PASS
        assert "0 invitation" in result.message
        assert mock_run.call_count == 3  # check + PUT + list; no PATCH

    def test_multiple_invitations_all_accepted(self) -> None:
        invite_json = json.dumps(
            [
                {"id": 1, "repository": {"full_name": "owner/repo"}},
                {"id": 2, "repository": {"full_name": "owner/repo"}},
            ]
        )
        responses = [
            _proc(1),
            _proc(0),
            _proc(0, invite_json),
            _proc(0, "204"),  # accept inv 1
            _proc(0, "204"),  # accept inv 2
        ]
        with patch("subprocess.run", side_effect=responses):
            result = _check_bot_collaborator("owner/repo", "tok")
        assert result.status == CheckStatus.PASS
        assert "2 invitation" in result.message

    def test_acceptance_non_204_returns_warn(self) -> None:
        invite_json = json.dumps([{"id": 1, "repository": {"full_name": "owner/repo"}}])
        responses = [
            _proc(1),
            _proc(0),
            _proc(0, invite_json),
            _proc(0, "403"),
        ]
        with patch("subprocess.run", side_effect=responses):
            result = _check_bot_collaborator("owner/repo", "tok")
        assert result.status == CheckStatus.WARN
        assert "403" in result.message

    def test_put_strips_gh_token_from_env(self) -> None:
        """PUT must not send GH_TOKEN so it runs as repo owner, not yeast-bot."""
        captured_env: dict = {}
        call_index = 0

        def fake_run(cmd, **kwargs):
            nonlocal call_index
            call_index += 1
            if call_index == 1:  # collaborator check — not a member
                return _proc(1)
            if call_index == 2:  # the PUT call
                captured_env.update(kwargs.get("env") or {})
                return _proc(0)
            return _proc(0, "[]")  # list invitations

        with (
            patch("subprocess.run", side_effect=fake_run),
            patch.dict(os.environ, {"GH_TOKEN": "owner-token"}),
        ):
            _check_bot_collaborator("owner/repo", "bot-token")

        assert call_index >= 2, "PUT was never called"
        assert "GH_TOKEN" not in captured_env


# ---------------------------------------------------------------------------
# Bot token validation (via run_health_checks)
# ---------------------------------------------------------------------------


class TestBotTokenValidation:
    def _run_checks(self, token: str, token_http_code: str):
        from breadforge.health import run_health_checks

        def fake_run(cmd, **kwargs):
            cmd_str = " ".join(str(c) for c in cmd)
            if "api.github.com/user" in cmd_str and "invitations" not in cmd_str:
                return _proc(0, token_http_code)
            if "auth" in cmd_str:
                return _proc(0)
            if "repo" in cmd_str and "view" in cmd_str:
                return _proc(0, '{"name": "repo"}')
            if "collaborators/yeast-bot" in cmd_str:
                return _proc(0)
            return _proc(0)

        env = (
            {"BREADFORGE_GH_TOKEN": token, "ANTHROPIC_API_KEY": "x"}
            if token
            else {"ANTHROPIC_API_KEY": "x"}
        )
        with (
            patch("subprocess.run", side_effect=fake_run),
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch.dict(os.environ, env, clear=True),
        ):
            return run_health_checks("owner/repo")

    def test_valid_token_passes(self) -> None:
        report = self._run_checks("valid-token", "200")
        check = next(c for c in report.checks if c.name == "bot-token")
        assert check.status == CheckStatus.PASS

    def test_invalid_token_fails(self) -> None:
        report = self._run_checks("bad-token", "401")
        check = next(c for c in report.checks if c.name == "bot-token")
        assert check.status == CheckStatus.FAIL
        assert "401" in check.message

    def test_missing_token_fails(self) -> None:
        report = self._run_checks("", "")
        check = next(c for c in report.checks if c.name == "bot-token")
        assert check.status == CheckStatus.FAIL
        assert "not set" in check.message
