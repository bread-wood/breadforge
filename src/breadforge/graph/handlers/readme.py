"""ReadmeHandler — generates a project README after all build/merge nodes complete.

Uses run_agent with Read/Write/Bash tools to write README.md and open a PR.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from breadforge.agents.runner import run_agent
from breadforge.beads.types import GraphNode
from breadforge.graph.node import NodeResult

if TYPE_CHECKING:
    from breadforge.beads.store import BeadStore
    from breadforge.config import Config
    from breadforge.logger import Logger


def _readme_prompt(repo: str, milestone: str, plan_artifact: dict) -> str:
    approach = plan_artifact.get("approach", "")
    modules = plan_artifact.get("modules", [])
    files_per_module = plan_artifact.get("files_per_module", {})

    module_lines = []
    for mod in modules:
        files = files_per_module.get(mod, [])
        module_lines.append(f"- **{mod}**: {', '.join(files)}")
    modules_text = "\n".join(module_lines)

    return f"""You are writing the README.md for the GitHub repo `{repo}` after a completed implementation milestone: `{milestone}`.

Implementation summary:
{approach}

Modules and files:
{modules_text}

Steps:
1. Clone the repo: `gh repo clone {repo} .`
2. Read the existing source files to understand what was built.
3. Write a clear, concise README.md at the repo root. Include:
   - Project name and one-line description
   - What it does (2-3 sentences)
   - How to install / run (based on pyproject.toml if present)
   - Module overview (one line per module)
   - How to run tests
4. Create a branch: `git checkout -b docs/readme`
5. `git add README.md && git commit -m "docs: add README"`
6. `git push -u origin docs/readme`
7. `gh pr create --repo {repo} --title "docs: add README" --body "Auto-generated README for {milestone}"`
8. Wait for CI: `gh pr checks <PR-number> --watch --repo {repo}`
9. Squash merge: `gh pr merge <PR-number> --repo {repo} --squash --delete-branch`
"""


class ReadmeHandler:
    """Generates a README.md for the repo and opens a PR."""

    def __init__(
        self,
        store: BeadStore | None = None,
        logger: Logger | None = None,
    ) -> None:
        self._store = store
        self._logger = logger

    async def execute(self, node: GraphNode, config: Config) -> NodeResult:
        repo = config.repo
        milestone = node.context.get("milestone", "")
        plan_artifact = node.context.get("plan_artifact", {})

        prompt = _readme_prompt(repo, milestone, plan_artifact)
        workspace = Path(tempfile.mkdtemp(prefix=f"breadforge-readme-{milestone}-"))

        result = await run_agent(
            prompt,
            model=config.model,
            timeout_minutes=15,
            cwd=workspace,
            allowed_tools=["Bash", "Read", "Write", "Glob"],
        )

        if not result.success:
            return NodeResult(
                success=False,
                error=f"readme agent exit {result.exit_code}: {(result.stderr or '')[:200]}",
            )

        # Close the milestone driver issue with a single summary comment
        milestone_issue_number: int | None = node.context.get("milestone_issue_number")
        if milestone_issue_number:
            modules = plan_artifact.get("modules", [])
            files_per_module = plan_artifact.get("files_per_module", {})
            module_lines = "\n".join(
                f"- `{m}`: {', '.join(f'`{f}`' for f in files_per_module.get(m, []))}"
                for m in modules
            )
            summary = (
                f"**`{milestone}` complete.** All modules built and merged.\n\n"
                f"{module_lines}"
            )
            subprocess.run(
                ["gh", "issue", "close", str(milestone_issue_number), "--repo", repo,
                 "--comment", summary],
                capture_output=True,
                text=True,
            )
            if self._store:
                bead = self._store.read_work_bead(milestone_issue_number)
                if bead:
                    bead.state = "closed"  # type: ignore[assignment]
                    self._store.write_work_bead(bead)

        return NodeResult(success=True, output={"readme": True, "repo": repo})

    def recover(self, node: GraphNode, config: Config) -> NodeResult | None:
        """Readme nodes have no recoverable state — always re-dispatch."""
        return None
