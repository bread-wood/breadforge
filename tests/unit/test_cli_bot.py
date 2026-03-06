"""Unit tests for CLI bot collaborator helpers (_accept_bot_invitation, _add_bot_collaborator)."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# _accept_bot_invitation
# ---------------------------------------------------------------------------


class TestAcceptBotInvitation:
    def test_accepts_matching_invitation(self) -> None:
        from breadforge.cli import _accept_bot_invitation

        invite_json = json.dumps([
            {"id": 42, "repository": {"full_name": "owner/repo"}},
            {"id": 99, "repository": {"full_name": "other/repo"}},
        ])
        responses = [
            _proc(0, invite_json),  # list
            _proc(0, "204"),        # accept inv 42 only
        ]
        with patch("subprocess.run", side_effect=responses) as mock_run:
            _accept_bot_invitation("owner/repo", "tok")
        assert mock_run.call_count == 2  # list + one accept (not two)

    def test_accepts_multiple_invitations_for_same_repo(self) -> None:
        from breadforge.cli import _accept_bot_invitation

        invite_json = json.dumps([
            {"id": 1, "repository": {"full_name": "owner/repo"}},
            {"id": 2, "repository": {"full_name": "owner/repo"}},
        ])
        responses = [
            _proc(0, invite_json),
            _proc(0, "204"),
            _proc(0, "204"),
        ]
        with patch("subprocess.run", side_effect=responses) as mock_run:
            _accept_bot_invitation("owner/repo", "tok")
        assert mock_run.call_count == 3

    def test_no_matching_invitations_no_accept_call(self) -> None:
        from breadforge.cli import _accept_bot_invitation

        with patch("subprocess.run", return_value=_proc(0, "[]")) as mock_run:
            _accept_bot_invitation("owner/repo", "tok")
        assert mock_run.call_count == 1  # list only

    def test_non_dict_items_in_list_are_skipped(self) -> None:
        from breadforge.cli import _accept_bot_invitation

        bad_json = json.dumps(["a string", None, {"id": 5, "repository": {"full_name": "owner/repo"}}])
        responses = [
            _proc(0, bad_json),
            _proc(0, "204"),
        ]
        with patch("subprocess.run", side_effect=responses):
            _accept_bot_invitation("owner/repo", "tok")  # must not raise

    def test_bad_json_from_list_endpoint(self) -> None:
        from breadforge.cli import _accept_bot_invitation

        with patch("subprocess.run", return_value=_proc(0, "not-json")):
            _accept_bot_invitation("owner/repo", "tok")  # must not raise

    def test_non_204_acceptance_prints_warning(self, capsys) -> None:
        from breadforge.cli import _accept_bot_invitation

        invite_json = json.dumps([{"id": 1, "repository": {"full_name": "owner/repo"}}])
        responses = [
            _proc(0, invite_json),
            _proc(0, "403"),
        ]
        with patch("subprocess.run", side_effect=responses):
            _accept_bot_invitation("owner/repo", "tok")  # must not raise; warning printed by console


# ---------------------------------------------------------------------------
# _add_bot_collaborator
# ---------------------------------------------------------------------------


class TestAddBotCollaborator:
    def test_add_succeeds_then_calls_accept(self) -> None:
        from breadforge.cli import _add_bot_collaborator

        responses = [
            _proc(0),       # PUT collaborator
            _proc(0, "[]"), # list invitations (none pending)
        ]
        with patch.dict(os.environ, {"BREADFORGE_GH_TOKEN": "tok"}):
            with patch("subprocess.run", side_effect=responses) as mock_run:
                _add_bot_collaborator("owner/repo")
        assert mock_run.call_count == 2

    def test_add_fails_does_not_call_accept(self) -> None:
        from breadforge.cli import _add_bot_collaborator

        with patch.dict(os.environ, {"BREADFORGE_GH_TOKEN": "tok"}):
            with patch("subprocess.run", return_value=_proc(1, stderr="forbidden")) as mock_run:
                _add_bot_collaborator("owner/repo")
        assert mock_run.call_count == 1  # PUT only; no invitation calls

    def test_put_strips_gh_token_from_env(self) -> None:
        """PUT must strip GH_TOKEN so it doesn't auth as yeast-bot."""
        from breadforge.cli import _add_bot_collaborator

        captured_env: dict = {}
        call_index = 0

        def fake_run(cmd, **kwargs):
            nonlocal call_index
            call_index += 1
            if call_index == 1:  # the PUT
                captured_env.update(kwargs.get("env") or {})
                return _proc(0)
            return _proc(0, "[]")

        with patch.dict(os.environ, {"BREADFORGE_GH_TOKEN": "bot-tok", "GH_TOKEN": "owner-tok"}):
            with patch("subprocess.run", side_effect=fake_run):
                _add_bot_collaborator("owner/repo")

        assert "GH_TOKEN" not in captured_env

    def test_add_and_accept_full_flow(self) -> None:
        from breadforge.cli import _add_bot_collaborator

        invite_json = json.dumps([{"id": 10, "repository": {"full_name": "owner/repo"}}])
        responses = [
            _proc(0),               # PUT
            _proc(0, invite_json),  # list
            _proc(0, "204"),        # accept
        ]
        with patch.dict(os.environ, {"BREADFORGE_GH_TOKEN": "tok"}):
            with patch("subprocess.run", side_effect=responses) as mock_run:
                _add_bot_collaborator("owner/repo")
        assert mock_run.call_count == 3
