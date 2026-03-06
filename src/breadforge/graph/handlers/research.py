"""ResearchHandler — research node executor with pluggable backend support.

Default behaviour (``config.research_backend == "anthropic"``) spawns a
restricted headless claude subprocess via :func:`run_agent` — identical to
the original implementation, and required to keep the WebSearch/WebFetch
tool restriction in place.

When a non-anthropic backend is configured (``"gemini"`` or ``"openai"``),
the handler calls the backend's :meth:`complete` method directly.  This
skips the subprocess and lets research be routed to Gemini or GPT-4.1 while
build nodes remain on Claude.

Timeout: 15 minutes (subprocess path only).
Findings are stored as markdown in the bead store under the node ID.
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
    """Runs a research agent to investigate unknowns.

    Routes to a subprocess (anthropic) or a direct backend call
    (gemini / openai) based on ``config.research_backend``.
    """

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

        if config.research_backend != "anthropic":
            findings = await self._execute_via_backend(prompt, config)
            if findings is None:
                return NodeResult(
                    success=False,
                    error=f"research backend '{config.research_backend}' returned no content",
                )
            agent_cost = None
        else:
            # Default: restricted subprocess claude with WebSearch/WebFetch only.
            result = await run_agent(
                prompt,
                model=node.assigned_model or config.model,
                timeout_minutes=RESEARCH_TIMEOUT_MINUTES,
                allowed_tools=RESEARCH_ALLOWED_TOOLS,
            )
            if not result.success:
                return NodeResult(
                    success=False,
                    error=f"research agent failed (exit {result.exit_code})",
                )
            findings = result.stdout.strip()
            agent_cost = result.cost_usd

        if self._store:
            self._store.store_research_findings(node.id, findings)

        if self._logger:
            self._logger.info(
                f"research {node.id} complete ({len(findings)} chars)",
                node_id=node.id,
                milestone=milestone,
            )

        out: dict = {"findings": findings, "node_id": node.id}
        if agent_cost is not None:
            out["cost_usd"] = agent_cost
        return NodeResult(success=True, output=out)

    def recover(self, node: GraphNode, config: Config) -> NodeResult | None:
        """Research nodes have no recoverable state — always re-dispatch."""
        return None

    async def _execute_via_backend(self, prompt: str, config: Config) -> str | None:
        """Call a non-anthropic backend directly and return the text content."""
        from breadforge.backends import get_backend

        backend = get_backend(
            config.research_backend,
            model=config.research_model,
        )
        response = await backend.complete(prompt, max_tokens=2048)
        return response.content.strip() or None
