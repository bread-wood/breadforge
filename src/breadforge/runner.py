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


def _build_env(model: str) -> dict[str, str]:
    """Build subprocess environment — explicit allowlist, no credential leakage."""
    env: dict[str, str] = {}

    # Pass through essential env vars
    for key in (
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
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "BREADMIN_DB_PATH",
        "BREADFORGE_MODEL",
    ):
        val = os.environ.get(key)
        if val is not None:
            env[key] = val

    # Set the model for this agent
    env["BREADFORGE_MODEL"] = model

    # Prevent recursive orchestrator nesting
    env["BREADFORGE_AGENT"] = "1"

    return env


async def run_agent(
    prompt: str,
    *,
    model: str = "claude-sonnet-4-6",
    timeout_minutes: int = 60,
    cwd: Path | None = None,
    allowed_tools: list[str] | None = None,
) -> RunResult:
    """Spawn a headless Claude Code agent and wait for completion."""
    start = datetime.now(UTC)

    cmd = ["claude", "--output-format", "stream-json", "--model", model, "--print"]

    if allowed_tools:
        cmd += ["--allowedTools", ",".join(allowed_tools)]

    # Append the prompt
    cmd += [prompt]

    env = _build_env(model)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    events: list[dict] = []

    async def read_stdout() -> None:
        assert proc.stdout
        async for line in proc.stdout:
            text = line.decode("utf-8", errors="replace").rstrip()
            stdout_chunks.append(text)
            # Parse stream-json events
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
        # SIGTERM → wait 5s → SIGKILL
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


def build_agent_prompt(
    issue_number: int,
    issue_title: str,
    issue_body: str,
    branch: str,
    repo: str,
    allowed_scope: list[str] | None = None,
) -> str:
    """Build the standard sub-agent prompt."""
    scope_note = ""
    if allowed_scope:
        scope_note = f"\nAllowed scope (only modify files within): {', '.join(allowed_scope)}"

    return f"""You are implementing GitHub issue #{issue_number} in repo `{repo}` on branch `{branch}`.

Issue: {issue_title}

{issue_body}
{scope_note}

Steps:
1. Clone the repo and create your branch:
   ```
   gh repo clone {repo} .
   git checkout -b {branch}
   git push -u origin {branch}
   ```
2. Read the full issue: `gh issue view {issue_number} --repo {repo}`
3. Before writing any code, reason through the approach, identify constraints, and plan the module breakdown.
4. Implement the changes.{" Only modify files within: " + ", ".join(allowed_scope) if allowed_scope else ""}
5. Run tests — all must pass.
6. Run lint — must be clean.
7. Commit referencing the issue: `git commit -m "feat: <description> (closes #{issue_number})"`
8. `git push`
9. Create PR: `gh pr create --repo {repo} --title "<title>" --body "Closes #{issue_number}"`
10. Watch CI: `gh pr checks <PR-number> --watch`
11. Read feedback: `gh pr view <PR-number> --json reviews,comments`
    Inline comments: `gh api repos/{repo}/pulls/<PR-number>/comments`
12. Triage feedback:
    - Fix now: in scope and clear → fix, push, re-check
    - File issue: valid but out of scope → `gh issue create --repo {repo}`
    - Skip: false positive → note in PR comment
13. STOP. Do not merge.
"""
