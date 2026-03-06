"""Unit tests for graph/node.py — NodeResult, BackendRouter, CredentialProxy, make_node."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from breadforge.graph.node import (
    BackendRouter,
    CredentialProxy,
    NodeResult,
    make_node,
)

# ---------------------------------------------------------------------------
# NodeResult
# ---------------------------------------------------------------------------


class TestNodeResult:
    def test_success_defaults(self) -> None:
        r = NodeResult(success=True)
        assert r.success is True
        assert r.output == {}
        assert r.error is None
        assert r.abandon is False

    def test_failure_with_error(self) -> None:
        r = NodeResult(success=False, error="clone failed")
        assert r.success is False
        assert r.error == "clone failed"

    def test_output_populated(self) -> None:
        r = NodeResult(success=True, output={"pr": 42})
        assert r.output == {"pr": 42}

    def test_output_none_becomes_empty_dict(self) -> None:
        r = NodeResult(success=True, output=None)
        assert r.output == {}

    def test_abandon_flag(self) -> None:
        r = NodeResult(success=False, error="dep abandoned", abandon=True)
        assert r.abandon is True

    def test_repr(self) -> None:
        r = NodeResult(success=False, error="boom")
        text = repr(r)
        assert "False" in text
        assert "boom" in text

    def test_repr_no_error(self) -> None:
        r = NodeResult(success=True)
        assert "None" in repr(r)


# ---------------------------------------------------------------------------
# BackendRouter
# ---------------------------------------------------------------------------


class TestBackendRouter:
    def test_default_build_model(self) -> None:
        router = BackendRouter()
        assert router.route("build") == "claude-sonnet-4-6"
        assert router.route("merge") == "claude-sonnet-4-6"
        assert router.route("readme") == "claude-sonnet-4-6"

    def test_default_research_model_falls_back_to_build(self) -> None:
        router = BackendRouter(build_model="claude-sonnet-4-6")
        assert router.route("research") == "claude-sonnet-4-6"
        assert router.route("plan") == "claude-sonnet-4-6"

    def test_research_model_override(self) -> None:
        router = BackendRouter(build_model="claude-sonnet-4-6", research_model="gemini-2.5-pro")
        assert router.route("research") == "gemini-2.5-pro"
        assert router.route("plan") == "gemini-2.5-pro"
        assert router.route("build") == "claude-sonnet-4-6"

    def test_design_model_override(self) -> None:
        router = BackendRouter(design_model="gpt-4.1")
        assert router.route("wait") == "gpt-4.1"
        assert router.route("consensus") == "gpt-4.1"
        assert router.route("design_doc") == "gpt-4.1"

    def test_design_model_defaults_to_research_model(self) -> None:
        router = BackendRouter(research_model="gemini-2.5-pro")
        assert router.route("design_doc") == "gemini-2.5-pro"

    def test_unknown_type_returns_design_model(self) -> None:
        router = BackendRouter(build_model="claude", research_model="gemini", design_model="gpt")
        assert router.route("unknown_type") == "gpt"

    def test_from_env_defaults(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            router = BackendRouter.from_env()
        assert router.build_model == "claude-sonnet-4-6"
        assert router.research_model == "claude-sonnet-4-6"

    def test_from_env_custom_values(self) -> None:
        env = {
            "BREADFORGE_BUILD_MODEL": "claude-opus-4-6",
            "BREADFORGE_RESEARCH_MODEL": "gemini-2.5-pro",
            "BREADFORGE_DESIGN_MODEL": "gpt-4.1",
        }
        with patch.dict(os.environ, env):
            router = BackendRouter.from_env()
        assert router.build_model == "claude-opus-4-6"
        assert router.research_model == "gemini-2.5-pro"
        assert router.design_model == "gpt-4.1"

    def test_from_env_empty_string_research_model(self) -> None:
        """Empty string env var → falls back to build_model."""
        with patch.dict(os.environ, {"BREADFORGE_RESEARCH_MODEL": ""}):
            router = BackendRouter.from_env()
        assert router.research_model == router.build_model


# ---------------------------------------------------------------------------
# CredentialProxy
# ---------------------------------------------------------------------------


class TestCredentialProxy:
    def test_scoped_token_returns_key(self) -> None:
        proxy = CredentialProxy(api_key="sk-test")
        assert proxy.scoped_token("build") == "sk-test"
        assert proxy.scoped_token("research") == "sk-test"
        assert proxy.scoped_token("design") == "sk-test"
        assert proxy.scoped_token("merge") == "sk-test"

    def test_scoped_token_none_key(self) -> None:
        proxy = CredentialProxy(api_key=None)
        assert proxy.scoped_token("build") is None

    def test_invalid_scope_raises(self) -> None:
        proxy = CredentialProxy(api_key="sk-test")
        with pytest.raises(ValueError, match="unknown scope"):
            proxy.scoped_token("invalid_scope")

    def test_from_env(self) -> None:
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-env"}):
            proxy = CredentialProxy.from_env()
        assert proxy.scoped_token("build") == "sk-env"

    def test_from_env_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            proxy = CredentialProxy.from_env()
        assert proxy.scoped_token("research") is None


# ---------------------------------------------------------------------------
# make_node
# ---------------------------------------------------------------------------


class TestMakeNode:
    def test_basic_build_node(self) -> None:
        node = make_node("v1-build-core", type="build")
        assert node.id == "v1-build-core"
        assert node.type == "build"
        assert node.state == "pending"
        assert node.depends_on == []
        assert node.context == {}
        assert node.retry_count == 0
        assert node.max_retries == 3
        assert node.assigned_model is None

    def test_extended_node_types(self) -> None:
        for t in ("wait", "consensus", "design_doc"):
            node = make_node(f"v1-{t}", type=t)
            assert node.type == t

    def test_depends_on(self) -> None:
        node = make_node("v1-merge", type="merge", depends_on=["v1-build-core"])
        assert node.depends_on == ["v1-build-core"]

    def test_context(self) -> None:
        node = make_node("v1-build", type="build", context={"files": ["src/a.py"]})
        assert node.context["files"] == ["src/a.py"]

    def test_max_retries(self) -> None:
        node = make_node("v1-build", type="build", max_retries=1)
        assert node.max_retries == 1

    def test_assigned_model(self) -> None:
        node = make_node("v1-build", type="build", assigned_model="claude-opus-4-6")
        assert node.assigned_model == "claude-opus-4-6"

    def test_state_override(self) -> None:
        node = make_node("v1-plan", type="plan", state="done")
        assert node.state == "done"

    def test_none_depends_on_becomes_empty(self) -> None:
        node = make_node("v1-build", type="build", depends_on=None)
        assert node.depends_on == []

    def test_none_context_becomes_empty(self) -> None:
        node = make_node("v1-build", type="build", context=None)
        assert node.context == {}

    def test_timestamps_set(self) -> None:
        node = make_node("v1-build", type="build")
        assert node.created_at is not None
        assert node.started_at is None
        assert node.completed_at is None
