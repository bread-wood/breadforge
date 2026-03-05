"""Preflight health checks."""

from __future__ import annotations

import json
import os
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


def _check_bot_collaborator(repo: str, bot_token: str) -> CheckResult:
    """Check that yeast-bot is an active collaborator; auto-add and accept invitation if not.

    Runs the PUT collaborator call as the repo owner (ambient gh credentials),
    then accepts the invitation as yeast-bot using *bot_token*.
    """
    # Check current status
    check = subprocess.run(
        ["gh", "api", f"repos/{repo}/collaborators/yeast-bot", "--silent"],
        capture_output=True,
        text=True,
    )
    if check.returncode == 0:
        return CheckResult(
            "bot-collaborator", CheckStatus.PASS, f"yeast-bot is a collaborator on {repo}"
        )

    # Not yet a collaborator — add using owner's ambient gh auth (no GH_TOKEN override)
    env = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}
    add = subprocess.run(
        ["gh", "api", f"repos/{repo}/collaborators/yeast-bot", "-X", "PUT", "-f", "permission=push"],
        capture_output=True,
        text=True,
        env=env,
    )
    if add.returncode != 0:
        return CheckResult(
            "bot-collaborator",
            CheckStatus.FAIL,
            f"could not add yeast-bot to {repo}: {add.stderr.strip()} — "
            f"run: gh api repos/{repo}/collaborators/yeast-bot -X PUT -f permission=push",
        )

    # Accept the pending invitation as yeast-bot
    list_r = subprocess.run(
        ["curl", "-s", "-H", f"Authorization: token {bot_token}",
         "https://api.github.com/user/repository_invitations"],
        capture_output=True,
        text=True,
    )
    try:
        invitations = json.loads(list_r.stdout)
        matching = [
            inv["id"] for inv in invitations
            if isinstance(inv, dict) and inv.get("repository", {}).get("full_name", "") == repo
        ]
    except (json.JSONDecodeError, KeyError, TypeError):
        matching = []

    for inv_id in matching:
        subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-X", "PATCH",
             "-H", f"Authorization: token {bot_token}",
             f"https://api.github.com/user/repository_invitations/{inv_id}"],
            capture_output=True,
            text=True,
        )

    return CheckResult(
        "bot-collaborator",
        CheckStatus.PASS,
        f"added yeast-bot as collaborator on {repo} and accepted {len(matching)} invitation(s)",
    )


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

    # 6. Credential proxy secret
    if os.environ.get("BREADFORGE_PROXY_SECRET"):
        checks.append(
            CheckResult("proxy-secret", CheckStatus.PASS, "BREADFORGE_PROXY_SECRET is set")
        )
    else:
        checks.append(
            CheckResult(
                "proxy-secret",
                CheckStatus.WARN,
                "BREADFORGE_PROXY_SECRET not set — proxy will use an ephemeral per-session key",
            )
        )

    # 7. Not running inside Claude Code (nesting guard)
    # 5. BREADFORGE_GH_TOKEN present (required for yeast-bot operations)
    bot_token = os.environ.get("BREADFORGE_GH_TOKEN") or ""
    if bot_token:
        checks.append(
            CheckResult("bot-token", CheckStatus.PASS, "BREADFORGE_GH_TOKEN is set")
        )
    else:
        checks.append(
            CheckResult(
                "bot-token",
                CheckStatus.FAIL,
                "BREADFORGE_GH_TOKEN is not set. "
                "Set it to yeast-bot's GitHub token (repo + workflow scopes) "
                "so build agents can authenticate to GitHub as the service account.",
            )
        )

    # 6. yeast-bot is repo collaborator (auto-adds and accepts invitation if missing)
    if repo and bot_token:
        checks.append(_check_bot_collaborator(repo, bot_token))

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
