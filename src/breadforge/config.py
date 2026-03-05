"""Config and platform repo registry."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

# ---------------------------------------------------------------------------
# Per-run config
# ---------------------------------------------------------------------------


@dataclass
class Config:
    """Runtime configuration for a breadforge session."""

    repo: str  # owner/repo
    concurrency: int = 3
    model: str = "claude-sonnet-4-6"
    agent_timeout_minutes: int = 60
    watchdog_interval_seconds: int = 60
    max_retries: int = 3
    beads_dir: Path = field(default_factory=lambda: Path.home() / ".breadforge" / "beads")

    # yeast-bot GitHub token — forwarded to build agents as GH_TOKEN so they
    # authenticate to GitHub as the service account rather than the operator.
    github_token: str | None = None

    # Multi-backend routing
    # research nodes default to the anthropic subprocess path (run_agent);
    # set to "gemini" or "openai" to use the SDK-based backend directly.
    research_backend: str = "anthropic"
    plan_backend: str = "anthropic"
    # Optional model overrides for research/plan backends (uses backend default if None).
    research_model: str | None = None
    plan_model: str | None = None

    @classmethod
    def from_env(cls, repo: str) -> Config:
        return cls(
            repo=repo,
            concurrency=int(os.environ.get("BREADFORGE_CONCURRENCY", "3")),
            model=os.environ.get("BREADFORGE_MODEL", "claude-sonnet-4-6"),
            agent_timeout_minutes=int(os.environ.get("BREADFORGE_AGENT_TIMEOUT_MINUTES", "60")),
            watchdog_interval_seconds=int(
                os.environ.get("BREADFORGE_WATCHDOG_INTERVAL_SECONDS", "60")
            ),
            max_retries=int(os.environ.get("BREADFORGE_MAX_RETRIES", "3")),
            beads_dir=Path(
                os.environ.get(
                    "BREADFORGE_BEADS_DIR",
                    str(Path.home() / ".breadforge" / "beads"),
                )
            ),
            github_token=os.environ.get("BREADFORGE_GH_TOKEN") or None,
            research_backend=os.environ.get("BREADFORGE_RESEARCH_BACKEND", "anthropic"),
            plan_backend=os.environ.get("BREADFORGE_PLAN_BACKEND", "anthropic"),
            research_model=os.environ.get("BREADFORGE_RESEARCH_MODEL") or None,
            plan_model=os.environ.get("BREADFORGE_PLAN_MODEL") or None,
        )


# ---------------------------------------------------------------------------
# Platform repo registry
# ---------------------------------------------------------------------------

_REGISTRY_PATH = Path.home() / ".breadforge" / "breadforge.toml"


@dataclass
class RepoEntry:
    """A registered repo in the platform registry."""

    repo: str  # owner/repo
    local_path: Path
    spec_dir: Path  # path to spec directory within the repo
    default_branch: str = "mainline"

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "local_path": str(self.local_path),
            "spec_dir": str(self.spec_dir),
            "default_branch": self.default_branch,
        }

    @classmethod
    def from_dict(cls, d: dict) -> RepoEntry:
        return cls(
            repo=d["repo"],
            local_path=Path(d["local_path"]),
            spec_dir=Path(d["spec_dir"]),
            default_branch=d.get("default_branch", "mainline"),
        )


class Registry:
    """Platform repo registry backed by ~/.breadforge/breadforge.toml."""

    def __init__(self, path: Path = _REGISTRY_PATH) -> None:
        self._path = path
        self._repos: dict[str, RepoEntry] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        with open(self._path, "rb") as f:
            data = tomllib.load(f)
        for entry in data.get("repos", []):
            r = RepoEntry.from_dict(entry)
            self._repos[r.repo] = r

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {"repos": [r.to_dict() for r in self._repos.values()]}
        with open(self._path, "wb") as f:
            tomli_w.dump(data, f)

    def add(self, entry: RepoEntry) -> None:
        self._repos[entry.repo] = entry
        self._save()

    def remove(self, repo: str) -> bool:
        if repo not in self._repos:
            return False
        del self._repos[repo]
        self._save()
        return True

    def get(self, repo: str) -> RepoEntry | None:
        return self._repos.get(repo)

    def list(self) -> list[RepoEntry]:
        return list(self._repos.values())

    def spec_dir_for(self, repo: str) -> Path | None:
        entry = self.get(repo)
        return entry.spec_dir if entry else None
