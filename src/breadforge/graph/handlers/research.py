"""ResearchHandler — restricted claude --print run for investigation tasks.

Uses claude --print with WebSearch and WebFetch only (no codebase access).
Timeout: 15 minutes. Findings stored as markdown in the beads directory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from breadforge.agents.prompts import RESEARCH_PROMPT
from breadforge.agents.runner import run_agent
from breadforge.beads.types import GraphNode
from breadforge.graph.node import NodeResult

if TYPE_CHECKING:
    from breadforge.beads.store import BeadStore
    from breadforge.config import Config
    from breadforge.logger import Logger

RESEARCH_TIMEOUT_MINUTES = 15
RESEARCH_ALLOWED_TOOLS = ["WebSearch", "WebFetch"]


class ResearchHandler:
    """Runs a restricted claude agent to investigate unknowns."""

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
        unknowns: list[str] = node.context.get("unknowns", [])

        if not unknowns:
            return NodeResult(success=True, output={"findings": "", "node_id": node.id})

        unknowns_text = "\n".join(f"- {u}" for u in unknowns)
        prompt = RESEARCH_PROMPT.format(
            repo=repo,
            milestone=milestone,
            unknowns=unknowns_text,
        )

        result = await run_agent(
            prompt,
            model=config.model,
            timeout_minutes=RESEARCH_TIMEOUT_MINUTES,
            allowed_tools=RESEARCH_ALLOWED_TOOLS,
        )

        if not result.success:
            return NodeResult(
                success=False,
                error=f"research agent failed (exit {result.exit_code})",
            )

        findings = result.stdout.strip()

        if self._store:
            self._store.store_research_findings(node.id, findings)

        if self._logger:
            self._logger.info(
                f"research {node.id} complete ({len(findings)} chars)",
                node_id=node.id,
                milestone=milestone,
            )

        return NodeResult(
            success=True,
            output={"findings": findings, "node_id": node.id},
        )
