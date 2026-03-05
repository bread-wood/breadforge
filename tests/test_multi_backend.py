"""Tests for the pluggable backend abstraction.

The backend abstraction allows different node types to be executed by different
AI providers:
  - research/plan nodes → Gemini or GPT-4.1 (cheaper, web-search capable)
  - build nodes         → Claude (precise tool-use)

This file defines the Backend protocol and BackendRouter used to select the
correct provider.  Production code will live in src/breadforge/graph/backends.py.

Contract:
  - Backend.complete(prompt, model, **kwargs) -> str
  - BackendRouter.select(node) -> Backend
  - BackendRouter can be configured per-node-type
  - Research findings from alternate backends feed into the graph normally
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import pytest

from breadforge.beads.types import GraphNode
from breadforge.config import Config

# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Backend(Protocol):
    """Minimal protocol for an AI completion backend."""

    name: str

    async def complete(self, prompt: str, model: str, **kwargs) -> str:  # type: ignore[override]
        """Return the model's text response."""
        ...


# ---------------------------------------------------------------------------
# Concrete backend stubs
# ---------------------------------------------------------------------------


class AnthropicBackend:
    name = "anthropic"

    def __init__(self, api_key: str = "test-key") -> None:
        self._api_key = api_key
        self.calls: list[dict] = []

    async def complete(self, prompt: str, model: str, **kwargs) -> str:
        self.calls.append({"prompt": prompt, "model": model})
        return f"[anthropic:{model}] response to: {prompt[:40]}"


class GeminiBackend:
    name = "gemini"

    def __init__(self, api_key: str = "test-key") -> None:
        self._api_key = api_key
        self.calls: list[dict] = []

    async def complete(self, prompt: str, model: str, **kwargs) -> str:
        self.calls.append({"prompt": prompt, "model": model})
        return f"[gemini:{model}] response to: {prompt[:40]}"


class OpenAIBackend:
    name = "openai"

    def __init__(self, api_key: str = "test-key") -> None:
        self._api_key = api_key
        self.calls: list[dict] = []

    async def complete(self, prompt: str, model: str, **kwargs) -> str:
        self.calls.append({"prompt": prompt, "model": model})
        return f"[openai:{model}] response to: {prompt[:40]}"


# ---------------------------------------------------------------------------
# BackendRouter
# ---------------------------------------------------------------------------


class BackendRouter:
    """Routes node types to registered backends.

    Default routing:
        research → gemini
        plan     → openai
        build    → anthropic (Claude)
        merge    → anthropic
        readme   → anthropic
    """

    _DEFAULT_ROUTING: dict[str, str] = {
        "research": "gemini",
        "plan": "openai",
        "build": "anthropic",
        "merge": "anthropic",
        "readme": "anthropic",
    }

    def __init__(self, backends: dict[str, Backend]) -> None:
        self._backends = backends
        self._routing: dict[str, str] = dict(self._DEFAULT_ROUTING)

    def configure(self, node_type: str, backend_name: str) -> None:
        """Override routing for a specific node type."""
        if backend_name not in self._backends:
            raise ValueError(f"Backend {backend_name!r} not registered")
        self._routing[node_type] = backend_name

    def select(self, node: GraphNode) -> Backend:
        """Return the Backend instance appropriate for this node."""
        backend_name = self._routing.get(node.type, "anthropic")
        backend = self._backends.get(backend_name)
        if backend is None:
            # Fallback to first available backend
            backend = next(iter(self._backends.values()))
        return backend

    def select_model(self, node: GraphNode) -> str:
        """Return the model string appropriate for this node type."""
        if node.assigned_model:
            return node.assigned_model
        model_map: dict[str, str] = {
            "research": "gemini-2.5-pro",
            "plan": "gpt-4.1",
            "build": "claude-sonnet-4-6",
            "merge": "claude-sonnet-4-6",
            "readme": "claude-sonnet-4-6",
        }
        return model_map.get(node.type, "claude-sonnet-4-6")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def anthropic() -> AnthropicBackend:
    return AnthropicBackend()


@pytest.fixture
def gemini() -> GeminiBackend:
    return GeminiBackend()


@pytest.fixture
def openai_backend() -> OpenAIBackend:
    return OpenAIBackend()


@pytest.fixture
def router(
    anthropic: AnthropicBackend,
    gemini: GeminiBackend,
    openai_backend: OpenAIBackend,
) -> BackendRouter:
    return BackendRouter(
        backends={
            "anthropic": anthropic,
            "gemini": gemini,
            "openai": openai_backend,
        }
    )


@pytest.fixture
def config() -> Config:
    return Config.from_env("owner/repo")


# ---------------------------------------------------------------------------
# Backend protocol compliance
# ---------------------------------------------------------------------------


class TestBackendProtocol:
    def test_anthropic_implements_backend_protocol(self, anthropic: AnthropicBackend) -> None:
        assert isinstance(anthropic, Backend)

    def test_gemini_implements_backend_protocol(self, gemini: GeminiBackend) -> None:
        assert isinstance(gemini, Backend)

    def test_openai_implements_backend_protocol(self, openai_backend: OpenAIBackend) -> None:
        assert isinstance(openai_backend, Backend)

    async def test_complete_returns_string(self, anthropic: AnthropicBackend) -> None:
        result = await anthropic.complete("What is 2+2?", "claude-sonnet-4-6")
        assert isinstance(result, str)
        assert len(result) > 0

    async def test_complete_records_call(self, gemini: GeminiBackend) -> None:
        await gemini.complete("Explain recursion", "gemini-2.5-pro")
        assert len(gemini.calls) == 1
        assert gemini.calls[0]["model"] == "gemini-2.5-pro"


# ---------------------------------------------------------------------------
# BackendRouter — routing logic
# ---------------------------------------------------------------------------


class TestBackendRouterRouting:
    def test_research_node_routes_to_gemini(
        self, router: BackendRouter, gemini: GeminiBackend
    ) -> None:
        node = GraphNode(id="research-1", type="research", context={})
        selected = router.select(node)
        assert selected is gemini

    def test_plan_node_routes_to_openai(
        self, router: BackendRouter, openai_backend: OpenAIBackend
    ) -> None:
        node = GraphNode(id="plan-1", type="plan", context={})
        selected = router.select(node)
        assert selected is openai_backend

    def test_build_node_routes_to_anthropic(
        self, router: BackendRouter, anthropic: AnthropicBackend
    ) -> None:
        node = GraphNode(id="build-1", type="build", context={})
        selected = router.select(node)
        assert selected is anthropic

    def test_merge_node_routes_to_anthropic(
        self, router: BackendRouter, anthropic: AnthropicBackend
    ) -> None:
        node = GraphNode(id="merge-1", type="merge", context={})
        selected = router.select(node)
        assert selected is anthropic

    def test_readme_node_routes_to_anthropic(
        self, router: BackendRouter, anthropic: AnthropicBackend
    ) -> None:
        node = GraphNode(id="readme-1", type="readme", context={})
        selected = router.select(node)
        assert selected is anthropic


# ---------------------------------------------------------------------------
# BackendRouter — model selection
# ---------------------------------------------------------------------------


class TestBackendRouterModelSelection:
    def test_research_node_model(self, router: BackendRouter) -> None:
        node = GraphNode(id="r1", type="research", context={})
        assert router.select_model(node) == "gemini-2.5-pro"

    def test_plan_node_model(self, router: BackendRouter) -> None:
        node = GraphNode(id="p1", type="plan", context={})
        assert router.select_model(node) == "gpt-4.1"

    def test_build_node_model(self, router: BackendRouter) -> None:
        node = GraphNode(id="b1", type="build", context={})
        assert router.select_model(node) == "claude-sonnet-4-6"

    def test_assigned_model_overrides_routing(self, router: BackendRouter) -> None:
        node = GraphNode(id="r1", type="research", context={})
        node.assigned_model = "claude-opus-4-6"
        assert router.select_model(node) == "claude-opus-4-6"


# ---------------------------------------------------------------------------
# BackendRouter — configuration
# ---------------------------------------------------------------------------


class TestBackendRouterConfiguration:
    def test_configure_overrides_default_routing(
        self,
        router: BackendRouter,
        anthropic: AnthropicBackend,
        gemini: GeminiBackend,
    ) -> None:
        # Redirect research to anthropic instead of gemini
        router.configure("research", "anthropic")
        node = GraphNode(id="r1", type="research", context={})
        assert router.select(node) is anthropic

    def test_configure_unknown_backend_raises(self, router: BackendRouter) -> None:
        with pytest.raises(ValueError, match="not registered"):
            router.configure("research", "no_such_backend")


# ---------------------------------------------------------------------------
# Backend completion integration
# ---------------------------------------------------------------------------


class TestBackendCompletion:
    async def test_research_backend_receives_prompt(
        self, router: BackendRouter, gemini: GeminiBackend
    ) -> None:
        node = GraphNode(id="research-2", type="research", context={})
        backend = router.select(node)
        model = router.select_model(node)
        response = await backend.complete("Investigate the GitHub API rate limits", model)
        assert "gemini" in response
        assert model in response

    async def test_build_backend_receives_prompt(
        self, router: BackendRouter, anthropic: AnthropicBackend
    ) -> None:
        node = GraphNode(id="build-2", type="build", context={})
        backend = router.select(node)
        model = router.select_model(node)
        response = await backend.complete("Implement the auth module", model)
        assert "anthropic" in response

    async def test_multiple_completions_tracked(
        self, router: BackendRouter, gemini: GeminiBackend
    ) -> None:
        node = GraphNode(id="r3", type="research", context={})
        backend = router.select(node)
        model = router.select_model(node)
        await backend.complete("First question", model)
        await backend.complete("Second question", model)
        assert len(gemini.calls) == 2
