"""Claude Code subprocess runner.

Spawns headless Claude Code sessions as subprocesses for agent dispatch.
Captures stream-json output, enforces timeouts, and handles SIGTERM → SIGKILL.

This is the canonical runner module as specified in breadforge v0.3.0.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class RunResult:
    """Result of a single run_agent invocation.

    Attributes:
        exit_code: Subprocess exit code. 0 = success.
        stdout: Full stdout captured from the subprocess.
        stderr: Full stderr captured from the subprocess.
        duration_ms: Wall-clock duration in milliseconds.
        started_at: UTC timestamp when the run began.
        events: Parsed stream-json event objects from stdout.
        input_tokens: Input token count extracted from the result event, or None.
        output_tokens: Output token count extracted from the result event, or None.
        error_type: Classified terminal error, or None if run succeeded.
            Valid values: "rate_limit", "billing_error", "auth_failure", "error_max_turns".
    """

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    events: list[dict] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    error_type: str | None = None

    @property
    def success(self) -> bool:
        """True when the subprocess exited with code 0."""
        return self.exit_code == 0

    def find_event(self, event_type: str) -> dict | None:
        """Return the first stream-json event with the given type, or None."""
        for e in self.events:
            if e.get("type") == event_type:
                return e
        return None

    @property
    def cost_usd(self) -> float | None:
        """Estimated cost from the stream-json result event, or None if unavailable."""
        result = self.find_event("result")
        if result is None:
            return None
        # The stream-json result event uses "total_cost_usd"
        cost = result.get("total_cost_usd") or result.get("cost_usd")
        if cost is not None:
            return float(cost)
        return None


def _build_env(
    model: str,
    *,
    proxy_url: str | None = None,
    proxy_token: str | None = None,
) -> dict[str, str]:
    """Build subprocess environment — explicit allowlist, no credential leakage.

    When *proxy_url* and *proxy_token* are supplied the agent subprocess routes
    its Anthropic API calls through the loopback credential proxy instead of
    receiving the real API key directly.  Raw API keys for other services are
    withheld when the proxy is active so that the scoped token is the only
    credential available to the agent.
    """
    env: dict[str, str] = {}

    always_pass = (
        "HOME",
        "PATH",
        "SHELL",
        "USER",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "TERM",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "BREADMIN_DB_PATH",
        "BREADFORGE_MODEL",
    )
    for key in always_pass:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val

    if proxy_url and proxy_token:
        # Route the agent's Anthropic calls through the credential proxy.
        # Do NOT forward real API keys — the scoped token is the only credential.
        env["ANTHROPIC_BASE_URL"] = proxy_url
        env["ANTHROPIC_API_KEY"] = proxy_token
    else:
        # No proxy: forward real API keys from the orchestrator environment.
        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
            val = os.environ.get(key)
            if val is not None:
                env[key] = val

    env["BREADFORGE_MODEL"] = model
    env["BREADFORGE_AGENT"] = "1"

    # Prevent recursive orchestrator nesting
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    return env


def _classify_error(result_event: dict, stderr_text: str) -> str | None:
    """Classify the terminal error from a stream-json result event and stderr.

    Returns one of: "rate_limit", "billing_error", "auth_failure",
    "error_max_turns", or None if the error is unclassified.
    """
    subtype = result_event.get("subtype", "")
    if subtype == "error_max_turns":
        return "error_max_turns"

    # Check result text and stderr for known error patterns
    result_text = str(result_event.get("result", "")).lower()
    combined = result_text + stderr_text.lower()

    if any(kw in combined for kw in ("rate limit", "rate_limit", "429", "too many requests")):
        return "rate_limit"
    if any(kw in combined for kw in ("billing", "payment", "quota exceeded", "402")):
        return "billing_error"
    if any(kw in combined for kw in ("invalid api key", "authentication", "401", "auth_failure")):
        return "auth_failure"

    return None


async def _run_agent_once(
    prompt: str,
    *,
    model: str,
    timeout_minutes: int,
    cwd: Path | None,
    allowed_tools: list[str] | None,
    proxy_url: str | None,
    proxy_token: str | None,
) -> RunResult:
    """Internal helper: run one Claude Code subprocess and return a RunResult."""
    start = datetime.now(UTC)

    # Prompt must come before --allowedTools; otherwise the claude CLI
    # misparses the positional argument and raises "Input must be provided".
    cmd = [
        "claude",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
        "--print",
        prompt,
    ]

    if allowed_tools:
        cmd += ["--allowedTools", ",".join(allowed_tools)]

    env = _build_env(model, proxy_url=proxy_url, proxy_token=proxy_token)

    # limit=8MB — claude --print can emit large JSON lines (tool outputs, file reads)
    _8MB = 8 * 1024 * 1024
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        limit=_8MB,
    )

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    events: list[dict] = []

    async def read_stdout() -> None:
        assert proc.stdout
        async for line in proc.stdout:
            text = line.decode("utf-8", errors="replace").rstrip()
            stdout_chunks.append(text)
            if text.startswith("{"):
                with contextlib.suppress(json.JSONDecodeError):
                    events.append(json.loads(text))

    async def read_stderr() -> None:
        assert proc.stderr
        data = await proc.stderr.read()
        stderr_chunks.append(data.decode("utf-8", errors="replace"))

    timeout_seconds = timeout_minutes * 60

    try:
        await asyncio.wait_for(
            asyncio.gather(read_stdout(), read_stderr(), proc.wait()),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        try:
            proc.send_signal(signal.SIGTERM)
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (TimeoutError, ProcessLookupError):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        exit_code = -1
    else:
        exit_code = proc.returncode or 0

    end = datetime.now(UTC)
    duration_ms = (end - start).total_seconds() * 1000
    stderr_text = "\n".join(stderr_chunks)

    # Extract token counts and error type from the result event
    input_tokens: int | None = None
    output_tokens: int | None = None
    error_type: str | None = None

    result_event: dict | None = None
    for e in events:
        if e.get("type") == "result":
            result_event = e
            break

    if result_event is not None:
        usage = result_event.get("usage", {})
        raw_input = usage.get("input_tokens")
        raw_output = usage.get("output_tokens")
        if raw_input is not None:
            input_tokens = int(raw_input)
        if raw_output is not None:
            output_tokens = int(raw_output)
        if result_event.get("is_error"):
            error_type = _classify_error(result_event, stderr_text)

    return RunResult(
        exit_code=exit_code,
        stdout="\n".join(stdout_chunks),
        stderr=stderr_text,
        duration_ms=duration_ms,
        started_at=start,
        events=events,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        error_type=error_type,
    )


async def run_agent(
    prompt: str,
    *,
    model: str = "claude-sonnet-4-6",
    timeout_minutes: int = 60,
    cwd: Path | None = None,
    allowed_tools: list[str] | None = None,
    proxy_url: str | None = None,
    proxy_token: str | None = None,
    fallback_model: str | None = "claude-haiku-4-5-20251001",
) -> RunResult:
    """Spawn a headless Claude Code agent and wait for completion.

    Runs ``claude --output-format stream-json --print <prompt>`` as a subprocess,
    captures all output, parses stream-json events, and returns a :class:`RunResult`.

    When *proxy_url* and *proxy_token* are provided the subprocess routes its
    Anthropic API requests through the loopback credential proxy rather than
    using a raw API key.

    When *fallback_model* is set and the primary run hits a rate_limit or
    overload error, the agent is retried once with the fallback model.

    Args:
        prompt: The prompt to pass to the Claude Code subprocess.
        model: Primary model identifier.
        timeout_minutes: Hard timeout before SIGTERM → SIGKILL.
        cwd: Working directory for the subprocess. Defaults to caller's cwd.
        allowed_tools: Restrict the agent to this tool list. None = no restriction.
        proxy_url: Base URL of the credential proxy (e.g. ``http://127.0.0.1:9000``).
        proxy_token: Scoped token issued by the credential proxy.
        fallback_model: Model to retry with on rate_limit/overload. None = no retry.

    Returns:
        :class:`RunResult` with exit code, captured output, parsed events, token
        counts, cost estimate, and classified error type.
    """
    result = await _run_agent_once(
        prompt,
        model=model,
        timeout_minutes=timeout_minutes,
        cwd=cwd,
        allowed_tools=allowed_tools,
        proxy_url=proxy_url,
        proxy_token=proxy_token,
    )

    if fallback_model and result.error_type in {"rate_limit", "overload"}:
        print(f"warning: downgrading to fallback model {fallback_model}")  # noqa: T201
        result = await _run_agent_once(
            prompt,
            model=fallback_model,
            timeout_minutes=timeout_minutes,
            cwd=cwd,
            allowed_tools=allowed_tools,
            proxy_url=proxy_url,
            proxy_token=proxy_token,
        )

    return result
