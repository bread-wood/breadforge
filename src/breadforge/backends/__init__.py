"""Multi-backend LLM abstraction for breadforge.

Provides a pluggable backend registry so different node types can route
to different LLM providers:

- research/plan nodes → Gemini or GPT-4.1 (configurable via
  ``BREADFORGE_RESEARCH_BACKEND`` / ``BREADFORGE_PLAN_BACKEND``)
- build nodes → Claude (Anthropic) always

Usage::

    from breadforge.backends import get_backend

    backend = get_backend("gemini", model="gemini-2.0-flash")
    response = await backend.complete(prompt, max_tokens=1024)
    print(response.content)

The :class:`CredentialProxy` can be used to issue short-lived scoped tokens
instead of passing raw API keys into agent subprocesses::

    from breadforge.backends import CredentialProxy

    proxy = CredentialProxy()
    token = proxy.issue_token("gemini", "gemini-2.0-flash", ttl_seconds=600)
    # validate before calling the real API
    scoped = proxy.validate(token)
"""

from __future__ import annotations

from .anthropic import AnthropicBackend
from .base import Backend, BackendResponse, CredentialProxy, ScopedToken
from .gemini import GeminiBackend
from .openai import OpenAIBackend

__all__ = [
    "AnthropicBackend",
    "Backend",
    "BackendResponse",
    "CredentialProxy",
    "GeminiBackend",
    "OpenAIBackend",
    "ScopedToken",
    "get_backend",
]

_REGISTRY: dict[str, type] = {
    "anthropic": AnthropicBackend,
    "gemini": GeminiBackend,
    "openai": OpenAIBackend,
}


def get_backend(name: str, model: str | None = None, **kwargs: object) -> Backend:
    """Instantiate a backend by registry name.

    Args:
        name: Backend name — one of ``"anthropic"``, ``"gemini"``, ``"openai"``.
        model: Optional model override (uses the backend's built-in default if omitted).
        **kwargs: Forwarded to the backend constructor (e.g. ``api_key``).

    Raises:
        ValueError: If *name* is not a registered backend.
    """
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"unknown backend {name!r}; choices: {sorted(_REGISTRY)}")
    if model is not None:
        return cls(model=model, **kwargs)  # type: ignore[return-value]
    return cls(**kwargs)  # type: ignore[return-value]
