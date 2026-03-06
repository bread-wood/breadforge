"""Unit tests for agents/runner.py.

Tests RunResult, _build_env, _classify_error, _run_agent_once, and run_agent.
All subprocess interaction is mocked; no live claude process is spawned.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Load agents/runner.py directly to avoid package install requirement
_runner_path = Path(__file__).parent / "runner.py"
_spec = importlib.util.spec_from_file_location("agents.runner", _runner_path)
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
sys.modules["agents.runner"] = _module
_spec.loader.exec_module(_module)  # type: ignore[union-attr]

RunResult = _module.RunResult
_build_env = _module._build_env
_classify_error = _module._classify_error
_run_agent_once = _module._run_agent_once
run_agent = _module.run_agent


# ---------------------------------------------------------------------------
# RunResult
# ---------------------------------------------------------------------------


class TestRunResult:
    def test_success_true_on_zero_exit(self):
        r = RunResult(exit_code=0, stdout="", stderr="", duration_ms=100.0)
        assert r.success is True

    def test_success_false_on_nonzero_exit(self):
        r = RunResult(exit_code=1, stdout="", stderr="", duration_ms=100.0)
        assert r.success is False

    def test_success_false_on_negative_exit(self):
        # -1 is used for timeout
        r = RunResult(exit_code=-1, stdout="", stderr="", duration_ms=100.0)
        assert r.success is False

    def test_find_event_returns_first_match(self):
        events = [
            {"type": "system", "data": "hello"},
            {"type": "result", "total_cost_usd": 0.05},
            {"type": "result", "total_cost_usd": 0.10},
        ]
        r = RunResult(exit_code=0, stdout="", stderr="", duration_ms=10.0, events=events)
        found = r.find_event("result")
        assert found is not None
        assert found["total_cost_usd"] == 0.05

    def test_find_event_returns_none_when_missing(self):
        r = RunResult(exit_code=0, stdout="", stderr="", duration_ms=10.0)
        assert r.find_event("result") is None

    def test_cost_usd_from_total_cost_usd(self):
        events = [{"type": "result", "total_cost_usd": 0.0123}]
        r = RunResult(exit_code=0, stdout="", stderr="", duration_ms=10.0, events=events)
        assert r.cost_usd == pytest.approx(0.0123)

    def test_cost_usd_fallback_to_cost_usd_field(self):
        events = [{"type": "result", "cost_usd": 0.0042}]
        r = RunResult(exit_code=0, stdout="", stderr="", duration_ms=10.0, events=events)
        assert r.cost_usd == pytest.approx(0.0042)

    def test_cost_usd_none_when_no_result_event(self):
        r = RunResult(exit_code=0, stdout="", stderr="", duration_ms=10.0)
        assert r.cost_usd is None

    def test_cost_usd_none_when_result_has_no_cost(self):
        events = [{"type": "result", "subtype": "success"}]
        r = RunResult(exit_code=0, stdout="", stderr="", duration_ms=10.0, events=events)
        assert r.cost_usd is None

    def test_default_started_at_is_utc(self):
        before = datetime.now(UTC)
        r = RunResult(exit_code=0, stdout="", stderr="", duration_ms=0.0)
        after = datetime.now(UTC)
        assert before <= r.started_at <= after

    def test_token_fields_default_none(self):
        r = RunResult(exit_code=0, stdout="", stderr="", duration_ms=0.0)
        assert r.input_tokens is None
        assert r.output_tokens is None

    def test_error_type_default_none(self):
        r = RunResult(exit_code=0, stdout="", stderr="", duration_ms=0.0)
        assert r.error_type is None

    def test_token_fields_stored(self):
        r = RunResult(
            exit_code=0,
            stdout="",
            stderr="",
            duration_ms=0.0,
            input_tokens=1234,
            output_tokens=567,
        )
        assert r.input_tokens == 1234
        assert r.output_tokens == 567


# ---------------------------------------------------------------------------
# _build_env
# ---------------------------------------------------------------------------


class TestBuildEnv:
    def test_sets_model_and_agent_flag(self):
        env = _build_env("claude-sonnet-4-6")
        assert env["BREADFORGE_MODEL"] == "claude-sonnet-4-6"
        assert env["BREADFORGE_AGENT"] == "1"

    def test_no_raw_keys_when_proxy_provided(self):
        env = _build_env(
            "claude-sonnet-4-6",
            proxy_url="http://127.0.0.1:9000",
            proxy_token="tok_abc",
        )
        assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9000"
        assert env["ANTHROPIC_API_KEY"] == "tok_abc"
        assert "OPENAI_API_KEY" not in env
        assert "GOOGLE_API_KEY" not in env

    def test_real_keys_forwarded_without_proxy(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real")
        monkeypatch.setenv("OPENAI_API_KEY", "oai-real")
        env = _build_env("claude-sonnet-4-6")
        assert env["ANTHROPIC_API_KEY"] == "sk-real"
        assert env["OPENAI_API_KEY"] == "oai-real"

    def test_orchestrator_vars_removed(self, monkeypatch):
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("CLAUDE_CODE", "1")
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
        env = _build_env("claude-sonnet-4-6")
        assert "CLAUDECODE" not in env
        assert "CLAUDE_CODE" not in env
        assert "CLAUDE_CODE_ENTRYPOINT" not in env

    def test_always_pass_keys_forwarded_when_set(self, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "gh_token_value")
        monkeypatch.setenv("HOME", "/home/user")
        env = _build_env("claude-sonnet-4-6")
        assert env["GH_TOKEN"] == "gh_token_value"
        assert env["HOME"] == "/home/user"

    def test_optional_keys_not_set_when_absent(self, monkeypatch):
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        env = _build_env("claude-sonnet-4-6")
        assert "GH_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env

    def test_proxy_url_only_no_token_falls_back_to_real_keys(self, monkeypatch):
        """Proxy URL alone (no token) should not set proxy — both required."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real")
        env = _build_env("claude-sonnet-4-6", proxy_url="http://127.0.0.1:9000")
        # Without proxy_token the condition is falsy — real key forwarded
        assert env.get("ANTHROPIC_API_KEY") == "sk-real"
        assert "ANTHROPIC_BASE_URL" not in env

    def test_model_override_always_applied(self):
        env = _build_env("claude-haiku-4-5-20251001")
        assert env["BREADFORGE_MODEL"] == "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# _classify_error
# ---------------------------------------------------------------------------


class TestClassifyError:
    def test_error_max_turns_from_subtype(self):
        assert _classify_error({"subtype": "error_max_turns"}, "") == "error_max_turns"

    def test_rate_limit_from_result_text(self):
        event = {"result": "rate limit exceeded"}
        assert _classify_error(event, "") == "rate_limit"

    def test_rate_limit_from_429_in_stderr(self):
        assert _classify_error({}, "error 429 too many requests") == "rate_limit"

    def test_rate_limit_keyword_too_many_requests(self):
        assert _classify_error({"result": "too many requests"}, "") == "rate_limit"

    def test_billing_error_from_billing_keyword(self):
        assert _classify_error({"result": "billing issue"}, "") == "billing_error"

    def test_billing_error_from_payment_keyword(self):
        assert _classify_error({}, "payment required") == "billing_error"

    def test_billing_error_from_402_in_stderr(self):
        assert _classify_error({}, "402 payment required") == "billing_error"

    def test_billing_error_from_quota_exceeded(self):
        assert _classify_error({"result": "quota exceeded"}, "") == "billing_error"

    def test_auth_failure_from_invalid_api_key(self):
        assert _classify_error({"result": "invalid api key"}, "") == "auth_failure"

    def test_auth_failure_from_401_in_stderr(self):
        assert _classify_error({}, "401 unauthorized") == "auth_failure"

    def test_auth_failure_from_authentication_keyword(self):
        assert _classify_error({"result": "authentication failed"}, "") == "auth_failure"

    def test_none_for_unrecognised_error(self):
        assert _classify_error({"result": "unknown error"}, "some stderr") is None

    def test_none_for_empty_event(self):
        assert _classify_error({}, "") is None

    def test_case_insensitive_matching(self):
        assert _classify_error({"result": "RATE LIMIT"}, "") == "rate_limit"
        assert _classify_error({}, "BILLING ISSUE") == "billing_error"


# ---------------------------------------------------------------------------
# _run_agent_once (mocked subprocess)
# ---------------------------------------------------------------------------


def _make_mock_proc(
    stdout_lines: list[str],
    stderr_bytes: bytes = b"",
    returncode: int = 0,
) -> MagicMock:
    """Build a mock asyncio subprocess with the given output."""
    proc = MagicMock()
    proc.returncode = returncode

    # stdout: async iterator over encoded lines
    async def stdout_iter():
        for line in stdout_lines:
            yield (line + "\n").encode()

    proc.stdout = stdout_iter()

    # stderr: reads all at once
    proc.stderr = AsyncMock()
    proc.stderr.read = AsyncMock(return_value=stderr_bytes)

    # wait() coroutine
    async def wait():
        return returncode

    proc.wait = wait
    proc.send_signal = MagicMock()
    proc.kill = MagicMock()
    return proc


@pytest.mark.asyncio
class TestRunAgentOnce:
    async def test_success_result(self):
        result_event = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "total_cost_usd": 0.01,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
        lines = [json.dumps(result_event)]
        mock_proc = _make_mock_proc(lines, returncode=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await _run_agent_once(
                "do something",
                model="claude-sonnet-4-6",
                timeout_minutes=5,
                cwd=None,
                allowed_tools=None,
                proxy_url=None,
                proxy_token=None,
            )

        assert result.success
        assert result.exit_code == 0
        assert result.input_tokens == 100
        assert result.output_tokens == 50
        assert result.cost_usd == pytest.approx(0.01)
        assert result.error_type is None

    async def test_non_json_lines_ignored(self):
        lines = ["not json at all", "also not json"]
        mock_proc = _make_mock_proc(lines, returncode=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await _run_agent_once(
                "prompt",
                model="claude-sonnet-4-6",
                timeout_minutes=5,
                cwd=None,
                allowed_tools=None,
                proxy_url=None,
                proxy_token=None,
            )

        assert result.events == []

    async def test_error_type_classified_from_result_event(self):
        result_event = {
            "type": "result",
            "is_error": True,
            "result": "rate limit exceeded",
            "usage": {},
        }
        lines = [json.dumps(result_event)]
        mock_proc = _make_mock_proc(lines, returncode=1)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await _run_agent_once(
                "prompt",
                model="claude-sonnet-4-6",
                timeout_minutes=5,
                cwd=None,
                allowed_tools=None,
                proxy_url=None,
                proxy_token=None,
            )

        assert result.error_type == "rate_limit"
        assert not result.success

    async def test_timeout_sets_exit_code_minus_one(self):
        async def slow_wait():
            await asyncio.sleep(999)

        mock_proc = _make_mock_proc([], returncode=0)
        mock_proc.wait = slow_wait

        # Make asyncio.wait_for raise TimeoutError
        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("asyncio.wait_for", side_effect=TimeoutError),
        ):
            result = await _run_agent_once(
                "prompt",
                model="claude-sonnet-4-6",
                timeout_minutes=1,
                cwd=None,
                allowed_tools=None,
                proxy_url=None,
                proxy_token=None,
            )

        assert result.exit_code == -1

    async def test_allowed_tools_passed_to_cmd(self):
        mock_proc = _make_mock_proc([], returncode=0)
        captured_cmd = []

        async def fake_exec(*args, **kwargs):
            captured_cmd.extend(args)
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await _run_agent_once(
                "prompt",
                model="claude-sonnet-4-6",
                timeout_minutes=5,
                cwd=None,
                allowed_tools=["Bash", "Read"],
                proxy_url=None,
                proxy_token=None,
            )

        cmd_str = " ".join(captured_cmd)
        assert "--allowedTools" in cmd_str
        assert "Bash,Read" in cmd_str

    async def test_no_allowed_tools_flag_when_none(self):
        mock_proc = _make_mock_proc([], returncode=0)
        captured_cmd = []

        async def fake_exec(*args, **kwargs):
            captured_cmd.extend(args)
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await _run_agent_once(
                "prompt",
                model="claude-sonnet-4-6",
                timeout_minutes=5,
                cwd=None,
                allowed_tools=None,
                proxy_url=None,
                proxy_token=None,
            )

        assert "--allowedTools" not in captured_cmd

    async def test_stderr_captured(self):
        mock_proc = _make_mock_proc([], stderr_bytes=b"some warning\n", returncode=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await _run_agent_once(
                "prompt",
                model="claude-sonnet-4-6",
                timeout_minutes=5,
                cwd=None,
                allowed_tools=None,
                proxy_url=None,
                proxy_token=None,
            )

        assert "some warning" in result.stderr

    async def test_duration_ms_positive(self):
        mock_proc = _make_mock_proc([], returncode=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await _run_agent_once(
                "prompt",
                model="claude-sonnet-4-6",
                timeout_minutes=5,
                cwd=None,
                allowed_tools=None,
                proxy_url=None,
                proxy_token=None,
            )

        assert result.duration_ms >= 0

    async def test_no_error_when_is_error_false(self):
        result_event = {
            "type": "result",
            "is_error": False,
            "result": "some output",
            "usage": {},
        }
        lines = [json.dumps(result_event)]
        mock_proc = _make_mock_proc(lines, returncode=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await _run_agent_once(
                "prompt",
                model="claude-sonnet-4-6",
                timeout_minutes=5,
                cwd=None,
                allowed_tools=None,
                proxy_url=None,
                proxy_token=None,
            )

        assert result.error_type is None

    async def test_cwd_passed_to_subprocess(self):
        mock_proc = _make_mock_proc([], returncode=0)
        captured_kwargs: dict = {}

        async def fake_exec(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return mock_proc

        test_cwd = Path("/tmp/test-cwd")
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await _run_agent_once(
                "prompt",
                model="claude-sonnet-4-6",
                timeout_minutes=5,
                cwd=test_cwd,
                allowed_tools=None,
                proxy_url=None,
                proxy_token=None,
            )

        assert captured_kwargs.get("cwd") == test_cwd


# ---------------------------------------------------------------------------
# run_agent (fallback logic)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRunAgent:
    async def test_returns_primary_result_on_success(self):
        ok_result = RunResult(exit_code=0, stdout="ok", stderr="", duration_ms=10.0)

        with patch.object(_module, "_run_agent_once", return_value=ok_result) as mock_once:
            result = await run_agent("prompt")

        mock_once.assert_called_once()
        assert result is ok_result

    async def test_fallback_on_rate_limit(self):
        rate_limited = RunResult(
            exit_code=1, stdout="", stderr="", duration_ms=5.0, error_type="rate_limit"
        )
        fallback_ok = RunResult(exit_code=0, stdout="ok", stderr="", duration_ms=8.0)

        call_count = 0
        results = [rate_limited, fallback_ok]

        async def fake_once(prompt, *, model, **kwargs):
            nonlocal call_count
            r = results[call_count]
            call_count += 1
            return r

        with patch.object(_module, "_run_agent_once", side_effect=fake_once):
            result = await run_agent("prompt", fallback_model="claude-haiku-4-5-20251001")

        assert call_count == 2
        assert result is fallback_ok

    async def test_no_fallback_when_fallback_model_none(self):
        rate_limited = RunResult(
            exit_code=1, stdout="", stderr="", duration_ms=5.0, error_type="rate_limit"
        )

        with patch.object(_module, "_run_agent_once", return_value=rate_limited) as mock_once:
            result = await run_agent("prompt", fallback_model=None)

        mock_once.assert_called_once()
        assert result is rate_limited

    async def test_no_fallback_on_auth_failure(self):
        """Non-retryable errors should not trigger fallback."""
        auth_failed = RunResult(
            exit_code=1, stdout="", stderr="", duration_ms=5.0, error_type="auth_failure"
        )

        with patch.object(_module, "_run_agent_once", return_value=auth_failed) as mock_once:
            result = await run_agent("prompt", fallback_model="claude-haiku-4-5-20251001")

        mock_once.assert_called_once()
        assert result is auth_failed

    async def test_no_fallback_on_billing_error(self):
        billing = RunResult(
            exit_code=1, stdout="", stderr="", duration_ms=5.0, error_type="billing_error"
        )

        with patch.object(_module, "_run_agent_once", return_value=billing) as mock_once:
            result = await run_agent("prompt", fallback_model="claude-haiku-4-5-20251001")

        mock_once.assert_called_once()
        assert result is billing

    async def test_fallback_uses_fallback_model(self):
        rate_limited = RunResult(
            exit_code=1, stdout="", stderr="", duration_ms=5.0, error_type="rate_limit"
        )
        fallback_ok = RunResult(exit_code=0, stdout="ok", stderr="", duration_ms=8.0)

        calls: list[str] = []
        results = [rate_limited, fallback_ok]

        async def fake_once(prompt, *, model, **kwargs):
            calls.append(model)
            return results[len(calls) - 1]

        with patch.object(_module, "_run_agent_once", side_effect=fake_once):
            await run_agent(
                "prompt",
                model="claude-sonnet-4-6",
                fallback_model="claude-haiku-4-5-20251001",
            )

        assert calls[0] == "claude-sonnet-4-6"
        assert calls[1] == "claude-haiku-4-5-20251001"

    async def test_default_model_is_sonnet(self):
        ok_result = RunResult(exit_code=0, stdout="", stderr="", duration_ms=5.0)
        calls: list[str] = []

        async def fake_once(prompt, *, model, **kwargs):
            calls.append(model)
            return ok_result

        with patch.object(_module, "_run_agent_once", side_effect=fake_once):
            await run_agent("prompt")

        assert calls[0] == "claude-sonnet-4-6"

    async def test_overload_error_triggers_fallback(self):
        overloaded = RunResult(
            exit_code=1, stdout="", stderr="", duration_ms=5.0, error_type="overload"
        )
        fallback_ok = RunResult(exit_code=0, stdout="ok", stderr="", duration_ms=8.0)

        call_count = 0
        results = [overloaded, fallback_ok]

        async def fake_once(prompt, *, model, **kwargs):
            nonlocal call_count
            r = results[call_count]
            call_count += 1
            return r

        with patch.object(_module, "_run_agent_once", side_effect=fake_once):
            result = await run_agent("prompt", fallback_model="claude-haiku-4-5-20251001")

        assert call_count == 2
        assert result is fallback_ok
