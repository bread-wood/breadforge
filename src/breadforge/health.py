"""Preflight health checks."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum


class CheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"


@dataclass
class CheckResult:
    name: str
    status: CheckStatus
    message: str


@dataclass
class HealthReport:
    checks: list[CheckResult]

    @property
    def healthy(self) -> bool:
        return all(c.status != CheckStatus.FAIL for c in self.checks)

    @property
    def fatal(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == CheckStatus.FAIL]

    @property
    def warnings(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == CheckStatus.WARN]


def run_health_checks(repo: str) -> HealthReport:
    checks: list[CheckResult] = []

    # 1. Claude Code CLI present
    if shutil.which("claude"):
        checks.append(CheckResult("claude-cli", CheckStatus.PASS, "claude found in PATH"))
    else:
        checks.append(CheckResult("claude-cli", CheckStatus.FAIL, "claude not found in PATH"))

    # 2. gh CLI present and authenticated
    if shutil.which("gh"):
        try:
            result = subprocess.run(
                ["gh", "auth", "status"], capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                checks.append(CheckResult("gh-auth", CheckStatus.PASS, "gh authenticated"))
            else:
                checks.append(
                    CheckResult(
                        "gh-auth",
                        CheckStatus.FAIL,
                        f"gh not authenticated: {result.stderr.strip()}",
                    )
                )
        except subprocess.TimeoutExpired:
            checks.append(CheckResult("gh-auth", CheckStatus.WARN, "gh auth check timed out"))
    else:
        checks.append(CheckResult("gh-cli", CheckStatus.FAIL, "gh not found in PATH"))

    # 3. git present
    if shutil.which("git"):
        checks.append(CheckResult("git", CheckStatus.PASS, "git found in PATH"))
    else:
        checks.append(CheckResult("git", CheckStatus.FAIL, "git not found in PATH"))

    # 4. Repo accessible
    if repo:
        try:
            result = subprocess.run(
                ["gh", "repo", "view", repo, "--json", "name"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                checks.append(CheckResult("repo-access", CheckStatus.PASS, f"{repo} accessible"))
            else:
                checks.append(
                    CheckResult(
                        "repo-access",
                        CheckStatus.FAIL,
                        f"cannot access {repo}: {result.stderr.strip()}",
                    )
                )
        except subprocess.TimeoutExpired:
            checks.append(
                CheckResult("repo-access", CheckStatus.WARN, "repo access check timed out")
            )

    # 5. ANTHROPIC_API_KEY set
    import os

    if os.environ.get("ANTHROPIC_API_KEY"):
        checks.append(CheckResult("anthropic-key", CheckStatus.PASS, "ANTHROPIC_API_KEY is set"))
    else:
        checks.append(
            CheckResult(
                "anthropic-key",
                CheckStatus.WARN,
                "ANTHROPIC_API_KEY not set (claude CLI may use its own auth)",
            )
        )

    # 6. Not running inside Claude Code (nesting guard)
    if os.environ.get("BREADFORGE_AGENT") == "1":
        checks.append(
            CheckResult(
                "nesting-guard",
                CheckStatus.FAIL,
                "breadforge orchestrator cannot run inside a breadforge agent",
            )
        )
    elif os.environ.get("CLAUDE_CODE"):
        checks.append(
            CheckResult(
                "nesting-guard",
                CheckStatus.WARN,
                "running inside Claude Code — ensure this is intentional",
            )
        )
    else:
        checks.append(CheckResult("nesting-guard", CheckStatus.PASS, "not nested in agent"))

    return HealthReport(checks=checks)
