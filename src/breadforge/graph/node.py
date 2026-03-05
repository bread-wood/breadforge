"""Graph node types, state machine, and NodeHandler protocol.

Re-exports GraphNode/PlanArtifact/NodeType/NodeState from beads.types for
convenience, and adds NodeResult + NodeHandler protocol used by handlers.

NodeType (defined in beads/types.py) includes all supported types including
the extended types: wait, consensus, design_doc.  ExtendedNodeType is kept
as an alias for backward compatibility.  Use make_node() for programmatic
node construction; GraphNode() can be used directly for all node types.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from breadforge.beads.types import (
    GraphNode,
    NodeState,
    NodeType,
    PlanArtifact,
)

if TYPE_CHECKING:
    from breadforge.config import Config

ExtendedNodeType = Literal[
    "research",
    "plan",
    "build",
    "merge",
    "readme",
    # consensus module additions
    "wait",
    "consensus",
    "design_doc",
]

__all__ = [
    "GraphNode",
    "NodeState",
    "NodeType",
    "ExtendedNodeType",
    "NodeResult",
    "NodeHandler",
    "PlanArtifact",
    "BackendRouter",
    "CredentialProxy",
    "make_node",
]


class NodeResult:
    """Result returned by a NodeHandler.execute() call.

    abandon=True skips the normal retry cycle and marks the node abandoned immediately.
    Use this when retrying would be pointless (e.g. build dependency already abandoned,
    duplicate PR detected).
    """

    __slots__ = ("success", "output", "error", "abandon")

    def __init__(
        self,
        success: bool,
        output: dict[str, Any] | None = None,
        error: str | None = None,
        abandon: bool = False,
    ) -> None:
        self.success = success
        self.output = output or {}
        self.error = error
        self.abandon = abandon

    def __repr__(self) -> str:
        return f"NodeResult(success={self.success}, error={self.error!r})"


class NodeHandler(Protocol):
    """Protocol that every handler (plan/research/build/merge/wait/consensus/design_doc)
    must implement."""

    async def execute(self, node: GraphNode, config: Config) -> NodeResult:
        """Execute the node and return a result. Must not mutate graph state."""
        ...

    def recover(self, node: GraphNode, config: Config) -> NodeResult | None:
        """Called for nodes found in 'running' state after a crash/restart.

        Return a NodeResult to mark the node done/failed without re-running it,
        or None to treat the node as pending and re-dispatch normally.
        Default: return None (re-dispatch).
        """
        return None


# ---------------------------------------------------------------------------
# BackendRouter — pluggable LLM backend routing per node type
# ---------------------------------------------------------------------------


class BackendRouter:
    """Routes node types to LLM backend model strings.

    research/plan nodes default to ``research_model`` (may be Gemini or
    GPT-4.1); build/merge/readme nodes default to ``build_model`` (Claude);
    wait/consensus/design_doc nodes use ``design_model``.

    Pass instances to GraphExecutor to override per-node model selection
    without changing Config.model.
    """

    _RESEARCH_TYPES: frozenset[str] = frozenset({"research", "plan"})
    _BUILD_TYPES: frozenset[str] = frozenset({"build", "merge", "readme"})

    def __init__(
        self,
        build_model: str = "claude-sonnet-4-6",
        research_model: str | None = None,
        design_model: str | None = None,
    ) -> None:
        self.build_model = build_model
        self.research_model = research_model or build_model
        self.design_model = design_model or self.research_model

    def route(self, node_type: str) -> str:
        """Return the model string for the given node type."""
        if node_type in self._RESEARCH_TYPES:
            return self.research_model
        if node_type in self._BUILD_TYPES:
            return self.build_model
        return self.design_model

    @classmethod
    def from_env(cls) -> BackendRouter:
        """Construct from BREADFORGE_* environment variables."""
        import os

        return cls(
            build_model=os.environ.get("BREADFORGE_BUILD_MODEL", "claude-sonnet-4-6"),
            research_model=os.environ.get("BREADFORGE_RESEARCH_MODEL") or None,
            design_model=os.environ.get("BREADFORGE_DESIGN_MODEL") or None,
        )


# ---------------------------------------------------------------------------
# CredentialProxy — scoped token issuance (v0.2 passthrough)
# ---------------------------------------------------------------------------


class CredentialProxy:
    """Issues scoped API tokens for agent sub-processes.

    Prevents raw API key propagation by centralising credential access.
    v0.2 is a passthrough wrapper; future versions may implement a loopback
    HTTP proxy with per-scope rate limiting and audit logging.
    """

    _VALID_SCOPES = frozenset({"build", "research", "design", "merge"})

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key

    def scoped_token(self, scope: str) -> str | None:
        """Return a token valid for *scope*.  Returns the raw key in v0.2."""
        if scope not in self._VALID_SCOPES:
            raise ValueError(f"unknown scope {scope!r}; valid: {sorted(self._VALID_SCOPES)}")
        return self._api_key

    @classmethod
    def from_env(cls) -> CredentialProxy:
        import os

        return cls(api_key=os.environ.get("ANTHROPIC_API_KEY"))


# ---------------------------------------------------------------------------
# make_node — factory for extended node types
# ---------------------------------------------------------------------------


def make_node(
    id: str,
    type: str,
    state: str = "pending",
    depends_on: list[str] | None = None,
    context: dict[str, Any] | None = None,
    max_retries: int = 3,
    assigned_model: str | None = None,
) -> GraphNode:
    """Construct a GraphNode, including extended types (wait/consensus/design_doc).

    Uses model_construct() to bypass Pydantic's Literal validation so that
    types outside of the core NodeType Literal are accepted at runtime.
    """
    return GraphNode.model_construct(
        id=id,
        type=type,
        state=state,
        depends_on=depends_on if depends_on is not None else [],
        context=context if context is not None else {},
        output=None,
        assigned_model=assigned_model,
        retry_count=0,
        max_retries=max_retries,
        created_at=datetime.now(UTC),
        started_at=None,
        completed_at=None,
    )
