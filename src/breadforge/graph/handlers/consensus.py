"""Consensus module handlers: WaitHandler, ConsensusHandler, DesignDocHandler.

WaitHandler
    Polls cross-repo CampaignBead entries until all blocking milestones are
    shipped.  Fails (triggering retry) while any upstream milestone is still
    pending/implementing; succeeds once every blocker is ``shipped``.

ConsensusHandler
    Selects the best proposal from a set of candidates.  Candidates can be
    passed directly in ``context["proposals"]`` or gathered from completed
    dependent node outputs via ``context["proposal_node_ids"]``.  An LLM
    call is used to pick the winner when more than one candidate exists.

DesignDocHandler
    Calls an LLM to produce a structured design document from a title,
    requirements, and optional constraints stored in node context.  The
    resulting doc is stored as a research finding and returned in output.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from breadforge.beads.types import GraphNode
from breadforge.graph.node import NodeResult

if TYPE_CHECKING:
    from breadforge.beads.store import BeadStore
    from breadforge.config import Config
    from breadforge.logger import Logger

# ---------------------------------------------------------------------------
# WaitHandler
# ---------------------------------------------------------------------------

_WAIT_POLL_SECONDS = 60


class WaitHandler:
    """Waits for cross-repo milestone dependencies to be shipped.

    Context keys
    ------------
    blocking_milestones : list[str]
        ``"owner/repo:milestone"`` strings that must reach ``shipped`` status.

    The handler returns failure while any blocker is unshipped (executor
    will retry up to ``node.max_retries`` times).  Set ``max_retries``
    proportionally to the expected wait time (e.g. 60 retries × 60 s =
    1 hour).
    """

    def __init__(
        self,
        store: BeadStore | None = None,
        logger: Logger | None = None,
    ) -> None:
        self._store = store
        self._logger = logger

    async def execute(self, node: GraphNode, config: Config) -> NodeResult:
        blocking: list[str] = node.context.get("blocking_milestones", [])
        if not blocking:
            return NodeResult(success=True, output={"unblocked": True, "blocking": []})

        unshipped = []
        for ref in blocking:
            if ":" not in ref:
                if self._logger:
                    self._logger.error(f"wait node {node.id}: invalid ref {ref!r}, skipping")
                continue
            repo, milestone = ref.split(":", 1)
            if not self._is_shipped(repo.strip(), milestone.strip(), config):
                unshipped.append(ref)

        if unshipped:
            if self._logger:
                self._logger.info(
                    f"wait node {node.id}: still blocked on {unshipped}",
                    node_id=node.id,
                )
            # Brief sleep so retries do not hammer disk I/O in tight loops
            await asyncio.sleep(_WAIT_POLL_SECONDS)
            return NodeResult(
                success=False,
                error=f"blocked: waiting for {', '.join(unshipped)}",
            )

        if self._logger:
            self._logger.info(f"wait node {node.id}: all blockers shipped", node_id=node.id)
        return NodeResult(success=True, output={"unblocked": True, "blocking": blocking})

    def recover(self, node: GraphNode, config: Config) -> NodeResult | None:
        return None

    def _is_shipped(self, repo: str, milestone: str, config: Config) -> bool:
        if not self._store:
            return False
        from breadforge.beads.store import BeadStore

        # Build a store scoped to the blocking repo
        blocking_store = BeadStore(config.beads_dir, repo)
        campaign = blocking_store.read_campaign_bead()
        if not campaign:
            return False
        plan = campaign.get_milestone(milestone, repo=repo)
        return plan is not None and plan.status == "shipped"


# ---------------------------------------------------------------------------
# ConsensusHandler
# ---------------------------------------------------------------------------

_CONSENSUS_PROMPT = """\
You are selecting the best proposal from the following candidates.

CANDIDATES:
{candidates_text}

Return a JSON object with exactly these keys:
{{
  "winner_index": <int, 0-based index of best proposal>,
  "rationale": "<1-2 sentence explanation>"
}}

Prefer the proposal that is most concrete, feasible, and aligned with the
system's existing architecture.  If all proposals are equivalent, pick the
first.
"""


class ConsensusHandler:
    """Selects the best proposal from multiple candidates.

    Context keys
    ------------
    proposals : list[dict]
        Each dict has ``"id"`` (str), ``"text"`` (str), and optionally
        ``"source"`` (str).  Used directly when present.
    proposal_node_ids : list[str]
        IDs of completed dependent nodes whose ``output["proposal"]`` values
        are collected as candidates.  Ignored when ``proposals`` is set.
    selection_model : str
        Optional model override for the selection LLM call.

    Output keys
    -----------
    winner_id : str
    winner_text : str
    rationale : str
    """

    def __init__(
        self,
        store: BeadStore | None = None,
        logger: Logger | None = None,
    ) -> None:
        self._store = store
        self._logger = logger

    async def execute(self, node: GraphNode, config: Config) -> NodeResult:
        proposals: list[dict[str, Any]] = node.context.get("proposals", [])

        if not proposals:
            # Gather from completed dependent node outputs
            node_ids: list[str] = node.context.get("proposal_node_ids", [])
            proposals = self._gather_from_nodes(node_ids)

        if not proposals:
            return NodeResult(
                success=False,
                error="consensus node has no proposals (set context.proposals or proposal_node_ids)",
            )

        if len(proposals) == 1:
            winner = proposals[0]
            return NodeResult(
                success=True,
                output={
                    "winner_id": winner.get("id", "0"),
                    "winner_text": winner.get("text", ""),
                    "rationale": "single candidate — no selection needed",
                },
            )

        model = node.context.get("selection_model") or config.model
        try:
            winner_index, rationale = await self._call_selection_llm(proposals, model)
        except Exception as e:
            # Fallback: pick first candidate
            if self._logger:
                self._logger.error(
                    f"consensus {node.id}: LLM selection failed ({e}), using first candidate",
                    node_id=node.id,
                )
            winner_index, rationale = 0, f"LLM selection failed: {e}"

        winner = proposals[winner_index] if winner_index < len(proposals) else proposals[0]
        if self._logger:
            self._logger.info(
                f"consensus {node.id}: selected candidate {winner_index} — {rationale[:80]}",
                node_id=node.id,
            )
        return NodeResult(
            success=True,
            output={
                "winner_id": winner.get("id", str(winner_index)),
                "winner_text": winner.get("text", ""),
                "rationale": rationale,
            },
        )

    def recover(self, node: GraphNode, config: Config) -> NodeResult | None:
        return None

    def _gather_from_nodes(self, node_ids: list[str]) -> list[dict[str, Any]]:
        if not self._store or not node_ids:
            return []
        proposals = []
        for nid in node_ids:
            stored = self._store.read_node(nid)
            if stored and stored.output:
                text = stored.output.get("proposal") or stored.output.get("findings", "")
                proposals.append({"id": nid, "text": str(text), "source": nid})
        return proposals

    async def _call_selection_llm(
        self,
        proposals: list[dict[str, Any]],
        model: str,
    ) -> tuple[int, str]:
        candidates_text = "\n\n".join(
            f"[{i}] (id={p.get('id', i)})\n{p.get('text', '')}" for i, p in enumerate(proposals)
        )
        prompt = _CONSENSUS_PROMPT.format(candidates_text=candidates_text)

        try:
            from breadmin_llm.registry import ProviderRegistry
            from breadmin_llm.types import LLMCall, LLMMessage, MessageRole

            registry = ProviderRegistry.default()
            call = LLMCall(
                model=model,
                messages=[LLMMessage(role=MessageRole.USER, content=prompt)],
                max_tokens=300,
                caller="breadforge.consensus",
            )
            response = await registry.complete(call)
            text = response.content
        except ImportError:
            import anthropic

            client = anthropic.AsyncAnthropic()
            response = await client.messages.create(
                model=model,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text  # type: ignore[union-attr]

        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(line for line in lines if not line.startswith("```")).strip()

        data = json.loads(text)
        return int(data["winner_index"]), str(data.get("rationale", ""))


# ---------------------------------------------------------------------------
# DesignDocHandler
# ---------------------------------------------------------------------------

_DESIGN_DOC_PROMPT = """\
You are a software architect.  Generate a concise design document for the
following task.

TITLE: {title}

REQUIREMENTS:
{requirements}

CONSTRAINTS:
{constraints}

Produce a well-structured markdown design document covering:
1. Overview and goals
2. Architecture / key components
3. Data flow
4. API / interface contracts (if applicable)
5. Open questions and risks

Be specific and practical.  Do not pad with boilerplate.
"""


class DesignDocHandler:
    """Generates a design document via LLM and stores it as a research finding.

    Context keys
    ------------
    title : str
        Short title for the design doc.
    requirements : str
        What needs to be built.
    constraints : str  (optional)
        Technical constraints or non-goals.
    design_model : str  (optional)
        Model override for this call.

    Output keys
    -----------
    doc : str          — raw markdown content
    doc_path : str     — path where the doc was stored (if store is available)
    """

    def __init__(
        self,
        store: BeadStore | None = None,
        logger: Logger | None = None,
    ) -> None:
        self._store = store
        self._logger = logger

    async def execute(self, node: GraphNode, config: Config) -> NodeResult:
        title: str = node.context.get("title", node.id)
        requirements: str = node.context.get("requirements", "")
        constraints: str = node.context.get("constraints", "None specified.")
        model: str = node.context.get("design_model") or config.model

        if not requirements:
            return NodeResult(
                success=False,
                error="design_doc node missing context.requirements",
            )

        prompt = _DESIGN_DOC_PROMPT.format(
            title=title,
            requirements=requirements[:4000],
            constraints=constraints[:1000],
        )

        try:
            doc = await self._call_design_llm(prompt, model)
        except Exception as e:
            return NodeResult(success=False, error=f"design_doc LLM call failed: {e}")

        doc_path = ""
        if self._store:
            path = self._store.store_research_findings(node.id, doc)
            doc_path = str(path)

        if self._logger:
            self._logger.info(
                f"design_doc {node.id}: generated {len(doc)} chars",
                node_id=node.id,
            )

        return NodeResult(
            success=True,
            output={"doc": doc, "doc_path": doc_path},
        )

    def recover(self, node: GraphNode, config: Config) -> NodeResult | None:
        """Re-use a previously stored doc if the node crashed after storing."""
        if not self._store:
            return None
        existing = self._store.read_research_findings(node.id)
        if existing:
            return NodeResult(
                success=True,
                output={"doc": existing, "doc_path": ""},
            )
        return None

    async def _call_design_llm(self, prompt: str, model: str) -> str:
        try:
            from breadmin_llm.registry import ProviderRegistry
            from breadmin_llm.types import LLMCall, LLMMessage, MessageRole

            registry = ProviderRegistry.default()
            call = LLMCall(
                model=model,
                messages=[LLMMessage(role=MessageRole.USER, content=prompt)],
                max_tokens=2000,
                caller="breadforge.design_doc",
            )
            response = await registry.complete(call)
            return response.content
        except ImportError:
            import anthropic

            client = anthropic.AsyncAnthropic()
            response = await client.messages.create(
                model=model,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text  # type: ignore[union-attr]
