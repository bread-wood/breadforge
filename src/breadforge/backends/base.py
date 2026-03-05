"""Backend protocol, response type, and credential proxy.

The ``CredentialProxy`` issues scoped, short-lived tokens so callers never
need to embed raw API keys in subprocesses or prompts.  Each token is bound
to a specific backend (anthropic / gemini / openai) and model name, and
expires after a configurable TTL.

Typical usage::

    proxy = CredentialProxy()
    token = proxy.issue_token("gemini", "gemini-2.0-flash", ttl_seconds=600)
    # pass ``token`` to the subprocess via a safe env var;
    # the subprocess validates it before calling the real API.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class BackendResponse:
    """Unified LLM response returned by every backend."""

    content: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


@runtime_checkable
class Backend(Protocol):
    """Protocol for pluggable LLM text-completion backends."""

    async def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 2048,
        system: str | None = None,
    ) -> BackendResponse:
        """Complete a prompt and return a :class:`BackendResponse`."""
        ...


# ---------------------------------------------------------------------------
# Credential proxy
# ---------------------------------------------------------------------------


@dataclass
class ScopedToken:
    """A short-lived token scoped to one backend and model."""

    token: str
    backend: str
    model: str
    issued_at: float = field(default_factory=time.time)
    ttl_seconds: int = 3600

    @property
    def expired(self) -> bool:
        return time.time() > self.issued_at + self.ttl_seconds


class CredentialProxy:
    """Loopback credential proxy that issues scoped tokens.

    Instead of injecting raw API keys into every subprocess environment,
    call :meth:`issue_token` to obtain a short-lived token bound to a
    specific backend and model.  Consumers call :meth:`validate` before
    making any real API call; the proxy resolves the token back to the
    backend scope and confirms it has not expired or been revoked.

    Example::

        proxy = CredentialProxy()
        token = proxy.issue_token("anthropic", "claude-sonnet-4-6", ttl_seconds=900)
        # inject ``token`` via an env var instead of ANTHROPIC_API_KEY
        scoped = proxy.validate(token)  # → ScopedToken(backend="anthropic", ...)
        proxy.revoke(token)             # invalidate immediately after use
    """

    def __init__(self) -> None:
        self._tokens: dict[str, ScopedToken] = {}

    def issue_token(
        self,
        backend: str,
        model: str,
        ttl_seconds: int = 3600,
    ) -> str:
        """Issue a new scoped token and return it."""
        token = secrets.token_urlsafe(32)
        self._tokens[token] = ScopedToken(
            token=token,
            backend=backend,
            model=model,
            ttl_seconds=ttl_seconds,
        )
        return token

    def validate(self, token: str) -> ScopedToken | None:
        """Return the :class:`ScopedToken` if valid, or ``None`` if expired/unknown."""
        st = self._tokens.get(token)
        if st is None:
            return None
        if st.expired:
            del self._tokens[token]
            return None
        return st

    def revoke(self, token: str) -> None:
        """Revoke a token immediately."""
        self._tokens.pop(token, None)

    def purge_expired(self) -> int:
        """Remove all expired tokens. Returns the count removed."""
        expired = [t for t, st in self._tokens.items() if st.expired]
        for t in expired:
            del self._tokens[t]
        return len(expired)
