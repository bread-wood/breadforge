"""Claude Code subprocess runner.

Spawns headless Claude Code sessions as subprocesses for agent dispatch.
Captures stream-json output, enforces timeouts, and handles SIGTERM → SIGKILL.
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
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    events: list[dict] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    def find_event(self, event_type: str) -> dict | None:
        for e in self.events:
            if e.get("type") == event_type:
                return e
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


async def run_agent(
    prompt: str,
    *,
    model: str = "claude-sonnet-4-6",
    timeout_minutes: int = 60,
    cwd: Path | None = None,
    allowed_tools: list[str] | None = None,
    proxy_url: str | None = None,
    proxy_token: str | None = None,
) -> RunResult:
    """Spawn a headless Claude Code agent and wait for completion.

    When *proxy_url* and *proxy_token* are provided the subprocess routes its
    Anthropic API requests through the loopback credential proxy rather than
    using a raw API key.
    """
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

    return RunResult(
        exit_code=exit_code,
        stdout="\n".join(stdout_chunks),
        stderr="\n".join(stderr_chunks),
        duration_ms=duration_ms,
        started_at=start,
        events=events,
    )
