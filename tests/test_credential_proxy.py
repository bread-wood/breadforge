"""Tests for the CredentialProxy loopback abstraction.

The CredentialProxy replaces raw API key injection into subprocess environments.
Instead of passing ANTHROPIC_API_KEY, GOOGLE_API_KEY, etc. directly, agents
receive a short-lived scoped token.  The proxy validates the token, looks up the
backing credential, and forwards the API call — limiting blast radius if a
sub-agent leaks its credential.

This file defines the CredentialProxy contract; production implementation will
live in src/breadforge/credentials.py.

Behavioral contract:
  - issue_token(service, scope) returns a token string
  - resolve_token(token) returns the (service, credential) pair if valid
  - tokens expire after ttl_seconds
  - tokens are single-service: a token for "anthropic" cannot resolve "google"
  - revoke_token(token) invalidates immediately
  - env_for_node(node, router) returns an env dict with scoped tokens instead
    of raw keys
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import pytest

from breadforge.beads.types import GraphNode
from breadforge.config import Config

# ---------------------------------------------------------------------------
# CredentialProxy implementation (contract under test)
# ---------------------------------------------------------------------------


@dataclass
class ScopedToken:
    token: str
    service: str
    scope: str
    issued_at: float = field(default_factory=time.monotonic)
    ttl_seconds: float = 3600.0

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.issued_at) > self.ttl_seconds


class CredentialProxy:
    """Issues scoped tokens instead of exposing raw API credentials.

    Args:
        credentials: mapping of service name → raw API key
        ttl_seconds: lifetime for issued tokens (default 1 hour)
    """

    def __init__(
        self,
        credentials: dict[str, str] | None = None,
        ttl_seconds: float = 3600.0,
    ) -> None:
        self._credentials: dict[str, str] = credentials or {}
        self._tokens: dict[str, ScopedToken] = {}
        self._ttl = ttl_seconds
        self._counter = 0

    def register(self, service: str, api_key: str) -> None:
        """Register or update a backing credential."""
        self._credentials[service] = api_key

    def issue_token(self, service: str, scope: str = "default") -> str:
        """Issue a scoped token for a service.  Raises KeyError if service unknown."""
        if service not in self._credentials:
            raise KeyError(f"No credential registered for service: {service!r}")
        self._counter += 1
        token = f"bf-token-{service}-{self._counter:06d}"
        self._tokens[token] = ScopedToken(
            token=token,
            service=service,
            scope=scope,
            ttl_seconds=self._ttl,
        )
        return token

    def resolve_token(self, token: str) -> tuple[str, str] | None:
        """Resolve a token to (service, raw_api_key).  Returns None if invalid/expired."""
        entry = self._tokens.get(token)
        if entry is None or entry.expired:
            return None
        raw_key = self._credentials.get(entry.service)
        if raw_key is None:
            return None
        return entry.service, raw_key

    def revoke_token(self, token: str) -> bool:
        """Revoke a token immediately.  Returns True if it existed."""
        return self._tokens.pop(token, None) is not None

    def env_for_node(self, node: GraphNode) -> dict[str, str]:
        """Build an env dict with scoped tokens for the node's required services.

        Nodes declare required services via context["required_services"] list.
        Falls back to empty dict for unknown services (does not raise).
        """
        required: list[str] = node.context.get("required_services", [])
        env: dict[str, str] = {}
        service_env_keys: dict[str, str] = {
            "anthropic": "BREADFORGE_ANTHROPIC_TOKEN",
            "google": "BREADFORGE_GOOGLE_TOKEN",
            "openai": "BREADFORGE_OPENAI_TOKEN",
            "github": "BREADFORGE_GH_TOKEN",
        }
        for service in required:
            if service not in self._credentials:
                continue
            token = self.issue_token(service, scope=node.id)
            env_key = service_env_keys.get(service, f"BREADFORGE_{service.upper()}_TOKEN")
            env[env_key] = token
        return env


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def proxy() -> CredentialProxy:
    return CredentialProxy(
        credentials={
            "anthropic": "sk-ant-test-key-00000",
            "google": "AIza-test-key-00000",
            "openai": "sk-openai-test-key-00000",
        }
    )


@pytest.fixture
def config() -> Config:
    return Config.from_env("owner/repo")


# ---------------------------------------------------------------------------
# Token issuance
# ---------------------------------------------------------------------------


class TestCredentialProxyTokenIssuance:
    def test_issue_token_returns_string(self, proxy: CredentialProxy) -> None:
        token = proxy.issue_token("anthropic")
        assert isinstance(token, str)
        assert len(token) > 0

    def test_issue_token_for_unknown_service_raises(self, proxy: CredentialProxy) -> None:
        with pytest.raises(KeyError, match="no_such_service"):
            proxy.issue_token("no_such_service")

    def test_each_token_is_unique(self, proxy: CredentialProxy) -> None:
        t1 = proxy.issue_token("anthropic")
        t2 = proxy.issue_token("anthropic")
        assert t1 != t2

    def test_tokens_for_different_services_are_unique(self, proxy: CredentialProxy) -> None:
        t1 = proxy.issue_token("anthropic")
        t2 = proxy.issue_token("google")
        assert t1 != t2


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------


class TestCredentialProxyResolution:
    def test_resolve_valid_token_returns_service_and_key(self, proxy: CredentialProxy) -> None:
        token = proxy.issue_token("anthropic")
        result = proxy.resolve_token(token)
        assert result is not None
        service, key = result
        assert service == "anthropic"
        assert key == "sk-ant-test-key-00000"

    def test_resolve_unknown_token_returns_none(self, proxy: CredentialProxy) -> None:
        assert proxy.resolve_token("bf-token-fake-999999") is None

    def test_resolved_key_is_correct_for_service(self, proxy: CredentialProxy) -> None:
        token = proxy.issue_token("google")
        result = proxy.resolve_token(token)
        assert result is not None
        _, key = result
        assert key == "AIza-test-key-00000"

    def test_token_for_one_service_does_not_resolve_another(self, proxy: CredentialProxy) -> None:
        token = proxy.issue_token("anthropic")
        service, key = proxy.resolve_token(token)  # type: ignore[misc]
        assert service == "anthropic"
        assert key != proxy._credentials.get("google")


# ---------------------------------------------------------------------------
# Token expiry and revocation
# ---------------------------------------------------------------------------


class TestCredentialProxyExpiry:
    def test_expired_token_returns_none(self) -> None:
        proxy = CredentialProxy(
            credentials={"anthropic": "sk-test"},
            ttl_seconds=0.0,  # immediately expired
        )
        token = proxy.issue_token("anthropic")
        # Force expiry by using negative issued_at
        proxy._tokens[token].issued_at = time.monotonic() - 9999
        assert proxy.resolve_token(token) is None

    def test_revoke_token_invalidates_it(self, proxy: CredentialProxy) -> None:
        token = proxy.issue_token("anthropic")
        assert proxy.resolve_token(token) is not None
        revoked = proxy.revoke_token(token)
        assert revoked is True
        assert proxy.resolve_token(token) is None

    def test_revoke_nonexistent_token_returns_false(self, proxy: CredentialProxy) -> None:
        assert proxy.revoke_token("bf-token-ghost-000001") is False


# ---------------------------------------------------------------------------
# env_for_node
# ---------------------------------------------------------------------------


class TestCredentialProxyEnvForNode:
    def test_env_contains_anthropic_token_for_build_node(self, proxy: CredentialProxy) -> None:
        node = GraphNode(
            id="build-core",
            type="build",
            context={"required_services": ["anthropic"]},
        )
        env = proxy.env_for_node(node)
        assert "BREADFORGE_ANTHROPIC_TOKEN" in env
        token = env["BREADFORGE_ANTHROPIC_TOKEN"]
        # Token should be resolvable
        resolved = proxy.resolve_token(token)
        assert resolved is not None
        assert resolved[0] == "anthropic"

    def test_env_does_not_contain_raw_api_key(self, proxy: CredentialProxy) -> None:
        node = GraphNode(
            id="research-1",
            type="research",
            context={"required_services": ["google"]},
        )
        env = proxy.env_for_node(node)
        # Raw key must not appear in env values
        raw_key = "AIza-test-key-00000"
        assert raw_key not in env.values()
        assert "BREADFORGE_GOOGLE_TOKEN" in env

    def test_env_is_empty_for_node_with_no_services(self, proxy: CredentialProxy) -> None:
        node = GraphNode(id="plan-1", type="plan", context={})
        env = proxy.env_for_node(node)
        assert env == {}

    def test_env_skips_unknown_services_silently(self, proxy: CredentialProxy) -> None:
        node = GraphNode(
            id="build-1",
            type="build",
            context={"required_services": ["anthropic", "unknown_service"]},
        )
        env = proxy.env_for_node(node)
        assert "BREADFORGE_ANTHROPIC_TOKEN" in env
        # unknown_service silently skipped
        assert len(env) == 1

    def test_env_tokens_are_unique_per_node(self, proxy: CredentialProxy) -> None:
        node_a = GraphNode(
            id="build-a",
            type="build",
            context={"required_services": ["anthropic"]},
        )
        node_b = GraphNode(
            id="build-b",
            type="build",
            context={"required_services": ["anthropic"]},
        )
        env_a = proxy.env_for_node(node_a)
        env_b = proxy.env_for_node(node_b)
        assert env_a["BREADFORGE_ANTHROPIC_TOKEN"] != env_b["BREADFORGE_ANTHROPIC_TOKEN"]

    def test_register_new_service_then_issue(self, proxy: CredentialProxy) -> None:
        proxy.register("slack", "xoxb-test-token")
        token = proxy.issue_token("slack")
        result = proxy.resolve_token(token)
        assert result is not None
        assert result[0] == "slack"
        assert result[1] == "xoxb-test-token"
