"""Tests for the ConsensusHandler node type.

ConsensusHandler runs multiple AI completions on the same question and aggregates
the responses using majority voting.  It is used for high-stakes decisions (model
tier selection, approval gates, confidence triage) where a single model output
is insufficient.

This file defines the handler inline; production code will go in
src/breadforge/graph/handlers/consensus.py.

Behavioral contract:
  - Collects responses from n_voters completions
  - A vote is the first line of the response (normalised to lowercase)
  - Majority vote wins; ties are broken alphabetically
  - Stores all raw responses in output["responses"]
  - Stores winning vote in output["decision"]
  - Stores vote tally in output["tally"]
  - Returns NodeResult(success=True) once quorum is reached
  - Returns NodeResult(success=False) if fewer than quorum_required voters
    return a non-empty response
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from breadforge.beads.types import GraphNode
from breadforge.config import Config
from breadforge.graph.executor import ExecutionGraph, GraphExecutor
from breadforge.graph.node import NodeResult

# ---------------------------------------------------------------------------
# ConsensusHandler implementation
# ---------------------------------------------------------------------------


CompletionFn = Callable[[str, str], Awaitable[str]]


class ConsensusHandler:
    """Runs n_voters completions and picks the majority response.

    Context keys:
        question (str): the prompt/question to present to each voter
        n_voters (int): total voters to run (default 3)
        quorum_required (int): minimum non-empty responses to consider valid
                                (default ceil(n_voters / 2))
        model (str): model to use for completions (overrides config.model)
    """

    def __init__(
        self,
        completion_fn: CompletionFn | None = None,
    ) -> None:
        self._completion_fn = completion_fn or self._default_complete

    async def execute(self, node: GraphNode, config: Config) -> NodeResult:
        question: str = node.context.get("question", "")
        n_voters: int = int(node.context.get("n_voters", 3))
        quorum_required: int = int(node.context.get("quorum_required", (n_voters // 2) + 1))
        model: str = node.context.get("model") or node.assigned_model or config.model

        if not question:
            return NodeResult(success=False, error="consensus node requires context['question']")

        # Run all voters concurrently
        tasks = [asyncio.create_task(self._completion_fn(question, model)) for _ in range(n_voters)]
        raw_responses: list[str] = []
        for task in tasks:
            try:
                resp = await task
                raw_responses.append(resp)
            except Exception:
                raw_responses.append("")  # treat failures as abstentions

        # Extract votes (first non-empty line, normalised)
        votes: list[str] = []
        for resp in raw_responses:
            first_line = resp.strip().splitlines()[0] if resp.strip() else ""
            if first_line:
                votes.append(first_line.lower().strip())

        if len(votes) < quorum_required:
            return NodeResult(
                success=False,
                error=(
                    f"consensus failed: only {len(votes)} valid votes (required {quorum_required})"
                ),
            )

        tally = Counter(votes)
        # Majority: most common; tie-break alphabetically
        max_count = tally.most_common(1)[0][1]
        candidates = sorted(v for v, c in tally.items() if c == max_count)
        decision = candidates[0]

        return NodeResult(
            success=True,
            output={
                "decision": decision,
                "tally": dict(tally),
                "responses": raw_responses,
                "votes": votes,
                "n_voters": n_voters,
                "quorum_required": quorum_required,
            },
        )

    async def _default_complete(self, prompt: str, model: str) -> str:
        """Fallback completion — requires API key; only called in integration tests."""
        raise NotImplementedError("Provide a completion_fn to ConsensusHandler in tests")

    def recover(self, node: GraphNode, config: Config) -> NodeResult | None:
        """Re-run consensus on restart — votes may differ; no idempotency assumed."""
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def make_consensus_node(
    node_id: str = "consensus-1",
    question: str = "Should we proceed? Answer yes or no.",
    n_voters: int = 3,
    quorum_required: int | None = None,
    max_retries: int = 3,
    depends_on: list[str] | None = None,
) -> GraphNode:
    """Build a 'consensus' GraphNode via model_construct (bypasses Literal check)."""
    context: dict = {"question": question, "n_voters": n_voters}
    if quorum_required is not None:
        context["quorum_required"] = quorum_required
    return GraphNode.model_construct(
        id=node_id,
        type="consensus",
        state="pending",
        depends_on=depends_on or [],
        context=context,
        output=None,
        assigned_model=None,
        retry_count=0,
        max_retries=max_retries,
        created_at=_now(),
        started_at=None,
        completed_at=None,
    )


def _make_completion(*responses: str) -> CompletionFn:
    """Return a completion function that cycles through fixed responses."""
    it = iter(responses)

    async def _fn(prompt: str, model: str) -> str:
        return next(it, "")

    return _fn


@pytest.fixture
def config() -> Config:
    return Config.from_env("owner/repo")


# ---------------------------------------------------------------------------
# Majority voting
# ---------------------------------------------------------------------------


class TestConsensusHandlerVoting:
    async def test_unanimous_vote_wins(self, config: Config) -> None:
        handler = ConsensusHandler(completion_fn=_make_completion("yes", "yes", "yes"))
        node = make_consensus_node(n_voters=3)
        result = await handler.execute(node, config)
        assert result.success
        assert result.output["decision"] == "yes"

    async def test_majority_wins_over_minority(self, config: Config) -> None:
        handler = ConsensusHandler(completion_fn=_make_completion("yes", "yes", "no"))
        node = make_consensus_node(n_voters=3)
        result = await handler.execute(node, config)
        assert result.success
        assert result.output["decision"] == "yes"

    async def test_alphabetical_tiebreak(self, config: Config) -> None:
        # yes and no each get 1 vote → alphabetical → "no" wins
        handler = ConsensusHandler(completion_fn=_make_completion("yes", "no"))
        node = make_consensus_node(n_voters=2, quorum_required=2)
        result = await handler.execute(node, config)
        assert result.success
        assert result.output["decision"] == "no"

    async def test_tally_reflects_vote_counts(self, config: Config) -> None:
        handler = ConsensusHandler(completion_fn=_make_completion("yes", "yes", "no"))
        node = make_consensus_node(n_voters=3)
        result = await handler.execute(node, config)
        assert result.output["tally"]["yes"] == 2
        assert result.output["tally"]["no"] == 1

    async def test_responses_stored_in_output(self, config: Config) -> None:
        handler = ConsensusHandler(completion_fn=_make_completion("yes", "no", "yes"))
        node = make_consensus_node(n_voters=3)
        result = await handler.execute(node, config)
        assert len(result.output["responses"]) == 3

    async def test_votes_normalised_to_lowercase(self, config: Config) -> None:
        handler = ConsensusHandler(completion_fn=_make_completion("YES", "Yes", "yes"))
        node = make_consensus_node(n_voters=3)
        result = await handler.execute(node, config)
        assert result.output["decision"] == "yes"
        assert all(v == "yes" for v in result.output["votes"])


# ---------------------------------------------------------------------------
# Quorum and failure paths
# ---------------------------------------------------------------------------


class TestConsensusHandlerQuorum:
    async def test_fails_without_question(self, config: Config) -> None:
        handler = ConsensusHandler(completion_fn=_make_completion("yes"))
        node = make_consensus_node(question="", n_voters=1)
        result = await handler.execute(node, config)
        assert not result.success
        assert "requires" in (result.error or "").lower()

    async def test_fails_when_below_quorum(self, config: Config) -> None:
        # 1 voter returns empty response → below quorum of 2
        handler = ConsensusHandler(completion_fn=_make_completion(""))
        node = make_consensus_node(n_voters=1, quorum_required=2)
        result = await handler.execute(node, config)
        assert not result.success
        assert "required" in (result.error or "").lower()

    async def test_empty_responses_count_as_abstentions(self, config: Config) -> None:
        handler = ConsensusHandler(completion_fn=_make_completion("yes", "", "yes"))
        node = make_consensus_node(n_voters=3, quorum_required=2)
        result = await handler.execute(node, config)
        assert result.success
        assert result.output["decision"] == "yes"

    async def test_recover_returns_none(self, config: Config) -> None:
        handler = ConsensusHandler(completion_fn=_make_completion("yes"))
        node = make_consensus_node()
        assert handler.recover(node, config) is None

    async def test_single_voter_succeeds(self, config: Config) -> None:
        handler = ConsensusHandler(completion_fn=_make_completion("approved"))
        node = make_consensus_node(n_voters=1, quorum_required=1)
        result = await handler.execute(node, config)
        assert result.success
        assert result.output["decision"] == "approved"


# ---------------------------------------------------------------------------
# Integration — consensus node through GraphExecutor
# ---------------------------------------------------------------------------


class TestConsensusHandlerIntegration:
    def test_executor_runs_consensus_then_build(self, config: Config) -> None:
        consensus_node = make_consensus_node(
            "gate",
            question="Should we proceed?",
            n_voters=3,
        )
        build_node = GraphNode(
            id="build-after-consensus",
            type="build",
            depends_on=["gate"],
        )
        graph = ExecutionGraph([consensus_node, build_node])

        consensus_handler = ConsensusHandler(completion_fn=_make_completion("yes", "yes", "no"))
        build_handler = AsyncMock()
        build_handler.execute = AsyncMock(return_value=NodeResult(success=True))
        build_handler.recover = lambda node, cfg: None

        executor = GraphExecutor(
            config=config,
            handlers={"consensus": consensus_handler, "build": build_handler},
            concurrency=2,
            watchdog_interval=0.1,
        )
        result = asyncio.run(executor.run(graph))
        assert result.success
        assert "gate" in result.done
        assert "build-after-consensus" in result.done

    def test_failed_consensus_unblocks_downstream(self, config: Config) -> None:
        """Abandoned consensus (no quorum) counts as terminal — downstream runs."""
        consensus_node = make_consensus_node(
            "gate-fail",
            question="",  # missing question → immediate failure
            n_voters=1,
            max_retries=1,
        )
        build_node = GraphNode(
            id="build-after-fail",
            type="build",
            depends_on=["gate-fail"],
        )
        graph = ExecutionGraph([consensus_node, build_node])

        handler = ConsensusHandler(completion_fn=_make_completion("yes"))
        build_handler = AsyncMock()
        build_handler.execute = AsyncMock(return_value=NodeResult(success=True))
        build_handler.recover = lambda node, cfg: None

        executor = GraphExecutor(
            config=config,
            handlers={"consensus": handler, "build": build_handler},
            concurrency=2,
            watchdog_interval=0.1,
        )
        result = asyncio.run(executor.run(graph))
        assert "gate-fail" in result.abandoned
        assert "build-after-fail" in result.done
